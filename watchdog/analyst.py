#!/usr/bin/env python3
"""Аналитик на Claude Agent SDK — единственное место, где просыпается модель.

Вызывается только при непустой дельте. В спокойный день этот файл не выполняет
ни строки, и подписка не расходуется.

Что модель может: читать файлы каталога инцидента, куда наши детекторы заранее
сложили факты и добранный контекст. Всё. У неё нет Bash, Write, Edit, сети и
подтягивания пользовательских настроек. Read-only здесь не обещание в промпте,
а отсутствие инструмента: даже полностью убеждённая инъекцией модель не найдёт,
чем воспользоваться.

Что модель отдаёт: разбор и варианты действий, названные идентификаторами из
каталога. Команду по идентификатору соберёт наш код, причём заново и уже без
участия модели.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog          # noqa: E402
import sandbox          # noqa: E402 — граница read-only, общая с диалогом
from deepen import deepen  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    query,
)

STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog")
INCIDENTS_DIR = os.path.join(STATE_DIR, "incidents")

# Модель и граница read-only живут в sandbox.py — там же, откуда их берёт диалог.
MAX_TURNS = int(os.environ.get("WATCHDOG_MAX_TURNS", "12"))

# Сколько разборов в сутки позволено делать сильной моделью. Дребезжащий крит
# иначе выел бы общую подписку: подавление дребезга ловит частые скачки, но не
# череду разных критов подряд.
MAX_CRIT_UPGRADES_PER_DAY = int(os.environ.get("WATCHDOG_CRIT_PER_DAY", "8"))
KEEP_INCIDENTS_DAYS = 14

SYSTEM_PROMPT = f"""\
Ты — сторож сервера. Твоя работа: объяснить владельцу, что именно произошло,
насколько это срочно, и предложить, что с этим делать.

# Что у тебя есть

Рабочий каталог — это каталог инцидента. В нём:
* `events.json` — что изменилось с прошлой проверки (ради этого тебя и разбудили);
* `facts.json` — полный снимок состояния машины на сейчас;
* `context/` — заранее собранные логи и раскладки по затронутым объектам.

Читай их через Read и Grep. Больше у тебя инструментов нет: ты ничего не
запускаешь и ничего не меняешь. Это осознанное ограничение, не сбой.

{sandbox.UNTRUSTED_INPUT_WARNING}

# Что вернуть

Ровно один объект JSON, без пояснений вокруг, без markdown-ограды:

{{
  "severity": "crit" | "warn" | "info",
  "headline": "одна строка, до 80 символов, суть без вводных",
  "explanation": "2-5 предложений: что произошло, почему, чем грозит",
  "options": [
    {{"action": "идентификатор", "target": "цель или null",
      "label": "текст кнопки, до 30 символов",
      "why": "одно предложение: что это даст"}}
  ]
}}

Правила по `options`:
* не больше четырёх вариантов, они станут кнопками в Telegram;
* последним всегда `{{"action": "nothing"}}` — право владельца ничего не делать;
* `action` — строго идентификатор из каталога ниже, ничего своего;
* если действия из каталога не подходят, оставь только `nothing` и объясни
  в `explanation`, что тут нужны руки и почему.

# Каталог действий

{catalog.describe_for_model()}

# Тон

Пиши по-русски, как инженер инженеру: коротко, конкретно, без «возможно, стоит
рассмотреть». Если причина непонятна — так и скажи, и назови, что посмотреть
руками. Не выдумывай причин, которых не видно в данных.
"""


def new_incident_dir():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(INCIDENTS_DIR, stamp)
    suffix = 0
    while os.path.exists(path):
        suffix += 1
        path = os.path.join(INCIDENTS_DIR, f"{stamp}-{suffix}")
    # 0770 нужны, чтобы бот (tgbot) мог дописать сюда выбор владельца и вопрос
    # к аналитику: общего у них ровно столько, сколько нужно, через группу
    # watchdog.
    #
    # chmod обязателен отдельной строкой. Режим в makedirs режется umask, а под
    # systemd он 0022 — каталоги получались 2750, без записи для группы. Из-за
    # этого молча не работала запись выбора владельца: record_choice гасил
    # ошибку прав, и аудит нажатий был пуст всё время существования сторожа.
    os.makedirs(path, mode=0o770)
    os.chmod(path, 0o2770)
    return path


def prune_incidents():
    """Каталоги инцидентов не должны копиться вечно."""
    cutoff = time.time() - KEEP_INCIDENTS_DAYS * 86400
    try:
        entries = os.listdir(INCIDENTS_DIR)
    except OSError:
        return
    for name in entries:
        path = os.path.join(INCIDENTS_DIR, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o640)   # бот читает вердикт, писать в него ему незачем


def extract_json(text):
    """Достаёт объект JSON из ответа модели.

    Просим голый JSON, но модель вправе обрамить его оградой или предисловием,
    и падать из-за этого сторож не должен. Ищем последний сбалансированный
    объект: если предисловие есть, полезная нагрузка в конце.
    """
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text)
    depth, start = 0, None
    candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


def sanitize(verdict, events):
    """Приводит ответ модели к тому, что мы готовы показать и исполнить.

    Модель могла назвать действие не из каталога, приписать цель туда, где её
    быть не должно, или забыть про `nothing`. Чиним молча: это не повод терять
    разбор целиком, но и доверять форме ответа на слово нельзя.
    """
    severity = verdict.get("severity")
    if severity not in ("crit", "warn", "info"):
        worst = {e.get("severity") for e in events}
        severity = "crit" if "crit" in worst else "warn" if "warn" in worst else "info"

    options, seen = [], set()
    for raw in verdict.get("options") or []:
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        spec = catalog.ACTIONS.get(action)
        if spec is None:
            continue  # не из каталога — молча выбрасываем
        target = raw.get("target") if spec["target"] else None
        if spec["target"] and not target:
            continue  # действию нужна цель, а её не назвали
        if (action, target) in seen:
            continue
        seen.add((action, target))
        options.append({
            "action": action,
            "target": target,
            "label": str(raw.get("label") or spec["title"])[:30],
            "why": str(raw.get("why") or spec["purpose"])[:200],
        })
        if len(options) >= 4:
            break

    if not any(o["action"] == "nothing" for o in options):
        options = options[:3] + [{
            "action": "nothing", "target": None,
            "label": "Ничего не делать", "why": "Принять к сведению.",
        }]

    return {
        "severity": severity,
        "headline": str(verdict.get("headline") or "Изменилось состояние сервера")[:80],
        "explanation": str(verdict.get("explanation") or "").strip()[:2000],
        "options": options,
    }


async def analyse(incident_dir, events, model=None):
    """Один заход модели. Возвращает текст ответа и служебные показатели."""
    prompt = (
        "Разберись, что произошло. Начни с events.json — это то, что изменилось. "
        "Затем посмотри facts.json и файлы в context/, относящиеся к затронутым "
        "объектам. Верни один объект JSON в оговорённом формате."
    )
    options = sandbox.options(cwd=incident_dir, system_prompt=SYSTEM_PROMPT,
                              max_turns=MAX_TURNS, model=model)

    chunks, meta = [], {}
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            meta = {
                "turns": getattr(message, "num_turns", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "is_error": getattr(message, "is_error", None),
            }
    return "\n".join(chunks), meta


def _all_resolved(events):
    """Все события — про то, что стало лучше."""
    return bool(events) and all(e.get("severity") == "resolved" for e in events)


def _resolved_verdict(events):
    """Вердикт без модели для случая «всё вернулось в норму».

    Будить модель ради «проблемы больше нет» — плата за ничто: 30-40 секунд и
    токены, чтобы получить ответ, который известен заранее. Именно так появился
    инцидент 12:41 «Тревога по памяти снята» — полноценный разбор ради хорошей
    новости. Хорошую новость сообщаем сами, коротко.
    """
    what = ", ".join(str(e.get("key")) for e in events[:3])
    if len(events) > 3:
        what += f" и ещё {len(events) - 3}"
    return {
        "severity": "info",
        "headline": "Всё вернулось в норму",
        "explanation": (
            f"То, о чём приходило предупреждение, снова в порядке: {what}. "
            "Делать ничего не нужно — это сообщение просто закрывает прошлое."),
        "options": [{"action": "nothing", "target": None,
                     "label": "Понятно", "why": "Закрыть уведомление."}],
    }


def _crit_model(events):
    """Сильная модель — только на настоящем крите и только пока есть суточный запас."""
    if not any(e.get("severity") == "crit" for e in events):
        return None
    path = os.path.join(STATE_DIR, ".crit-upgrades.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    used = data.get("count", 0) if data.get("day") == today else 0
    if used >= MAX_CRIT_UPGRADES_PER_DAY:
        return None
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"day": today, "count": used + 1}, f)
    except OSError:
        pass
    return sandbox.MODEL_CRIT


def main():
    delta = json.load(sys.stdin)
    events = delta.get("events") or []

    if not events:
        # Ради этого всё и затевалось: тихий день не стоит ни одного токена.
        json.dump({"analysed": False, "reason": "дельты нет"},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    os.makedirs(INCIDENTS_DIR, mode=0o2770, exist_ok=True)
    prune_incidents()

    incident_dir = new_incident_dir()
    _write(os.path.join(incident_dir, "events.json"), events)
    _write(os.path.join(incident_dir, "facts.json"), delta.get("snapshot", {}))
    files = deepen(events, os.path.join(incident_dir, "context"))

    if _all_resolved(events):
        # Хорошая новость модели не требует.
        verdict = _resolved_verdict(events)
        text, meta = "", {"skipped": "все события — возврат в норму"}
        result = {
            "analysed": True,
            "incident": os.path.basename(incident_dir),
            "incident_dir": incident_dir,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "events": events,
            "verdict": verdict,
            "context_files": files,
            "meta": meta,
        }
        _write(os.path.join(incident_dir, "verdict.json"), result)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    try:
        text, meta = asyncio.run(analyse(incident_dir, events, _crit_model(events)))
    except Exception as exc:
        # Модель недоступна — это не повод молчать о поломке. Отдаём сырые
        # события, чтобы уведомление всё равно ушло владельцу.
        verdict = {
            "severity": "crit" if any(e.get("severity") == "crit" for e in events) else "warn",
            "headline": "Изменилось состояние сервера (разбор недоступен)",
            "explanation": f"Аналитик не отработал: {type(exc).__name__}: {exc}. "
                           "События ниже приведены как есть.",
            "options": [{"action": "nothing", "target": None,
                         "label": "Ничего не делать", "why": "Принять к сведению."}],
        }
        text, meta = "", {"error": str(exc)}
    else:
        parsed = extract_json(text)
        if parsed is None:
            verdict = {
                "severity": "warn",
                "headline": "Изменилось состояние сервера",
                "explanation": (text.strip()[:1500] or "Модель не вернула разбор."),
                "options": [],
            }
        else:
            verdict = parsed
        verdict = sanitize(verdict, events)

    result = {
        "analysed": True,
        "incident": os.path.basename(incident_dir),
        "incident_dir": incident_dir,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": events,
        "verdict": verdict,
        "context_files": files,
        "meta": meta,
    }
    _write(os.path.join(incident_dir, "verdict.json"), result)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
