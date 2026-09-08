"""Мост между кнопкой в Telegram и root-исполнителем.

Кнопка приносит только идентификатор инцидента и номер варианта. Что это за
вариант, читается здесь из verdict.json, записанного аналитиком, — бот не берёт
ни действие, ни цель из самого нажатия. Даже подделанная callback_data не
выберет ничего, кроме варианта, который аналитик уже предложил, а исполнитель
всё равно проверит его заново.
"""

import json
import os
import re
import subprocess

STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog")
INCIDENTS_DIR = os.path.join(STATE_DIR, "incidents")
REMEDY = "/usr/local/sbin/watchdog-remedy"

# Идентификатор инцидента — это имя каталога, поэтому форму проверяем строго:
# всё, что не подходит, до файловой системы не доходит.
INCIDENT_RE = re.compile(r"^\d{8}T\d{6}Z(?:-\d{1,3})?$")

REMEDY_TIMEOUT = 150


class IncidentError(Exception):
    """Не удалось разобрать нажатие. Текст показывается владельцу."""


def load(incident_id):
    if not INCIDENT_RE.match(incident_id or ""):
        raise IncidentError("Некорректный идентификатор инцидента.")
    path = os.path.join(INCIDENTS_DIR, incident_id, "verdict.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise IncidentError("Инцидент не найден — возможно, он уже устарел и удалён.")
    except (OSError, json.JSONDecodeError) as exc:
        raise IncidentError(f"Не удалось прочитать инцидент: {exc}")


def option(incident_id, index):
    """Вариант по номеру. Действие и цель берутся отсюда, не из нажатия."""
    result = load(incident_id)
    options = result.get("verdict", {}).get("options") or []
    if not 0 <= index < len(options):
        raise IncidentError("Такого варианта в этом инциденте нет.")
    return result, options[index]


def run_remedy(action, target=None):
    """Зовёт root-исполнителя. Он же и решает, позволено ли это."""
    cmd = ["sudo", "-n", REMEDY, action]
    if target:
        cmd.append(target)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=REMEDY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Исполнитель не ответил за {REMEDY_TIMEOUT} с."}
    except OSError as exc:
        return {"ok": False, "output": f"Исполнитель недоступен: {exc}"}

    raw = (proc.stdout or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        detail = raw or (proc.stderr or "").strip() or "(пустой ответ)"
        return {"ok": False, "output": f"Непонятный ответ исполнителя: {detail[:300]}"}


def record_choice(incident_id, entry):
    """Дописывает решение владельца в каталог инцидента.

    Аудит исполнителя фиксирует запуски; здесь фиксируется выбор, включая
    «ничего не делать», — иначе непонятно, отреагировал ли владелец вообще.
    """
    if not INCIDENT_RE.match(incident_id or ""):
        return
    path = os.path.join(INCIDENTS_DIR, incident_id, "choices.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(path, 0o660)   # пишет бот, читает сторож — обоим по группе
    except OSError:
        pass
