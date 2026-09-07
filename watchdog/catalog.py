#!/usr/bin/env python3
"""Каталог починок: единственный источник правды о том, что вообще исполнимо.

Модель не сочиняет команды — она называет идентификатор из этого файла. Строку
для запуска собирает здесь наш код. Разница принципиальная: скомпрометированная
модель (или инъекция из логов, которые пишет атакующий) может в худшем случае
предложить не то действие из списка, но не может предложить своё.

Тот же модуль импортирует root-хелпер и проверяет заявку заново, уже не доверяя
ничему из того, что пришло от бота. Поэтому здесь нет ни одной функции, которая
принимала бы готовую команду, — только идентификатор и цель.

Каталог намеренно узкий. Действие попадает сюда, только если оно обратимо и его
последствия предсказуемы. Правки конфигов, удаление данных, что-либо про сеть,
SSH и пользователей сюда не входят и входить не должны.
"""

import re
import subprocess

# Имена, которые вообще имеют право быть целью. Проверяем формой, а не только
# принадлежностью к списку: так мусор отсеивается до любых обращений к системе.
UNIT_RE = re.compile(r"^[A-Za-z0-9@._\-\\]{1,128}\.(service|socket|timer)$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# Юниты, которые нельзя трогать никогда, даже если они упали и даже если
# владелец нажмёт кнопку. Рестарт ssh при сетевой поломке — это потеря доступа
# к машине; остальное здесь такого же рода: чинить это должен человек глазами.
UNIT_DENY = re.compile(
    r"^(ssh|sshd|systemd-.*|dbus.*|networkd?|systemd-networkd|NetworkManager"
    r"|wg-quick@.*|xray-bridge|crowdsec|fail2ban)\.(service|socket)$"
)

CMD_TIMEOUT = 120


class RemedyError(Exception):
    """Заявка отвергнута. Текст уходит владельцу как есть."""


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def failed_units():
    """Юниты, лежащие прямо сейчас. Спрашиваем систему, а не заявителя."""
    out = _run(["systemctl", "list-units", "--state=failed",
                "--no-legend", "--plain", "--no-pager"])
    if out is None:
        return set()
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def broken_containers():
    """Контейнеры не в порядке прямо сейчас."""
    out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if out is None:
        return set()
    broken = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, state, status = parts
        if state != "running" or "unhealthy" in status.lower():
            broken.add(name)
    return broken


# --- Построители команд -----------------------------------------------------
#
# Каждый получает уже проверенную по форме цель и обязан сам убедиться, что
# трогать её сейчас уместно. Живое состояние перепроверяется здесь, а не
# берётся из присланных фактов: между советом модели и нажатием кнопки могли
# пройти часы, и юнит мог подняться сам.

def _restart_unit(target):
    if not target or not UNIT_RE.match(target):
        raise RemedyError(f"Недопустимое имя юнита: {target!r}")
    if UNIT_DENY.match(target):
        raise RemedyError(f"{target} в постоянном запрете: чинить только руками")
    if target not in failed_units():
        raise RemedyError(f"{target} сейчас не в состоянии failed — рестарт не нужен")
    return ["systemctl", "restart", "--", target]


def _restart_container(target):
    if not target or not CONTAINER_RE.match(target):
        raise RemedyError(f"Недопустимое имя контейнера: {target!r}")
    if target not in broken_containers():
        raise RemedyError(f"Контейнер {target} сейчас в порядке — рестарт не нужен")
    return ["docker", "restart", "--", target]


def _prune_builder(target):
    # Кэш сборки — главный пожиратель диска, и он восстановим: следующая сборка
    # просто пойдёт дольше. Ровно это же делает еженедельный root-крон.
    return ["docker", "builder", "prune", "-af", "--filter", "until=72h"]


def _vacuum_journal(target):
    # Неделя журнала остаётся: этого хватает на разбор свежей поломки.
    return ["journalctl", "--vacuum-time=7d"]


ACTIONS = {
    "nothing": {
        "title": "Ничего не делать",
        "purpose": "Принять к сведению. Фиксируется в аудите наравне с остальным.",
        "target": None,
        "build": None,
    },
    "restart_unit": {
        "title": "Перезапустить сервис",
        "purpose": "Поднять упавший systemd-юнит. Только тот, что сейчас в failed.",
        "target": "unit",
        "build": _restart_unit,
    },
    "restart_container": {
        "title": "Перезапустить контейнер",
        "purpose": "Поднять вставший или unhealthy docker-контейнер.",
        "target": "container",
        "build": _restart_container,
    },
    "prune_builder": {
        "title": "Очистить кэш сборки Docker",
        "purpose": "Освободить диск. Кэш восстановим, следующая сборка просто дольше.",
        "target": None,
        "build": _prune_builder,
    },
    "vacuum_journal": {
        "title": "Усечь журнал systemd",
        "purpose": "Освободить диск, сохранив последнюю неделю логов.",
        "target": None,
        "build": _vacuum_journal,
    },
}


def describe_for_model():
    """Каталог в том виде, в каком его видит модель: без команд, только смысл."""
    lines = []
    for action_id, spec in ACTIONS.items():
        target = f", требует поле target ({spec['target']})" if spec["target"] else ""
        lines.append(f"- `{action_id}` — {spec['title']}. {spec['purpose']}{target}")
    return "\n".join(lines)


def resolve(action_id, target=None):
    """Превращает идентификатор и цель в команду. Единственный вход к исполнению."""
    spec = ACTIONS.get(action_id)
    if spec is None:
        raise RemedyError(f"Неизвестное действие: {action_id!r}")
    if spec["build"] is None:
        return None  # «ничего не делать» — исполнять нечего
    if spec["target"] is None and target:
        raise RemedyError(f"Действие {action_id} не принимает цель")
    return spec["build"](target)


def execute(action_id, target=None):
    """Исполняет действие. Вызывается только root-хелпером."""
    cmd = resolve(action_id, target)
    if cmd is None:
        return {"ok": True, "action": action_id, "output": "Действий не предпринято."}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action_id,
                "output": f"Команда не уложилась в {CMD_TIMEOUT} c."}
    output = (r.stdout + r.stderr).strip() or "(команда отработала молча)"
    return {"ok": r.returncode == 0, "action": action_id, "target": target,
            "output": output[:1500]}
