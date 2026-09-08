"""Диалог по инциденту: владелец спрашивает — аналитик отвечает.

Зачем. Разбор приходит одним сообщением и заканчивается тремя кнопками. Спросить
«почему упало?», «это уже было?», «что смотреть руками?» было нельзя — ответ в
Telegram просто проваливался в пустоту, потому что бот текстовые сообщения вне
диалога добавления сервера не обрабатывал вовсе.

Что здесь НЕ появляется. Ни одного нового полномочия. Тот же каталог инцидента
рабочим каталогом, те же `Read`/`Grep`/`Glob` из `sandbox.py`, тот же пустой
`setting_sources`. Модель получает не права, а ходы. Поэтому диалог и сделан
первым: пользы много, граница безопасности не двигается.

Разговор — не пульт. Отсюда нельзя ничего выполнить: чтобы что-то изменилось,
владелец жмёт кнопку, действие сверяется с каталогом, а исполняет его root-хелпер,
проверяющий заявку заново. Соблазн «раз он понимает, пусть и сделает» закрыт
отсутствием инструмента, а не обещанием в промпте.

Бюджет. Подписка общая с повседневной работой владельца, поэтому число вопросов
ограничено и на инцидент, и на сутки. Упёршись в потолок, бот говорит об этом
прямо — молчание было бы неотличимо от поломки.

# Почему вопрос и ответ разнесены по файлам

Бот работает под `tgbot`, а подписка Claude привязана к `plg`: `.credentials.json`
читается только владельцем, а у `tgbot` вдобавок `HOME=/nonexistent`. Позвать
модель прямо из бота попросту нельзя.

Соблазнительное решение — дать боту `sudo -u plg` — тут неприемлемо: у `plg`
беспарольный sudo на всё, то есть «стать plg» и «стать root» это одно и то же.
Ровно от этого бот и отделяли.

Поэтому стороны общаются через отдельный каталог `ask/`, доступный обеим по
группе `watchdog`:

    бот (tgbot) → ask/<метка>.request → служба watchdog-ask (plg)
                → ask/<метка>.response → бот

Службу поднимает path-юнит systemd по появлению файла, так что ответ начинает
готовиться сразу, а не по таймеру.

Каталог именно плоский, и это не вкусовщина: `PathExistsGlob` в systemd следит
только за тем уровнем, где стоит шаблон. Проверено — файл в
`incidents/<id>/ask.request` служба не замечала вовсе, а тот же файл уровнем
выше запускал её мгновенно.

Права нигде не пересекаются: бот кладёт текст, `plg` его читает. Ничего
исполняемого через эту границу не ходит, а идентификатор происшествия из заявки
служба проверяет заново — своей же строгой проверкой формы.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

import sandbox
from claude_agent_sdk import AssistantMessage, TextBlock, query

MAX_TURNS = int(os.environ.get("WATCHDOG_ASK_TURNS", "8"))

# Потолки. Инцидент разбирают, а не переписываются с ним неделями; суточный
# предел защищает подписку от случайного залипания на кнопке.
MAX_PER_INCIDENT = int(os.environ.get("WATCHDOG_ASK_PER_INCIDENT", "12"))
MAX_PER_DAY = int(os.environ.get("WATCHDOG_ASK_PER_DAY", "40"))

# Сколько последних пар вопрос-ответ показываем модели. Больше не нужно:
# полный контекст инцидента она и так перечитывает с диска.
HISTORY_DEPTH = 6

MAX_QUESTION_LEN = 1000
LOCK_STALE_SECONDS = 300

SYSTEM_PROMPT = f"""\
Ты — сторож сервера. Ты уже разобрал этот инцидент и прислал владельцу вердикт.
Теперь владелец задаёт уточняющий вопрос, и ты отвечаешь.

# Что у тебя есть

Рабочий каталог — каталог инцидента:
* `events.json` — что изменилось и разбудило проверку;
* `facts.json` — полный снимок состояния машины на тот момент;
* `context/` — заранее собранные логи и раскладки по затронутым объектам;
* `verdict.json` — твой собственный прошлый разбор.

Читай их через Read и Grep. Других инструментов у тебя нет: ты ничего не
запускаешь и ничего не меняешь. Это ограничение, а не сбой, и обходить его не
нужно — если для ответа нужна команда, назови её владельцу, он выполнит сам.

{sandbox.UNTRUSTED_INPUT_WARNING}

# Как отвечать

* По-русски, как инженер инженеру: коротко, конкретно, без вводных.
* Не больше 15 строк. Это сообщение в Telegram, а не отчёт.
* Опирайся на файлы. Не знаешь — так и скажи и назови, что посмотреть руками
  (конкретной командой).
* Не выдумывай причин, которых не видно в данных. «В логах этого нет» —
  нормальный ответ.
* Не предлагай нажать кнопки, которых нет в вердикте.
* Простой текст. Без markdown-разметки, без таблиц, без ``` — сообщение уйдёт
  как есть.
"""


class AskError(Exception):
    """Ответить нельзя. Текст показывается владельцу как есть."""


def _dialogue_path(incident_dir):
    return os.path.join(incident_dir, "dialogue.jsonl")


def history(incident_dir):
    """Прошлые вопросы и ответы по этому инциденту."""
    try:
        with open(_dialogue_path(incident_dir), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _record(incident_dir, entry):
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(_dialogue_path(incident_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(_dialogue_path(incident_dir), 0o660)  # пишет бот, читает сторож
    except OSError:
        pass


def _day_counter_path(incident_dir):
    """Суточный счётчик лежит рядом с инцидентами, а не внутри одного из них."""
    return os.path.join(os.path.dirname(incident_dir.rstrip("/")), ".ask-quota.json")


def _check_and_bump_daily_quota(incident_dir):
    path = _day_counter_path(incident_dir)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    used = data.get(today, 0) if data.get("day") == today else 0
    if used >= MAX_PER_DAY:
        raise AskError(
            f"На сегодня лимит вопросов исчерпан ({MAX_PER_DAY}). "
            "Так подписка не расходуется на переписку целиком. "
            "Ограничение снимается в полночь по UTC.")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"day": today, today: used + 1}, f)
        os.chmod(path, 0o660)
    except OSError:
        pass


class _Lock:
    """Не даёт запустить два аналитика на один каталог инцидента."""

    def __init__(self, incident_dir):
        self.path = os.path.join(incident_dir, ".ask.lock")

    def __enter__(self):
        try:
            age = time.time() - os.path.getmtime(self.path)
            if age < LOCK_STALE_SECONDS:
                raise AskError("Предыдущий вопрос ещё обрабатывается — подождите.")
            os.unlink(self.path)          # замок протух, прошлый заход не дожил
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
            os.close(fd)
        except FileExistsError:
            raise AskError("Предыдущий вопрос ещё обрабатывается — подождите.")
        except OSError:
            pass                          # без замка хуже, но не смертельно
        return self

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _build_prompt(question, past):
    lines = []
    if past:
        lines.append("Уже обсуждалось в этом инциденте:")
        for turn in past[-HISTORY_DEPTH:]:
            lines.append(f"  Владелец: {turn.get('q', '')}")
            lines.append(f"  Ты: {turn.get('a', '')}")
        lines.append("")
    lines.append(f"Вопрос владельца: {question}")
    lines.append("")
    lines.append("Ответь, опираясь на файлы каталога инцидента.")
    return "\n".join(lines)


async def _run(incident_dir, prompt):
    options = sandbox.options(cwd=incident_dir, system_prompt=SYSTEM_PROMPT,
                              max_turns=MAX_TURNS)
    chunks = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "\n".join(chunks).strip()


def _validate(incident_dir, question):
    """Общие проверки. Делаются на обеих сторонах: бот отвечает владельцу сразу,
    а служба не доверяет тому, что получила через файл."""
    question = (question or "").strip()
    if not question:
        raise AskError("Пустой вопрос.")
    if len(question) > MAX_QUESTION_LEN:
        raise AskError(f"Вопрос длиннее {MAX_QUESTION_LEN} символов — сократите.")
    if not os.path.isdir(incident_dir):
        raise AskError("Происшествие не найдено — возможно, оно уже устарело и удалено.")
    if len(history(incident_dir)) >= MAX_PER_INCIDENT:
        raise AskError(
            f"По этому происшествию уже задано {MAX_PER_INCIDENT} вопросов — это предел. "
            "Если разговор зашёл так далеко, дальше быстрее посмотреть руками.")
    return question


def ask(incident_dir, question):
    """Зовёт модель и возвращает ответ. Исполняется ТОЛЬКО под plg (см. заголовок).

    Бот эту функцию не вызывает: у него нет credentials подписки.
    """
    question = _validate(incident_dir, question)
    _check_and_bump_daily_quota(incident_dir)
    past = history(incident_dir)

    with _Lock(incident_dir):
        try:
            answer = asyncio.run(_run(incident_dir, _build_prompt(question, past)))
        except Exception as exc:
            raise AskError(f"Аналитик не ответил: {type(exc).__name__}: {exc}")

    if not answer:
        raise AskError("Аналитик промолчал. Попробуйте переформулировать вопрос.")

    _record(incident_dir, {"q": question, "a": answer})
    return answer


# --- Обмен между ботом и службой -------------------------------------------

STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog")
ASK_DIR = os.path.join(STATE_DIR, "ask")
INCIDENTS_DIR = os.path.join(STATE_DIR, "incidents")

# Метка запроса — имя файла, поэтому форму проверяем строго.
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
INCIDENT_RE = re.compile(r"^\d{8}T\d{6}Z(?:-\d{1,3})?$")

RESPONSE_KEEP_SECONDS = 600


def _incident_dir(incident_id):
    """Каталог происшествия по идентификатору из заявки.

    Служба не доверяет тому, что пришло файлом, даже от бота: имя проверяется
    формой, иначе до файловой системы дошло бы что угодно.
    """
    if not INCIDENT_RE.match(incident_id or ""):
        raise AskError("Не понял, о каком происшествии речь.")
    path = os.path.join(INCIDENTS_DIR, incident_id)
    if not os.path.isdir(path):
        raise AskError("Это происшествие уже удалено — они хранятся две недели.")
    return path


def submit(incident_dir, question):
    """Кладёт вопрос для службы-ответчика. Возвращает метку этого запроса."""
    question = _validate(incident_dir, question)
    token = os.urandom(8).hex()
    payload = {"incident": os.path.basename(incident_dir.rstrip("/")),
               "question": question,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        os.makedirs(ASK_DIR, mode=0o770, exist_ok=True)
        path = os.path.join(ASK_DIR, f"{token}.request")
        tmp = path + ".tmp"
        # Через временный файл: служба не должна увидеть половину заявки.
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp, 0o660)
        os.replace(tmp, path)
    except OSError as exc:
        raise AskError(f"Не удалось передать вопрос сторожу: {exc}")
    return token


def collect(token):
    """Читает ответ на конкретный запрос. None — ещё не готов."""
    if not TOKEN_RE.match(token or ""):
        return None
    path = os.path.join(ASK_DIR, f"{token}.response")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("error"):
        raise AskError(data["error"])
    return data.get("answer")


def _prune_responses():
    """Прочитанные ответы никому не нужны — но и удалять сразу нельзя:
    бот может ещё не успеть их забрать."""
    cutoff = time.time() - RESPONSE_KEEP_SECONDS
    try:
        names = os.listdir(ASK_DIR)
    except OSError:
        return
    for name in names:
        if not name.endswith(".response"):
            continue
        path = os.path.join(ASK_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


def serve():
    """Отвечает на все ожидающие вопросы. Зовётся из ask-runner.py под plg."""
    answered = 0
    _prune_responses()
    try:
        names = sorted(os.listdir(ASK_DIR))
    except OSError:
        return 0

    for name in names:
        if not name.endswith(".request"):
            continue
        token = name[:-len(".request")]
        if not TOKEN_RE.match(token):
            continue
        request = os.path.join(ASK_DIR, name)
        try:
            with open(request, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            payload = {}
        try:
            os.unlink(request)       # снимаем заявку до работы: не зациклимся
        except OSError:
            pass

        try:
            incident_dir = _incident_dir(payload.get("incident"))
            result = {"answer": ask(incident_dir, payload.get("question"))}
        except AskError as exc:
            result = {"error": str(exc)}
        except Exception as exc:     # noqa: BLE001 — служба не должна падать целиком
            result = {"error": f"Сбой при разборе вопроса: {type(exc).__name__}: {exc}"}

        try:
            path = os.path.join(ASK_DIR, f"{token}.response")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            os.chmod(path, 0o660)
        except OSError:
            pass
        answered += 1
    return answered
