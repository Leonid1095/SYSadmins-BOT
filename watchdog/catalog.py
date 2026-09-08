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

import fnmatch
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

# Контейнеры, чья остановка — норма. Комнаты civ4col гасятся сами через
# полчаса; «поднять» такую значит спорить с её же устройством.
#
# Список берётся из root-овского файла, а не из monitor.local.conf в домашнем
# каталоге: этот модуль исполняется от root, и политику ему должен задавать
# файл, который правится шагом установки. install.sh переносит туда значение
# из monitor.local.conf, так что источник правды остаётся один.
REMEDY_CONF = "/etc/watchdog-remedy.conf"
DEFAULT_DOCKER_IGNORE = ("civ4col-pitboss*",)


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


def ignored_containers():
    """Шаблоны имён, которые каталог не трогает."""
    try:
        with open(REMEDY_CONF, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return list(DEFAULT_DOCKER_IGNORE)
    match = re.search(r'^\s*DOCKER_IGNORE="([^"]*)"', text, re.MULTILINE)
    return match.group(1).split() if match else list(DEFAULT_DOCKER_IGNORE)


def _container_rows():
    """Имя → (состояние, статус) для всех контейнеров, включая остановленные."""
    out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    rows = {}
    if out is None:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def broken_containers():
    """Контейнеры не в порядке прямо сейчас."""
    return {name for name, (state, status) in _container_rows().items()
            if state != "running" or "unhealthy" in status.lower()}


# Контейнер, вышедший с нулевым кодом, сделал свою работу и закончил — так
# живут разовые задачи. Поднимать такой значит выполнить её ещё раз, и это не
# «обратимо с предсказуемыми последствиями»: на этой машине под описание
# попадает svod-migrate-1, миграция базы. Найдено на живом инвентаре до
# первого нажатия кнопки.
CLEAN_EXIT_RE = re.compile(r"^Exited \(0\)")


def stopped_containers():
    """Контейнеры, которые именно стоят и которые имеет смысл поднимать.

    Отдельно от broken_containers: перезапускать больной и поднимать стоящий —
    разные действия, и путать их нельзя. Контейнер в restarting уже пытается
    подняться сам, а вышедший с кодом 0 — не сломался, а закончил.
    """
    return {name for name, (state, status) in _container_rows().items()
            if state in ("exited", "created", "dead")
            and not CLEAN_EXIT_RE.match(status.strip())}


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


def _start_container(target):
    # Именно start, а НЕ compose up: последний умеет тянуть образы и
    # пересоздавать контейнер, то есть менять то, что запущено. Это другой
    # класс полномочий, и в каталоге ему не место.
    if not target or not CONTAINER_RE.match(target):
        raise RemedyError(f"Недопустимое имя контейнера: {target!r}")
    if any(fnmatch.fnmatch(target, p) for p in ignored_containers()):
        raise RemedyError(f"Контейнер {target} останавливается штатно — поднимать не нужно")
    if target not in stopped_containers():
        # Отказ обязан говорить правду о причине. «Не остановлен» про
        # завершившуюся миграцию — это неверный диагноз, а неверный диагноз
        # владелец принимает за факт: ровно так сегодня уже вышло с ключом
        # агента, где ответ моста выдавался за ответ агента.
        state, status = _container_rows().get(target, (None, ""))
        if state is None:
            raise RemedyError(f"Контейнера {target} на машине нет")
        if CLEAN_EXIT_RE.match(status.strip()):
            raise RemedyError(f"Контейнер {target} завершился сам с кодом 0 — "
                              f"это разовая задача, а не поломка")
        raise RemedyError(f"Контейнер {target} сейчас работает — поднимать нечего")
    return ["docker", "start", "--", target]


def _reset_failed(target):
    # Чистая бухгалтерия: снимаем отметку об аварии, после чего юнит снова
    # может подняться по своей же политике перезапуска. Ничего не запускаем.
    if not target or not UNIT_RE.match(target):
        raise RemedyError(f"Недопустимое имя юнита: {target!r}")
    if UNIT_DENY.match(target):
        raise RemedyError(f"{target} в постоянном запрете: чинить только руками")
    if target not in failed_units():
        raise RemedyError(f"{target} сейчас не в состоянии failed — снимать нечего")
    return ["systemctl", "reset-failed", "--", target]


def _rotate_logs(target):
    # Взято вместо «усечь лог»: у того целью был бы путь, названный моделью, а
    # ротация — штатный механизм, и ничего не теряет.
    return ["logrotate", "-f", "/etc/logrotate.conf"]


def _renew_certs(target):
    # Идемпотентно: без готовых к продлению сертификатов ничего не делает.
    # Закрывает реальный случай — остаток упал ниже 30 дней, значит
    # автопродление сломалось и его надо подтолкнуть руками.
    return ["certbot", "renew"]


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
    "start_container": {
        "title": "Поднять контейнер",
        "purpose": "Запустить остановленный контейнер как есть, без пересборки "
                   "и без обновления образа. Только тот, что сейчас стоит.",
        "target": "container",
        "build": _start_container,
    },
    "reset_failed": {
        "title": "Снять отметку об аварии",
        "purpose": "Дать юниту, помеченному как failed, снова подниматься по "
                   "своей политике перезапуска. Сам ничего не запускает.",
        "target": "unit",
        "build": _reset_failed,
    },
    "rotate_logs": {
        "title": "Провернуть ротацию логов",
        "purpose": "Освободить диск штатным механизмом, ничего не удаляя сверх "
                   "того, что уже разрешено настройками.",
        "target": None,
        "build": _rotate_logs,
    },
    "renew_certs": {
        "title": "Продлить сертификаты",
        "purpose": "Подтолкнуть продление сертификатов, если автоматическое "
                   "сломалось. Ничего не делает, когда продлевать нечего.",
        "target": None,
        "build": _renew_certs,
        # Продление ходит к внешнему сервису и по каждому домену отдельно,
        # поэтому общего потолка ему мало.
        "timeout": 240,
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
    timeout = ACTIONS[action_id].get("timeout", CMD_TIMEOUT)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action_id,
                "output": f"Команда не уложилась в {timeout} c."}
    output = (r.stdout + r.stderr).strip() or "(команда отработала молча)"
    return {"ok": r.returncode == 0, "action": action_id, "target": target,
            "output": output[:1500]}
