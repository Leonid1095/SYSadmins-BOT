#!/usr/bin/env python3
"""Добор контекста под конкретную поломку — детерминированно, без модели.

Это ответ на вопрос «как дать аналитику докопаться, не выдавая ему Bash».
Решение: копать заранее и по правилам. Упал юнит — кладём рядом его последние
строки журнала; встал контейнер — его логи и вердикт healthcheck; кончается
диск — раскладку, куда он делся. Что именно читать, решает вот этот код, а не
модель, начитавшаяся содержимого логов.

Всё собранное складывается в каталог инцидента файлами. Аналитику остаётся
Read по этому каталогу — инструмент, которым нельзя ничего сделать.

Содержимое логов недоверенное: в nginx и fail2ban строки пишет тот, кто ломится
снаружи. Здесь мы их не интерпретируем, только сохраняем, а аналитик предупреждён
в системном промпте, что это данные, а не указания.
"""

import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import PLGAMESBOT_DB  # noqa: E402 — путь к базе определён один раз

CMD_TIMEOUT = 20
MAX_BYTES = 24_000   # хватает на разбор, но не топит контекст аналитика

UNIT_RE = re.compile(r"^[A-Za-z0-9@._\-\\]{1,128}\.(service|socket|timer)$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _capture(cmd, timeout=CMD_TIMEOUT):
    """Возвращает вывод команды вместе с диагностикой сбоя: аналитику полезно
    знать, что данных нет именно потому, что команда не отработала."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(не уложилось в {timeout} c: {' '.join(cmd)})"
    except OSError as exc:
        return f"(не удалось запустить {cmd[0]}: {exc})"
    text = (r.stdout or "") + (r.stderr or "")
    return text.strip()[-MAX_BYTES:] or "(пусто)"


# --- Сборщики по типам событий ---------------------------------------------

def _unit_context(name):
    if not UNIT_RE.match(name):
        return None
    return {
        f"unit-{name}-status.txt": _capture(
            ["systemctl", "status", "--no-pager", "--lines=0", "--", name]),
        f"unit-{name}-journal.txt": _capture(
            ["journalctl", "-u", name, "-n", "80", "--no-pager", "--output=short-iso"]),
    }


def _container_context(name):
    if not CONTAINER_RE.match(name):
        return None
    return {
        f"container-{name}-logs.txt": _capture(
            ["docker", "logs", "--tail", "80", "--timestamps", "--", name]),
        f"container-{name}-inspect.txt": _capture(
            ["docker", "inspect", "--format",
             "State={{.State.Status}} ExitCode={{.State.ExitCode}} "
             "OOM={{.State.OOMKilled}} Error={{.State.Error}} "
             "RestartCount={{.RestartCount}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}нет{{end}}",
             "--", name]),
    }


def _disk_context(_key):
    """Куда делся диск. Три обычных виновника: образы Docker, журнал, логи."""
    return {
        "disk-usage.txt": _capture(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"]),
        "disk-docker.txt": _capture(["docker", "system", "df"]),
        "disk-journal.txt": _capture(["journalctl", "--disk-usage"]),
        "disk-largest.txt": _capture(
            ["du", "-xh", "--max-depth=2", "--threshold=1G", "/var", "/home", "/opt"],
            timeout=60),
    }


def _memory_context(_key):
    return {
        "memory-summary.txt": _capture(["free", "-h"]),
        "memory-top.txt": _capture(
            ["ps", "-eo", "pid,user,rss,pcpu,comm", "--sort=-rss", "--no-headers"]),
    }


def _cpu_context(_key):
    return {
        "cpu-top.txt": _capture(
            ["ps", "-eo", "pid,user,pcpu,rss,comm", "--sort=-pcpu", "--no-headers"]),
        "cpu-loadavg.txt": _capture(["cat", "/proc/loadavg"]),
    }


def _http_context(url):
    """Веб-эндпоинт отвалился — смотрим, что говорит фронт-прокси."""
    ctx = {"http-nginx-error.txt": _capture(
        ["tail", "-n", "60", "/var/log/nginx/error.log"])}
    host = re.sub(r"^https?://", "", url).split("/")[0]
    if re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        ctx[f"http-{host}-nginx-conf.txt"] = _capture(
            ["grep", "-rl", "--", host, "/etc/nginx/sites-enabled/"])
    return ctx


def _smart_context(dev):
    if not re.fullmatch(r"/dev/[a-z0-9]{1,16}", dev):
        return None
    cmd = ["/usr/sbin/smartctl", "-a", dev]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    return {f"smart-{os.path.basename(dev)}.txt": _capture(cmd)}


def _remote_context(name):
    """Что можно узнать об удалённом сервере, не имея на нём доступа.

    Немного: жив ли порт и что отвечает агент. Логи оттуда не достать — по
    этому поводу аналитик и должен советовать смотреть руками.
    """
    return {
        f"remote-{name}-note.txt": (
            "Сервер наблюдается только через агента, SSH-доступа у сторожа нет.\n"
            "Подробности о состоянии смотри в facts.json → remote.\n"
            "Если агент молчит: проверить, что bot-agent.service поднят, что порт\n"
            "5000 открыт для этого сервера и что версия агента умеет проверять\n"
            "подпись (старые ждут секрет в заголовке и отвечают 403/503)."
        )
    }


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _plgamesbot_context(name):
    """Что известно про канал, где наш бот замолчал.

    В каталоге действий сторожа нет и не может быть кнопки «попросить стримера
    снять бан»: это чужой чат, туда нужны его руки. Поэтому полезный разбор
    отвечает на два вопроса — что именно должен набрать стример и есть ли у нас
    способ ему это сказать. Второй важнее: у большинства каналов Telegram не
    привязан, и знание о бане остаётся нашим личным.
    """
    if not CHANNEL_RE.match(name or "") or not os.path.exists(PLGAMESBOT_DB):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % PLGAMESBOT_DB, uri=True, timeout=CMD_TIMEOUT)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(streamers)")}
        tg = "s.telegram_id" if "telegram_id" in cols else "NULL"
        row = con.execute(
            "SELECT s.twitch_username, s.is_stream_live, %s, "
            "       b.state, b.detail, b.is_mod, b.heartbeat_at, b.last_cmd, b.last_cmd_at "
            "  FROM streamers s LEFT JOIN bot_status b ON b.streamer_id = s.twitch_id "
            " WHERE s.twitch_username = ?" % tg, (name,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None

    username, live, telegram, state, detail, is_mod, heartbeat, last_cmd, last_cmd_at = row
    reach = ("Telegram привязан — можно написать в личку"
             if telegram else
             "Telegram не привязан — написать некому, остаётся объявление в кабинете")
    lines = [
        f"канал: {username}",
        f"сейчас в эфире: {'да' if live else 'нет'}",
        f"состояние бота: {state or 'нет записи'}",
        f"подробность: {detail or '—'}",
        f"права модератора: {'есть' if is_mod else 'НЕТ'}",
        f"последняя отметка бота: {heartbeat or '—'}",
        f"последняя отвеченная команда: {last_cmd or '—'} ({last_cmd_at or '—'})",
        f"связь со стримером: {reach}",
        "",
        "Чинится не с нашей стороны: бан снимает сам стример командой "
        "/unban plgames_bot, права модератора даёт /mod plgames_bot. "
        "Действий каталога для этого нет и быть не может.",
    ]
    return {f"plgamesbot-{username}.txt": "\n".join(lines)}


GATHERERS = {
    "systemd": _unit_context,
    "docker": _container_context,
    "disk": _disk_context,
    "memory": _memory_context,
    "cpu": _cpu_context,
    "http": _http_context,
    "smart": _smart_context,
    "remote": _remote_context,
    "plgamesbot": _plgamesbot_context,
    "plgamesbot_public": _plgamesbot_context,
}


def deepen(events, context_dir):
    """Собирает контекст под список событий. Возвращает имена созданных файлов.

    Каждый сборщик вызывается не более раза на цель: три события об одном
    контейнере не должны трижды тянуть его логи.
    """
    os.makedirs(context_dir, mode=0o770, exist_ok=True)
    os.chmod(context_dir, 0o2770)   # makedirs режется umask, chmod — нет
    written, seen = [], set()

    for event in events:
        kind, key = event.get("kind"), event.get("key", "")
        gatherer = GATHERERS.get(kind)
        if gatherer is None or (kind, key) in seen:
            continue
        seen.add((kind, key))

        try:
            files = gatherer(key)
        except Exception as exc:
            files = {f"{kind}-{key}-error.txt": f"сбор контекста не удался: {exc}"}
        if not files:
            continue

        for name, body in files.items():
            safe = re.sub(r"[^A-Za-z0-9._\-]", "_", name)[:120]
            path = os.path.join(context_dir, safe)
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(body)
            os.chmod(path, 0o640)
            written.append(safe)

    return sorted(written)
