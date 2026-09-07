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
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TIMEOUT = 20

SEVERITY_MARK = {"crit": "🔴", "warn": "🟡", "info": "🔵", "resolved": "🟢"}
KIND_NAME = {
    "disk": "Диск", "memory": "Память", "cpu": "Процессор",
    "temperature": "Температура", "systemd": "Сервис", "docker": "Контейнер",
    "http": "Эндпоинт", "cert": "Сертификат", "smart": "Диск (SMART)",
}


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
    esc = html.escape

    lines = [f"{mark} <b>{esc(verdict['headline'])}</b>", ""]
    if verdict.get("explanation"):
        lines += [esc(verdict["explanation"]), ""]

    lines.append("<i>Что изменилось:</i>")
    for event in result.get("events", [])[:8]:
        kind = KIND_NAME.get(event.get("kind"), event.get("kind", "?"))
        emark = SEVERITY_MARK.get(event.get("severity"), "•")
        detail = f" — {esc(str(event['detail']))}" if event.get("detail") else ""
        lines.append(f"{emark} {esc(kind)}: <code>{esc(str(event.get('key')))}</code>{detail}")

    lines += ["", f"<code>{esc(result['incident'])}</code>"]
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
    print(json.dumps({"delivered": True, "incident": result["incident"],
                      "message_id": response["result"]["message_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
