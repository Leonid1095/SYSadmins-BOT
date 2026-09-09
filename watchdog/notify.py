#!/usr/bin/env python3
"""Доставка разбора владельцу — с кнопками вариантов.

Кнопка не исполняет действие. Она лишь называет боту номер варианта внутри
инцидента; что этот номер значит, бот прочитает из verdict.json, а исполнит
root-хелпер, который заново всё проверит. Поэтому в callback_data нет ни команд,
ни имён целей: перехваченная или подделанная кнопка не даёт ничего сверх того,
что уже разрешено каталогом.

Токен берём из окружения — его отдаёт systemd через EnvironmentFile, не открывая
процессу сам файл.
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TIMEOUT = 20

SEVERITY_MARK = {"crit": "🔴", "warn": "🟡", "info": "🔵", "resolved": "🟢"}
KIND_NAME = {
    "disk": "Диск", "memory": "Память", "cpu": "Процессор",
    "temperature": "Температура", "systemd": "Служба", "docker": "Контейнер",
    "http": "Сайт", "cert": "Сертификат", "smart": "Здоровье диска",
    "remote": "Удалённый сервер",
}

# Для этих видов ключ совпадает с названием и повторять его незачем: строка
# «Память: memory — ...» выглядела отладочным выводом, а не сообщением человеку.
KIND_IS_THE_KEY = {"memory", "cpu", "temperature"}

# А здесь наоборот: имя объекта владелец придумал сам, и оно говорит больше
# любого нашего слова. «Удалённый сервер Второй сервер» — не название, а склейка.
KEY_IS_ENOUGH = {"remote"}

# У сайтов и сертификатов ключ — полный URL. Схема и хвостовой слэш в строке
# уведомления не несут ничего: домен и так узнаётся.
def short_target(key):
    return re.sub(r"^https?://", "", str(key)).rstrip("/")


def subject_of(event):
    """О чём эта строка — словами. Общее для уведомления и для напоминания,
    иначе одно и то же событие называлось бы в них по-разному."""
    kind_id = event.get("kind")
    kind = KIND_NAME.get(kind_id, kind_id or "?")
    key = str(event.get("key") or "")

    if kind_id in KEY_IS_ENOUGH and key:
        return key
    if kind_id in KIND_IS_THE_KEY or key == kind_id or not key:
        return kind
    if kind_id in ("http", "cert"):
        return f"{kind} {short_target(key)}"
    return f"{kind} {KEY_NAME.get(key, key)}"

# Внутренние имена целей, у которых есть человеческое название.
KEY_NAME = {"root": "корневого раздела", "hdd": "с бэкапами"}

# Куда переехало состояние — словами. Владельцу важно не название полосы,
# а направление: стало хуже или отпустило.
DIRECTION = {
    "ok": "вернулось в норму",
    "warn": "стало хуже",
    "crit": "стало плохо",
    "resolved": "вернулось в норму",
    "flapping": "скачет туда-сюда",
}


def esc(value):
    """Экранируем три символа, а не четыре: кавычка в тексте безвредна, а
    &quot; вместо неё владелец читает как мусор в цитате из лога."""
    return html.escape(str(value), quote=False)


def describe_event(event):
    """Одна строка «что изменилось», написанная для человека."""
    mark = SEVERITY_MARK.get(event.get("severity"), "•")
    subject = f"<b>{esc(subject_of(event))}</b>"

    # У списочных событий направление уже сидит в самом описании: «упала»,
    # «снова запущен». Приписывать к нему «стало плохо» — говорить дважды.
    where = "" if event.get("list_field") else \
        DIRECTION.get(event.get("to") or event.get("severity"), "")
    detail = str(event.get("detail") or "")

    line = f"{mark} {subject}"
    if where:
        line += f" — {where}"
    if detail:
        line += f": {esc(detail)}"
    return line


def _proxy_opener():
    """Telegram у провайдера заблокирован — ходим тем же мостом, что и бот."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": proxy, "http": proxy}))


def api_call(token, method, payload):
    data = urllib.parse.urlencode(
        {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
         for k, v in payload.items() if v is not None}
    ).encode()
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data)
    try:
        with _proxy_opener().open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode() or "{}")
    except Exception as exc:
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


def render(result):
    """Собирает текст уведомления. HTML, а не MarkdownV2: экранировать надо
    ровно три символа, и содержимое логов не превратит сообщение в кашу."""
    verdict = result["verdict"]
    mark = SEVERITY_MARK.get(verdict["severity"], "⚪️")

    lines = [f"{mark} <b>{esc(verdict['headline'])}</b>", ""]
    if verdict.get("explanation"):
        lines += [esc(verdict["explanation"]), ""]

    lines.append("<i>Что изменилось:</i>")
    for event in result.get("events", [])[:8]:
        lines.append(describe_event(event))

    # Пометка о слабом разборе. Крит разбирается сильной моделью, но суточный
    # потолок таких разборов конечен, и владелец должен знать, что этот разбор
    # сделан обычной моделью, — иначе он поверит ему ровно так же.
    if result.get("meta", {}).get("crit_budget_exhausted"):
        lines += ["", "<i>Суточный запас усиленных разборов исчерпан — "
                      "это разбор обычной моделью.</i>"]

    # Подсказка про диалог. Без неё возможность не существует: владелец не
    # догадается, что на сообщение бота вообще можно отвечать.
    lines += ["", "<i>Ответьте на это сообщение, чтобы спросить подробнее.</i>"]
    # Идентификатор нужен не только для порядка: по нему бот находит инцидент,
    # когда владелец отвечает на сообщение. Отдельного хранилища для связи
    # «сообщение → инцидент» поэтому не требуется.
    lines += [f"<code>{esc(result['incident'])}</code>"]
    return "\n".join(lines)


def keyboard(result):
    """Кнопки вариантов. В callback_data — только номер варианта."""
    incident = result["incident"]
    rows = []
    for index, option in enumerate(result["verdict"]["options"]):
        rows.append([{
            "text": option["label"],
            "callback_data": f"wd:{incident}:{index}",
        }])
    return {"inline_keyboard": rows} if rows else None


def main():
    result = json.load(sys.stdin)
    if not result.get("analysed"):
        return 0

    token = os.environ.get("TELEGRAM_TOKEN", "")
    owner = os.environ.get("OWNER_ID", "")
    if not token or not owner:
        print("notify: TELEGRAM_TOKEN или OWNER_ID не заданы", file=sys.stderr)
        return 1

    response = api_call(token, "sendMessage", {
        "chat_id": owner,
        "text": render(result),
        "parse_mode": "HTML",
        "reply_markup": keyboard(result),
        "disable_web_page_preview": "true",
    })

    if not response.get("ok"):
        # Описание ошибки от Telegram печатаем, но токен в него не попадает.
        print(f"notify: не доставлено — {response.get('description')}", file=sys.stderr)
        return 1

    message_id = response["result"]["message_id"]
    # Номер сообщения нужен дожиму: напоминание должно уходить веткой к
    # исходному, а не отдельной строкой в ленте. Раньше он просто печатался
    # в stdout и терялся.
    remember(result.get("incident_dir"), message_id)
    print(json.dumps({"delivered": True, "incident": result["incident"],
                      "message_id": message_id}, ensure_ascii=False))
    return 0


def remember(incident_dir, message_id):
    """Кладёт номер сообщения рядом с вердиктом. Не вышло — не беда:
    напоминание уйдёт отдельным сообщением, а не пропадёт."""
    if not incident_dir:
        return
    path = os.path.join(incident_dir, "message.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"message_id": message_id}, f)
        os.chmod(path, 0o640)
    except OSError as exc:
        print(f"notify: номер сообщения не сохранён — {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
