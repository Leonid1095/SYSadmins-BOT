#!/usr/bin/env python3
"""Сравнение снимков: что именно изменилось со времени прошлого запуска.

Это единственное место, где решается, что считать событием. Модель просыпается
только если здесь что-то нашлось, поэтому цена ошибки несимметрична: лишнее
событие стоит токенов и доверия к уведомлениям, пропущенное — стоит поломки.

Два правила против шума, оба выстраданы практикой monitor.sh:

* Полосы вместо чисел. Событие рождает переход ok→warn→crit, а не каждый
  изменившийся процент. Диск, третьи сутки стоящий на 81%, молчит.
* Гистерезис. Чтобы выйти из полосы вниз, значение должно отойти от порога на
  запас. Иначе диск, дышащий вокруг 80%, будил бы модель каждые пять минут.

Первый запуск ничего не сообщает: он лишь запоминает базу. Иначе установка
сторожа обернулась бы залпом из всего, что накопилось за годы.
"""

import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONF = os.path.join(BASE_DIR, "monitor.local.conf")
STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Пороги. «Растущие» — чем больше, тем хуже; для сертификатов наоборот.
RISING = {
    ("disk", "pct"): (80, 90),
    ("memory", "used_pct"): (90, 95),
    ("cpu", "load_per_core_pct"): (150, 250),
    ("temperature", "cpu_c"): (80, 90),
}

# Своп намеренно вынесен из общего списка. Сам по себе он ничего не значит:
# ядро складывает туда страницы, которых давно не касались, и не забирает их
# обратно, пока они не понадобятся. Своп на 80% при 25 свободных гигабайтах
# ОЗУ — это история прошлого всплеска, а не проблема сейчас.
#
# Ровно на таком колебании сторож будил модель дважды за полдня (08:22 «своп
# 80%», 12:41 «тревога снята, 65%»), и оба раза модель приходила к выводу, что
# всё в порядке. Подавление дребезга это не ловит: оно рассчитано на четыре
# смены за полчаса, а не на медленный дрейф через порог за сутки.
#
# Своп становится новостью только вместе с давлением на саму память.
SWAP_RISING = (90, 98)
SWAP_NEEDS_RAM_PCT = 75
# Пороги для удалённых серверов. Отдельно от локальных: там мы видим только
# то, что отдал агент, и лезть глубже некуда.
REMOTE_RISING = {
    "disk_pct": (80, 90),
    "mem_pct": (90, 95),
    "cpu_pct": (85, 95),
}

CERT_DAYS = (30, 10)   # warn / crit — предупредить, пока продление ещё возможно
DEADBAND = 3           # запас на выход из полосы вниз

# Дребезг: объект, который циклит crit→resolved→crit, будил бы модель каждые
# пять минут и выедал подписку, ничего не сообщая сверх первого раза. После
# FLAP_LIMIT смен полосы за FLAP_WINDOW он замолкает на FLAP_MUTE, а владелец
# получает одно сообщение — про сам дребезг, который и есть настоящая новость.
FLAP_WINDOW = 30 * 60
FLAP_LIMIT = 4
FLAP_MUTE = 60 * 60

SEVERITY_ORDER = {"ok": 0, "resolved": 0, "info": 1, "warn": 2, "crit": 3}


def describe(field, name, value):
    """Подпись к числу словами.

    Раньше в сообщение уходило «swap_pct=80» — имя внутреннего поля и число.
    Владельцу это ничего не говорит, а выглядит как отладочный вывод.
    """
    # Какой именно диск/объект — подставляет notify.py в заголовке строки,
    # здесь повторять незачем: получалось «Диск корневого раздела: занято 91%
    # (корневой раздел)».
    texts = {
        "pct": f"занято {value}%",
        "used_pct": f"оперативной памяти занято {value}%",
        "load_per_core_pct": f"очередь к процессору {value}% от числа ядер",
        "cpu_c": f"температура процессора {value}°C",
        # То же для удалённых серверов. Раньше их метрики уходили как
        # «disk_pct=93» — тот же отладочный вывод, от которого избавлялись
        # у локальных, просто в ветке, до которой не дошли руки.
        "disk_pct": f"диск занят на {value}%",
        "mem_pct": f"оперативной памяти занято {value}%",
        "cpu_pct": f"процессор загружен на {value}%",
    }
    return texts.get(field, f"{field}={value}")


# Как называется по-русски то, что мы нашли в списке. Сами имена полей —
# внутренние: «Контейнер svod-bot-1 — стало плохо: stopped» выглядит выводом
# отладчика, а не сообщением человеку.
LIST_STATE = {
    "failed": ("упала", "снова работает"),
    # PLGamesBot. Для зрителя все три — одно и то же: бот в чате не отвечает.
    "banned": ("нашего бота забанили — снять: /unban plgames_bot",
               "бан снят, бот снова отвечает"),
    "nomod": ("боту не дали прав модератора, поэтому он молчит — нужна /mod plgames_bot",
              "права модератора вернулись, бот заговорил"),
    "no_signal": ("бот числится включённым, но не подаёт признаков жизни",
                  "бот снова выходит на связь"),
    "advertised_silent": ("канал в эфире с молчащим ботом, а мы показываем его "
                          "в публичном списке — приводим зрителя туда, где нашего "
                          "продукта нет", "убран из публичного списка"),
    "unreachable": ("публичный список не проверить — правило про молчащие каналы "
                    "сейчас никто не сторожит", "публичный список снова проверяется"),
    "stopped": ("не запущен", "снова запущен"),
    "unhealthy": ("работает, но проверка здоровья не проходит", "проверка здоровья снова проходит"),
    "restarting": ("перезапускается по кругу", "перестал перезапускаться"),
}


def describe_list(field, recovered):
    """Строка для события из списка — в нужную сторону."""
    bad, good = LIST_STATE.get(field, (field, f"больше не {field}"))
    return good if recovered else bad


# --- Полосы -----------------------------------------------------------------

def rising_band(value, warn, crit, previous):
    """Полоса для «чем больше, тем хуже», с запасом на возврат вниз."""
    if value >= crit:
        return "crit"
    if value >= warn:
        band = "warn"
    else:
        band = "ok"
    # Спуск требует уйти ниже порога на DEADBAND, подъём — нет.
    if previous == "crit" and value > crit - DEADBAND:
        return "crit"
    if previous in ("crit", "warn") and band == "ok" and value > warn - DEADBAND:
        return "warn"
    return band


def falling_band(days, warn, crit):
    """Полоса для сертификатов: меньше дней — хуже. Гистерезис не нужен,
    остаток убывает на день в сутки и дрожать у порога не может."""
    if days <= crit:
        return "crit"
    if days <= warn:
        return "warn"
    return "ok"


# --- Конфигурация исключений ------------------------------------------------

# Значения по умолчанию совпадают с monitor.sh. Раньше их здесь не было, и
# сторож брал списки только из monitor.local.conf: если владелец их там не
# переопределил, исключений не было вовсе — и сторож будил модель на комнатах
# civ4col, которые гасятся сами.
DEFAULT_IGNORE = {
    "docker": "civ4col-pitboss*",
    "systemd": "fwupd.service fwupd-refresh.service",
}


def _conf_list(name, default=""):
    """Читает NAME="a b c" из monitor.local.conf, не исполняя файл."""
    try:
        with open(LOCAL_CONF, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return default.split()
    match = re.search(rf'^\s*{name}="([^"]*)"', text, re.MULTILINE)
    return match.group(1).split() if match else default.split()


def ignored():
    """Контейнеры и юниты, чья остановка — норма, а не авария.

    Переиспользуем те же списки, что и monitor.sh: если комнаты civ4col гасятся
    сами через 30 минут, сторож не должен считать это происшествием.

    Элементы списка — шаблоны, а не точные имена: комнат заводят новые, и
    перечислять каждую значит однажды забыть. Ровно так и вышло — pitboss4,
    5 и 6 появились после того, как список писали, и каждое их автогашение
    давало тревогу."""
    return {
        "docker": _conf_list("DOCKER_IGNORE", DEFAULT_IGNORE["docker"]),
        "systemd": _conf_list("SERVICES_IGNORE", DEFAULT_IGNORE["systemd"]),
    }


def _drop_ignored(names, patterns):
    """Отсеивает имена, подходящие под любой шаблон исключений."""
    return {n for n in names if not any(fnmatch.fnmatch(n, p) for p in patterns)}


# --- Сравнение --------------------------------------------------------------

class DeltaBuilder:
    def __init__(self, previous_bands, flap_state=None, now_ts=None):
        self.previous_bands = previous_bands or {}
        self.flap = {k: dict(v) for k, v in (flap_state or {}).items()}
        self.now_ts = now_ts if now_ts is not None else time.time()
        self.bands = {}
        self.events = []

    def _flapping(self, slot):
        """Регистрирует смену полосы и отвечает, замолчал ли объект.

        Возвращает "muted", если объект уже признан дребезжащим и молчит;
        "started", если признан прямо сейчас; None, если всё в порядке.
        """
        record = self.flap.setdefault(slot, {"changes": [], "muted_until": 0})

        if self.now_ts < record["muted_until"]:
            return "muted"

        window_start = self.now_ts - FLAP_WINDOW
        record["changes"] = [t for t in record["changes"] if t >= window_start]
        record["changes"].append(self.now_ts)

        if len(record["changes"]) >= FLAP_LIMIT:
            record["muted_until"] = self.now_ts + FLAP_MUTE
            record["changes"] = []
            return "started"
        return None

    def _emit(self, kind, key, was, now, detail, slot=None, list_field=None):
        """Событие рождается только при смене полосы.

        `slot` и `list_field` — адрес объекта в снимке. Они не нужны ни модели,
        ни уведомлению, а нужны дожиму (`followup.py`): чтобы через полчаса
        сказать «диск ушёл с 94% на 96%», надо знать, где именно смотреть
        значение, а по паре «вид + имя» это восстанавливается неоднозначно —
        у памяти, например, два разных числа с одинаковым видом и ключом.
        """
        if was == now:
            return

        state = self._flapping(f"{kind}.{key}")
        if state == "muted":
            return
        if state == "started":
            self.events.append({
                "kind": kind, "key": key, "from": was, "to": "flapping",
                "severity": "warn", "flapping": True,
                "detail": f"состояние скачет ({detail}); дальнейшие смены "
                          f"на {FLAP_MUTE // 60} мин не сообщаются",
            })
            return

        severity = "resolved" if SEVERITY_ORDER[now] < SEVERITY_ORDER.get(was, 0) else now
        event = {
            "kind": kind, "key": key,
            "from": was, "to": now,
            "severity": severity, "detail": detail,
        }
        if slot:
            event["slot"] = slot
        if list_field:
            event["list_field"] = list_field
        self.events.append(event)

    def numeric(self, facts):
        for (section, field), (warn, crit) in RISING.items():
            data = facts.get(section)
            if not isinstance(data, dict):
                continue
            # disk вложен на уровень глубже: {"root": {...}, "hdd": {...}}
            entries = data.items() if field not in data else [(section, data)]
            for name, values in entries:
                if not isinstance(values, dict) or field not in values:
                    continue
                value = values[field]
                slot = f"{section}.{name}.{field}"
                was = self.previous_bands.get(slot, "ok")
                now = rising_band(value, warn, crit, was)
                self.bands[slot] = now
                self._emit(section, name, was, now, describe(field, name, value),
                           slot=slot)

    def swap(self, facts):
        """Своп — только при одновременном давлении на оперативную память."""
        memory = facts.get("memory")
        if not isinstance(memory, dict) or "swap_pct" not in memory:
            return
        slot = "memory.memory.swap_pct"
        was = self.previous_bands.get(slot, "ok")
        swap_pct = memory["swap_pct"]
        ram_pct = memory.get("used_pct", 0)

        if ram_pct < SWAP_NEEDS_RAM_PCT:
            # Память свободна — что бы ни лежало в свопе, это не новость.
            now = "ok"
        else:
            now = rising_band(swap_pct, *SWAP_RISING, was)

        self.bands[slot] = now
        self._emit("memory", "memory", was, now,
                   f"подкачка занята на {swap_pct}%, "
                   f"при этом оперативной памяти занято {ram_pct}%", slot=slot)

    def certificates(self, facts):
        for url, entry in (facts.get("endpoints") or {}).items():
            days = entry.get("cert_days")
            if days is None:
                continue
            slot = f"cert.{url}"
            was = self.previous_bands.get(slot, "ok")
            now = falling_band(days, *CERT_DAYS)
            self.bands[slot] = now
            self._emit("cert", url, was, now, f"остаётся {days} дн.", slot=slot)

    def http(self, facts):
        for url, entry in (facts.get("endpoints") or {}).items():
            code = entry.get("http")
            slot = f"http.{url}"
            was = self.previous_bands.get(slot, "ok")
            now = "ok" if code is not None and 200 <= code < 400 else "crit"
            self.bands[slot] = now
            self._emit("http", url, was, now,
                       f"HTTP {code}" if code else "нет ответа", slot=slot)

    def remote(self, facts):
        """Доступность удалённых серверов и их метрики.

        Недоступность — самое важное здесь: если агент молчит, всё остальное
        про этот сервер мы всё равно не знаем.
        """
        for name, entry in (facts.get("remote") or {}).items():
            slot = f"remote.{name}.reachable"
            # Сервер, которого в прошлом снимке не было, — это не поломка, а
            # первое наблюдение. Владелец добавляет его в бот ДО установки
            # агента (бот на этом шаге и выдаёт команду установки), так что
            # «недоступен» — ожидаемое состояние первых минут. Без этой
            # оговорки добавление трёх серверов означало бы три критичных
            # разбора подряд про то, что владелец и так делает руками.
            # Это та же мысль, что и базовый снимок при первом запуске.
            first_time = slot not in self.previous_bands
            was = self.previous_bands.get(slot, "ok")
            reachable = bool(entry.get("reachable"))
            now = "ok" if reachable else "crit"
            self.bands[slot] = now
            detail = "агент отвечает" if reachable else (entry.get("error") or "агент молчит")
            if not first_time:
                self._emit("remote", name, was, now, detail, slot=slot)

            if not reachable:
                continue
            for field, (warn, crit) in REMOTE_RISING.items():
                if field not in entry:
                    continue
                value = entry[field]
                metric_slot = f"remote.{name}.{field}"
                was = self.previous_bands.get(metric_slot, "ok")
                band = rising_band(value, warn, crit, was)
                self.bands[metric_slot] = band
                self._emit("remote", name, was, band,
                           describe(field, name, value), slot=metric_slot)

    def smart(self, facts):
        for dev, verdict in (facts.get("smart") or {}).items():
            slot = f"smart.{dev}"
            was = self.previous_bands.get(slot, "ok")
            now = "ok" if verdict.upper() in ("PASSED", "OK") else "crit"
            self.bands[slot] = now
            self._emit("smart", dev, was, now, f"самопроверка — {verdict}", slot=slot)

    def lists(self, previous_facts, facts, skip):
        """Списки сравниваем поимённо: замена одной поломки другой не должна
        пройти молча только потому, что счётчик не изменился."""
        sources = [
            ("systemd", "failed", "crit", skip["systemd"]),
            ("docker", "stopped", "warn", skip["docker"]),
            ("docker", "unhealthy", "crit", skip["docker"]),
            ("docker", "restarting", "crit", skip["docker"]),
            # PLGamesBot. Крит по решению владельца 09.09.2026: канал, где бот
            # молчит, — это остановленный продукт у живого стримера, и узнавать
            # об этом через пять дней мы уже пробовали.
            ("plgamesbot", "banned", "crit", ()),
            ("plgamesbot", "nomod", "crit", ()),
            ("plgamesbot", "no_signal", "crit", ()),
            ("plgamesbot_public", "advertised_silent", "crit", ()),
            # А это не поломка продукта, а слепота сторожа: сама ручка не
            # ответила. Крит на неё повесил бы вторую тревогу поверх той,
            # которую и так поднимет проверка сайтов.
            ("plgamesbot_public", "unreachable", "warn", ()),
        ]
        for section, field, severity, ignore in sources:
            before, after = previous_facts.get(section), facts.get(section)
            # Раздела не было в прошлом снимке — это первое наблюдение, а не
            # залп аварий: ровно так же ведёт себя только что добавленный
            # сервер. Иначе включение нового детектора означало бы пачку
            # критов о том, что владелец и так знает, и съеденный лимит
            # сильной модели.
            #
            # Раздела нет сейчас — детектор в этот проход не отработал.
            # Молчание не равно «всё починилось»: раньше пропавший раздел
            # объявлял выздоровевшими все упавшие юниты разом.
            if before is None or after is None:
                continue
            was = set(before.get(field) or [])
            now = set(after.get(field) or [])
            was, now = _drop_ignored(was, ignore), _drop_ignored(now, ignore)
            for item in sorted(now - was):
                self._emit(section, item, "ok", severity,
                           describe_list(field, recovered=False), list_field=field)
            for item in sorted(was - now):
                self._emit(section, item, severity, "ok",
                           describe_list(field, recovered=True), list_field=field)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state):
    os.makedirs(STATE_DIR, mode=0o2750, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o640)


def compare(snapshot, state):
    facts = snapshot.get("facts", {})
    skip = ignored()

    if state is None:
        # Первый запуск: запоминаем базу и молчим.
        builder = DeltaBuilder({}, None)
        builder.numeric(facts)
        builder.swap(facts)
        builder.certificates(facts)
        builder.http(facts)
        builder.smart(facts)
        builder.remote(facts)
        return [], builder.bands, True, builder.flap

    builder = DeltaBuilder(state.get("bands"), state.get("flap"))
    builder.numeric(facts)
    builder.swap(facts)
    builder.certificates(facts)
    builder.http(facts)
    builder.smart(facts)
    builder.remote(facts)
    builder.lists(state.get("facts", {}), facts, skip)
    return builder.events, builder.bands, False, builder.flap


def main():
    snapshot = json.load(sys.stdin)
    state = load_state()
    events, bands, baseline, flap = compare(snapshot, state)

    save_state({
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bands": bands,
        "flap": flap,
        "facts": snapshot.get("facts", {}),
    })

    events.sort(key=lambda e: -SEVERITY_ORDER.get(e["severity"], 0))
    json.dump({
        "baseline": baseline,
        "events": events,
        "snapshot": snapshot,
    }, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
