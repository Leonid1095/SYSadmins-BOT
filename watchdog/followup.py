#!/usr/bin/env python3
"""Дожим: напоминание о крите, на который никто не ответил.

Зачем это вообще. Критичный инцидент был ровно одним сообщением. Не заметили
ночью — тишина: никто не напомнит, а через две недели каталог инцидента
вычистится вместе с разбором. При этом аудит фиксирует нажатия, то есть
молчание владельца ничем не отличалось от «принял к сведению».

Как устроено. У инцидента появляется состояние:

    new → acknowledged (нажата любая кнопка или задан вопрос)
        → closed       (объект вернулся в норму сам)

Пока крит в `new`, напоминание уходит с растущей паузой — 10 мин, 30 мин, час,
дальше каждые три часа, не больше шести раз, потом сторож сообщает, что
перестаёт напоминать. В каждом напоминании — сколько прошло времени и
**сдвинулось ли что-нибудь**: «диск ушёл с 94% на 96%» полезнее, чем повтор
исходного текста.

Признак «отреагировал» не хранится отдельно, а выводится из следов, которые
владелец уже оставляет: `choices.jsonl` (нажатие, включая «ничего не делать») и
`dialogue.jsonl` (вопрос аналитику). Второй писатель через границу прав нам не
нужен — эти файлы пишет бот, а дожим их только читает.

Признак «закрылось само» берётся из снимка сторожа: `state.json` содержит и
текущие полосы, и последние факты, то есть ровно то, по чему делался вывод о
поломке. Отдельный опрос системы не нужен и был бы вторым источником правды.

> Известное ограничение, принятое сознательно. Напоминания идут тем же каналом,
> что и первое сообщение: Telegram через мост на 127.0.0.1:18443. Отвалится мост
> или сеть — не придёт ни исходное сообщение, ни один дожим. Запасной канал
> (ntfy/email) обвязан в monitor.sh и сторожем не используется. Отдельно: при
> по-настоящему умирающей машине сторож может не запуститься вовсе — на этот
> случай остаётся более грубый, но живучий monitor.sh из root-крона.
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402 — доставка и разбор события общие с первым сообщением

STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
INCIDENTS_DIR = os.path.join(STATE_DIR, "incidents")

# Паузы между напоминаниями, от предыдущего напоминания (первая — от инцидента).
# Растущая, а не постоянная: первые минуты решают, дальше владелец, скорее
# всего, спит, и долбить его каждые десять минут бессмысленно и вредно.
SCHEDULE = (10 * 60, 30 * 60, 60 * 60, 3 * 3600, 3 * 3600, 3 * 3600)

# Инцидент, который к первому взгляду дожима уже старше всего расписания,
# дожимать поздно: окно напоминаний прошло целиком, и начинать его задним
# числом — это залп накопленного, ровно тот, от которого delta.py защищается
# базовым снимком. Проверено на живых данных: к моменту включения на диске
# лежали два крита с прошлой ночи, и без этого правила владелец получил бы по
# шесть напоминаний о том, что и так знает.
STALE_AFTER = sum(SCHEDULE)

# Единицы измерения по имени поля — чтобы «94» превращалось в «94%», а не в
# голое число рядом со словом «было».
UNITS = {
    "pct": "%", "used_pct": "%", "swap_pct": "%", "load_per_core_pct": "%",
    "cpu_c": "°C", "disk_pct": "%", "mem_pct": "%", "cpu_pct": "%",
}


# --- Адресация значений в снимке --------------------------------------------

def _split_slot(slot):
    """Разбирает адрес объекта на голову и остаток.

    Разбирать целиком по точкам нельзя: в адресе сертификата и сайта сидит URL,
    а в нём точек сколько угодно."""
    head, _, rest = (slot or "").partition(".")
    return head, rest


def value_at(slot, facts):
    """Текущее значение объекта по его адресу из события.

    Возвращает пару (значение, имя поля) или (None, None), если в снимке такого
    объекта уже нет — например, сайт убрали из списка проверяемых.
    """
    head, rest = _split_slot(slot)
    if not head or not rest:
        return None, None

    if head == "cert":
        entry = (facts.get("endpoints") or {}).get(rest) or {}
        return entry.get("cert_days"), "cert_days"
    if head == "http":
        entry = (facts.get("endpoints") or {}).get(rest) or {}
        return entry.get("http"), "http"
    if head == "smart":
        return (facts.get("smart") or {}).get(rest), "smart"
    if head == "remote":
        name, _, field = rest.rpartition(".")
        entry = (facts.get("remote") or {}).get(name) or {}
        return entry.get(field), field

    # Числовые разделы: "disk.root.pct", "memory.memory.used_pct".
    name, _, field = rest.rpartition(".")
    section = facts.get(head)
    if not isinstance(section, dict) or not field:
        return None, None
    if name == head:                      # память и процессор лежат плоско
        return section.get(field), field
    entry = section.get(name)
    return (entry.get(field) if isinstance(entry, dict) else None), field


def _fmt(field, value):
    if value is None:
        return "нет данных"
    if field == "reachable":
        return "отвечает" if value else "молчит"
    if field == "cert_days":
        return f"{value} дн."
    if field == "http":
        return f"HTTP {value}"
    return f"{value}{UNITS.get(field, '')}"


# --- Состояние объекта прямо сейчас -----------------------------------------

def still_broken(event, bands, facts):
    """Объект из события всё ещё не в порядке?

    Полосы и факты берутся из снимка сторожа — того же, по которому делался
    вывод о поломке. Спрашивать систему заново здесь нельзя: получилось бы два
    источника правды, расходящихся в момент, когда это важнее всего.
    """
    if event.get("severity") == "resolved":
        return False

    list_field = event.get("list_field")
    if list_field:
        section = (facts.get(event.get("kind")) or {}).get(list_field) or []
        return event.get("key") in section

    slot = event.get("slot")
    if not slot:
        # Событие старого образца, без адреса. Считаем незакрытым: молчание
        # опаснее лишнего напоминания.
        return True
    return bands.get(slot, "ok") != "ok"


def movement(event, then_facts, now_facts):
    """Одна строка: сдвинулось ли что-нибудь с момента инцидента."""
    kind = notify.KIND_NAME.get(event.get("kind"), event.get("kind", "?"))
    key = str(event.get("key") or "")
    if event.get("kind") in notify.KIND_IS_THE_KEY or key == event.get("kind"):
        subject = kind
    else:
        subject = f"{kind} {notify.KEY_NAME.get(key, key)}"

    list_field = event.get("list_field")
    if list_field:
        section = (now_facts.get(event.get("kind")) or {}).get(list_field) or []
        if key in section:
            return f"{subject}: по-прежнему {list_field}"
        return f"{subject}: вернулся в норму сам"

    slot = event.get("slot")
    if not slot:
        return f"{subject}: {event.get('detail') or 'без подробностей'}"

    was, field = value_at(slot, then_facts)
    now, field_now = value_at(slot, now_facts)
    field = field_now or field
    if now is None:
        return f"{subject}: объекта больше нет в проверках"
    if was == now:
        return f"{subject}: без изменений, {_fmt(field, now)}"
    return f"{subject}: было {_fmt(field, was)}, сейчас {_fmt(field, now)}"


# --- Учёт напоминаний --------------------------------------------------------

def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data, mode=0o640):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, mode)


def acknowledged(incident_dir):
    """Владелец как-то отреагировал: нажал кнопку или спросил.

    Оба следа оставляет бот, дожим их только читает. Отдельного признака не
    заводим: он немедленно разошёлся бы с этими файлами.
    """
    for name in ("choices.jsonl", "dialogue.jsonl"):
        path = os.path.join(incident_dir, name)
        try:
            if os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    return False


def _created_ts(result, incident_dir):
    stamp = result.get("created_at")
    if stamp:
        try:
            return datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(os.path.join(incident_dir, "verdict.json"))
    except OSError:
        return time.time()


def humanize(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч" + (f" {minutes} мин" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} сут" + (f" {hours} ч" if hours else "")


# --- Сообщения ---------------------------------------------------------------

def render_reminder(result, moves, elapsed, is_last):
    verdict = result["verdict"]
    esc = html.escape
    lines = [
        f"🔴 <b>Без ответа {humanize(elapsed)}</b>",
        "",
        f"<b>{esc(verdict['headline'])}</b>",
        "",
        "<i>Что с тех пор:</i>",
    ]
    lines += [f"• {esc(m)}" for m in moves[:8]]
    if is_last:
        lines += ["", "<i>Это последнее напоминание — дальше молчу.</i>"]
    else:
        lines += ["", "<i>Ответьте на это сообщение, чтобы спросить подробнее.</i>"]
    lines.append(f"<code>{esc(result['incident'])}</code>")
    return "\n".join(lines)


def render_closed(result, elapsed):
    esc = html.escape
    return (
        f"🟢 <b>Закрылось само</b>\n\n"
        f"{esc(result['verdict']['headline'])}\n\n"
        f"Прошло {humanize(elapsed)}, объект вернулся в норму без вмешательства. "
        f"Напоминать больше не буду.\n"
        f"<code>{esc(result['incident'])}</code>"
    )


def send(token, owner, text, reply_to=None, keyboard=None):
    response = notify.api_call(token, "sendMessage", {
        "chat_id": owner,
        "text": text,
        "parse_mode": "HTML",
        "reply_to_message_id": str(reply_to) if reply_to else None,
        "reply_markup": keyboard,
        "disable_web_page_preview": "true",
    })
    if not response.get("ok"):
        print(f"followup: не доставлено — {response.get('description')}", file=sys.stderr)
        return None
    return response["result"]["message_id"]


# --- Основной проход ---------------------------------------------------------

def due(track, created_ts, now):
    """Пора ли напоминать и последнее ли это напоминание."""
    sent = track.get("sent", 0)
    if sent >= len(SCHEDULE):
        return False, False
    since = track.get("last_at") or created_ts
    if now - since < SCHEDULE[sent]:
        return False, False
    return True, sent + 1 == len(SCHEDULE)


def process(incident_dir, bands, facts, now, token, owner):
    """Один инцидент. Возвращает, что с ним сделали, — для журнала и тестов."""
    result = _read_json(os.path.join(incident_dir, "verdict.json"))
    if not isinstance(result, dict) or "verdict" not in result:
        return "нет вердикта"

    track = _read_json(os.path.join(incident_dir, "followup.json"), {}) or {}
    if track.get("state") in ("acknowledged", "closed", "exhausted", "stale"):
        return track["state"]

    # Дожимаем только крит. Warn и info живут одним сообщением: напоминать о
    # них значит вернуть тот самый спам, от которого только что уходили.
    if result["verdict"].get("severity") != "crit":
        return "не крит"

    events = result.get("events") or []
    created = _created_ts(result, incident_dir)
    elapsed = now - created
    path = os.path.join(incident_dir, "followup.json")

    if acknowledged(incident_dir):
        track.update({"state": "acknowledged", "at": now})
        _write_json(path, track)
        return "acknowledged"

    if track.get("sent", 0) == 0 and elapsed > STALE_AFTER:
        track.update({"state": "stale", "at": now})
        _write_json(path, track)
        return "просрочен"

    if not any(still_broken(e, bands, facts) for e in events):
        track.update({"state": "closed", "at": now})
        _write_json(path, track)
        # Молча — если ещё ни разу не напоминали. Владелец и так получит
        # отдельное сообщение «всё вернулось в норму» от обычного разбора,
        # а два сообщения об одном и том же — снова спам.
        if track.get("sent", 0):
            send(token, owner, render_closed(result, elapsed),
                 reply_to=track.get("reply_to"))
        return "closed"

    ready, is_last = due(track, created, now)
    if not ready:
        return "ждём"

    # Снимок на момент инцидента лежит рядом — его положил туда аналитик,
    # чтобы модель могла в него смотреть. Он же годится, чтобы показать,
    # насколько с тех пор сдвинулось.
    then = _read_json(os.path.join(incident_dir, "facts.json"), {}) or {}
    then_facts = then.get("facts") if isinstance(then.get("facts"), dict) else then
    moves = [movement(e, then_facts, facts) for e in events]
    if not moves:
        moves = ["подробностей нет"]

    reply_to = track.get("reply_to") or (
        _read_json(os.path.join(incident_dir, "message.json"), {}) or {}).get("message_id")
    message_id = send(token, owner, render_reminder(result, moves, elapsed, is_last),
                      reply_to=reply_to, keyboard=notify.keyboard(result))
    if message_id is None:
        return "не доставлено"

    track["sent"] = track.get("sent", 0) + 1
    track["last_at"] = now
    track["reply_to"] = reply_to or message_id
    track["state"] = "exhausted" if is_last else "new"
    _write_json(path, track)
    return "напомнили"


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "")
    owner = os.environ.get("OWNER_ID", "")
    if not token or not owner:
        print("followup: TELEGRAM_TOKEN или OWNER_ID не заданы", file=sys.stderr)
        return 1

    state = _read_json(STATE_FILE, {}) or {}
    bands, facts = state.get("bands") or {}, state.get("facts") or {}
    if not bands:
        # Снимка нет — судить не о чем. Молчим, а не напоминаем вслепую.
        return 0

    now = time.time()
    try:
        names = sorted(os.listdir(INCIDENTS_DIR))
    except OSError:
        return 0

    done = {}
    for name in names:
        path = os.path.join(INCIDENTS_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            done[name] = process(path, bands, facts, now, token, owner)
        except Exception as exc:   # один битый инцидент не должен глушить остальные
            print(f"followup: {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            done[name] = "сбой"

    interesting = {k: v for k, v in done.items()
                   if v in ("напомнили", "closed", "acknowledged", "сбой", "не доставлено")}
    if interesting:
        print(json.dumps(interesting, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
