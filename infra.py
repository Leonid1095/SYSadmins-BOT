"""Инфраструктура центрального сервера — для показа в боте.

Данные берутся из снимка сторожа (`/var/lib/watchdog/state.json`), который
`watchdog/delta.py` переписывает каждые пять минут. Своих измерений здесь нет, и
это осознанно: бот работает под `tgbot`, у которого нет ни доступа к docker, ни
sudo на smartctl. Собирать то же самое второй раз означало бы выдать боту —
процессу, смотрящему в интернет, — права, которых у него сейчас нет.

Читать снимок бот может без новых прав: каталог `2750 plg:watchdog`, файл
`0640 plg:watchdog`, а `tgbot` состоит в группе `watchdog`.

Отсюда же берётся карточка самого центрального сервера. Агент для этого не
нужен: он живёт на 127.0.0.1, а его ключ лежит в файле, читаемом только root.
Снимок сторожа содержит те же диск, память, процессор и температуру — и достаётся
без сети, без секрета и без открытого порта.

Формулировки здесь человеческие намеренно. «swap_pct: 80» ничего не говорит
владельцу в час ночи; «подкачка занята на 80% — память кончается, система начала
выгружать процессы на диск» говорит.
"""

import html
import json
import os
from datetime import datetime, timezone


def esc(value) -> str:
    """Экранирование для HTML-сообщений Telegram.

    Нужно на КАЖДОМ значении, пришедшем из снимка. Имена контейнеров задаёт тот,
    кто их запускал, домены приходят из конфига, тексты ошибок — из библиотек.
    Неэкранированный `<` ломает разметку целиком: Telegram отвечает 400, и
    владелец не видит сообщения вообще — ровно в тот момент, когда что-то упало.
    """
    return html.escape(str(value))

STATE_FILE = os.path.join(
    os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog"), "state.json")

# Те же пороги, по которым сторож решает, будить ли модель (watchdog/delta.py).
# Держим их согласованными: иначе бот покажет «всё хорошо» там, где сторож уже
# считает проблемой, и владелец перестанет верить обоим.
BANDS = {
    "disk":    (80, 90),
    "memory":  (90, 95),
    "cpu":     (150, 250),
    "temp":    (80, 90),
}
# Своп — особый случай, см. watchdog/delta.py: сам по себе он ничего не значит,
# ядро просто не забирает обратно давно не тронутые страницы. Тревожен он только
# вместе с нехваткой оперативной памяти.
SWAP_RISING = (90, 98)
SWAP_NEEDS_RAM_PCT = 75

CERT_DAYS = (30, 10)   # warn / crit

MARK = {"ok": "🟢", "warn": "🟡", "crit": "🔴", "unknown": "⚪️"}


class SnapshotUnavailable(Exception):
    """Снимка нет или он нечитаем. Текст показывается владельцу как есть."""


def load_snapshot():
    """Последний снимок сторожа. Отсутствие файла — не сбой, а состояние."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SnapshotUnavailable(
            "Сторож ещё ни разу не отработал — данных пока нет.\n"
            "Первый снимок появится в течение пяти минут после запуска таймера.")
    except PermissionError:
        raise SnapshotUnavailable(
            "Нет доступа к снимку сторожа. Проверьте, что бот состоит в группе "
            "<code>watchdog</code>.")
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotUnavailable(f"Снимок сторожа не читается: {exc}")


def age_text(snapshot):
    """Возраст данных словами. Владельцу важно понимать, насколько цифры свежие."""
    raw = snapshot.get("updated_at")
    if not raw:
        return "время снимка неизвестно"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return "время снимка неизвестно"
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 90:
        return "только что"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} сут назад — сторож, похоже, не работает"


def _band(value, warn, crit):
    if value is None:
        return "unknown"
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "ok"


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- Карточка центрального сервера -----------------------------------------

def render_host(snapshot):
    """Состояние самой машины — человеческим языком, с объяснением каждой цифры."""
    facts = snapshot.get("facts", {})
    lines = []

    disk = (facts.get("disk") or {}).get("root") or {}
    pct, free = disk.get("pct"), disk.get("free_gb")
    band = _band(pct, *BANDS["disk"])
    if pct is None:
        lines.append(f"{MARK['unknown']} <b>Диск</b> — данных нет")
    else:
        tail = {
            "ok": "запас есть",
            "warn": "уже стоит посмотреть, чем занято",
            "crit": "место вот-вот кончится, нужно чистить",
        }[band]
        lines.append(f"{MARK[band]} <b>Диск</b> — занято {pct}%, "
                     f"свободно {free} ГБ. {tail.capitalize()}.")

    hdd = (facts.get("disk") or {}).get("hdd")
    if hdd:
        band = _band(hdd.get("pct"), *BANDS["disk"])
        lines.append(f"{MARK[band]} <b>Диск бэкапов</b> — занято {hdd.get('pct')}%, "
                     f"свободно {hdd.get('free_gb')} ГБ.")

    mem = facts.get("memory") or {}
    used, total = mem.get("used_pct"), mem.get("total_gb")
    band = _band(used, *BANDS["memory"])
    if used is not None:
        tail = {
            "ok": "хватает",
            "warn": "памяти в обрез",
            "crit": "память на исходе, процессы могут начать падать",
        }[band]
        lines.append(f"{MARK[band]} <b>Память</b> — занято {used}% из {total} ГБ. "
                     f"{tail.capitalize()}.")

    swap = mem.get("swap_pct")
    if swap:
        # Тот же критерий, что у сторожа: своп важен только при нехватке ОЗУ.
        if (used or 0) < SWAP_NEEDS_RAM_PCT:
            band = "ok"
            tail = ("Это не проблема: память свободна, а в подкачке просто лежит "
                    "то, к чему давно не обращались.")
        else:
            band = _band(swap, *SWAP_RISING)
            tail = ("Оперативной памяти не хватает, и система выгружает процессы "
                    "на диск — всё, что туда попало, работает заметно медленнее.")
        lines.append(f"{MARK[band]} <b>Подкачка</b> — занята на {swap}%. {tail}")

    cpu = facts.get("cpu") or {}
    load_pct, cores = cpu.get("load_per_core_pct"), cpu.get("cores")
    band = _band(load_pct, *BANDS["cpu"])
    if load_pct is not None:
        tail = {
            "ok": "сервер не загружен",
            "warn": "очередь на процессор длиннее обычного",
            "crit": "процессор не справляется, задачи стоят в очереди",
        }[band]
        lines.append(f"{MARK[band]} <b>Процессор</b> — загрузка {load_pct}% "
                     f"на {cores} {_plural(cores, 'ядро', 'ядра', 'ядер')} "
                     f"(за 5 мин). {tail.capitalize()}.")

    temp = (facts.get("temperature") or {}).get("cpu_c")
    if temp is not None:
        band = _band(temp, *BANDS["temp"])
        tail = {"ok": "норма", "warn": "горячевато",
                "crit": "перегрев, проверьте охлаждение"}[band]
        lines.append(f"{MARK[band]} <b>Температура</b> — {temp}°C. {tail.capitalize()}.")

    smart = facts.get("smart") or {}
    bad = [d for d, v in smart.items() if v != "PASSED"]
    if bad:
        lines.append(f"{MARK['crit']} <b>Здоровье дисков</b> — "
                     f"{esc(', '.join(bad))} не проходят самопроверку. "
                     "Это предвестник отказа: запланируйте замену.")
    elif smart:
        lines.append(f"{MARK['ok']} <b>Здоровье дисков</b> — "
                     f"самопроверку прошли все ({len(smart)} шт).")

    return "\n".join(lines)


# --- Разделы инфраструктуры -------------------------------------------------

def overview_counts(snapshot):
    """Числа для сводки. Возвращает (что, всего, проблемных) по каждому разделу."""
    facts = snapshot.get("facts", {})

    docker = facts.get("docker") or {}
    broken = (docker.get("stopped", []) + docker.get("unhealthy", [])
              + docker.get("restarting", []))
    total = docker.get("total")

    systemd = facts.get("systemd") or {}
    failed = systemd.get("failed", [])

    endpoints = facts.get("endpoints") or {}
    sites_bad = [u for u, e in endpoints.items()
                 if e.get("http") is None or int(e.get("http", 0)) >= 500]
    certs = {u: e["cert_days"] for u, e in endpoints.items() if "cert_days" in e}
    certs_bad = [u for u, d in certs.items() if d <= CERT_DAYS[0]]

    return {
        "docker": (total, len(broken)),
        "systemd": (None, len(failed)),
        "sites": (len(endpoints), len(sites_bad)),
        "certs": (len(certs), len(certs_bad)),
    }


def render_overview(snapshot):
    counts = overview_counts(snapshot)
    lines = []

    total, broken = counts["docker"]
    mark = MARK["ok"] if not broken else MARK["warn"]
    if total:
        lines.append(f"{mark} <b>Контейнеры</b> — работают {total - broken} из {total}"
                     + (f", проблемных {broken}" if broken else ""))
    else:
        lines.append(f"{mark} <b>Контейнеры</b> — "
                     + (f"проблемных {broken}" if broken else "все в порядке"))

    _, failed = counts["systemd"]
    mark = MARK["ok"] if not failed else MARK["crit"]
    lines.append(f"{mark} <b>Службы</b> — "
                 + (f"упало {failed}" if failed else "упавших нет"))

    total, bad = counts["sites"]
    mark = MARK["ok"] if not bad else MARK["crit"]
    lines.append(f"{mark} <b>Сайты</b> — отвечают {total - bad} из {total}"
                 + (f", молчат {bad}" if bad else ""))

    total, bad = counts["certs"]
    mark = MARK["ok"] if not bad else MARK["warn"]
    lines.append(f"{mark} <b>Сертификаты</b> — под наблюдением {total}"
                 + (f", истекают скоро {bad}" if bad else ", все свежие"))

    security = snapshot.get("facts", {}).get("security") or {}
    if security:
        bans = security.get("crowdsec_bans")
        if bans is not None:
            lines.append(f"🛡 <b>Заблокировано адресов</b> — {bans}")

    return "\n".join(lines)


def render_containers(snapshot):
    docker = (snapshot.get("facts") or {}).get("docker")
    if docker is None:
        return ("Данных по контейнерам нет — сторож не смог опросить Docker.\n"
                "Обычно это значит, что демон не запущен.")
    total = docker.get("total")
    head = (f"Всего контейнеров: <b>{total}</b>\n\n" if total else "")

    blocks = []
    for key, title, why in (
        ("stopped", "Остановлены",
         "Контейнер не запущен. Часть из них гасится намеренно — такие перечислены "
         "в исключениях и сторож про них молчит."),
        ("unhealthy", "Нездоровы",
         "Контейнер работает, но его собственная проверка здоровья не проходит: "
         "снаружи сервис уже может не отвечать."),
        ("restarting", "Перезапускаются",
         "Контейнер падает и поднимается по кругу. Смотреть логи — сам он не выправится."),
    ):
        names = docker.get(key) or []
        if not names:
            continue
        listing = "\n".join(f"  • <code>{esc(n)}</code>" for n in names)
        blocks.append(f"<b>{title} ({len(names)})</b>\n<i>{why}</i>\n{listing}")

    if not blocks:
        return head + f"{MARK['ok']} Все контейнеры работают и проходят проверку здоровья."
    return head + "\n\n".join(blocks)


def render_services(snapshot):
    systemd = (snapshot.get("facts") or {}).get("systemd")
    if systemd is None:
        return "Данных по службам нет — сторож не смог опросить systemd."
    failed = systemd.get("failed") or []
    if not failed:
        return (f"{MARK['ok']} Упавших служб нет.\n\n"
                "<i>Сюда попадают только юниты в состоянии failed — то есть те, "
                "что systemd пытался поднять и сдался.</i>")
    listing = "\n".join(f"  • <code>{esc(n)}</code>" for n in failed)
    return (f"{MARK['crit']} <b>Упавшие службы ({len(failed)})</b>\n\n{listing}\n\n"
            "<i>systemd пытался поднять их и сдался. Причина — в журнале: "
            "<code>journalctl -u имя-службы -n 50</code></i>")


def render_sites(snapshot):
    endpoints = (snapshot.get("facts") or {}).get("endpoints")
    if not endpoints:
        return ("Список сайтов пуст.\n\n"
                "<i>Он берётся из ENDPOINTS в monitor.local.conf.</i>")

    ok, broken = [], []
    for url, entry in sorted(endpoints.items()):
        code = entry.get("http")
        host = url.replace("https://", "").replace("http://", "")
        if code is None:
            broken.append(f"  {MARK['crit']} <code>{esc(host)}</code> — не ответил "
                          f"({esc(entry.get('error', 'нет связи'))})")
        elif int(code) >= 500:
            broken.append(f"  {MARK['crit']} <code>{esc(host)}</code> — ошибка сервера ({esc(code)})")
        elif int(code) >= 400:
            ok.append(f"  {MARK['warn']} <code>{esc(host)}</code> — {esc(code)}")
        else:
            ok.append(f"  {MARK['ok']} <code>{esc(host)}</code> — {esc(code)}")

    parts = []
    if broken:
        parts.append("<b>Не отвечают</b>\n" + "\n".join(broken))
    if ok:
        parts.append(f"<b>Отвечают ({len(ok)})</b>\n" + "\n".join(ok))
    parts.append("<i>Проверка идёт с самого сервера, мимо прокси — так же, "
                 "как сайт видит обычный посетитель. Коды 4xx это не поломка "
                 "сервера, а ответ «страницы нет».</i>")
    return "\n\n".join(parts)


def render_certs(snapshot):
    endpoints = (snapshot.get("facts") or {}).get("endpoints") or {}
    certs = {u: e["cert_days"] for u, e in endpoints.items() if "cert_days" in e}
    if not certs:
        return "Данных по сертификатам нет."

    rows = []
    for url, days in sorted(certs.items(), key=lambda kv: kv[1]):
        host = url.replace("https://", "").replace("http://", "")
        if days <= CERT_DAYS[1]:
            mark, note = MARK["crit"], " — продлевать сейчас"
        elif days <= CERT_DAYS[0]:
            mark, note = MARK["warn"], " — пора продлевать"
        else:
            mark, note = MARK["ok"], ""
        rows.append(f"  {mark} <code>{esc(host)}</code> — {days} "
                    f"{_plural(days, 'день', 'дня', 'дней')}{note}")

    return ("<b>Сколько осталось сертификатам</b>\n\n" + "\n".join(rows) +
            "\n\n<i>Let's Encrypt обычно продлевает сам за 30 дней до конца. "
            "Если счётчик ушёл ниже 30 и не растёт — автопродление сломалось.</i>")


def render_security(snapshot):
    security = (snapshot.get("facts") or {}).get("security")
    if not security:
        return ("Данных нет — не установлены ни crowdsec, ни fail2ban, "
                "либо сторожу не хватило прав их опросить.")
    lines = []
    bans = security.get("crowdsec_bans")
    if bans is not None:
        lines.append(f"🛡 <b>CrowdSec</b> — сейчас заблокировано адресов: {bans}")
    jails = security.get("fail2ban_jails")
    if jails is not None:
        lines.append(f"🚧 <b>fail2ban</b> — активных правил: {jails}")
    lines.append("")
    lines.append("<i>Это косвенный признак: резкий рост числа блокировок значит, "
                 "что машину щупают активнее обычного. Сам по себе рост — "
                 "не поломка, защита работает именно так.</i>")
    return "\n".join(lines)


def render_remote(snapshot):
    """Удалённые серверы — как их видит сторож, с объяснением отказов."""
    remote = (snapshot.get("facts") or {}).get("remote")
    if not remote:
        return None
    lines = []
    for name, entry in sorted(remote.items()):
        ip = entry.get("ip", "?")
        if entry.get("reachable"):
            lines.append(
                f"  {MARK['ok']} <b>{esc(name)}</b> (<code>{esc(ip)}</code>) — "
                f"диск {entry.get('disk_pct')}%, память {entry.get('mem_pct')}%, "
                f"процессор {entry.get('cpu_pct')}%")
        else:
            lines.append(f"  {MARK['crit']} <b>{esc(name)}</b> (<code>{esc(ip)}</code>) — "
                         f"{esc(entry.get('error', 'нет связи'))}")
    return "\n".join(lines)
