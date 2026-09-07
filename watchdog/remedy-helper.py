#!/usr/bin/env python3
"""Исполнитель починок. Единственное, что сторож умеет запускать от root.

Вызывается ботом через sudo и не доверяет вызывающему ни в чём: имя действия
сверяется с каталогом, цель — с формой и с живым состоянием системы, команда
собирается здесь заново. Всё, что пришло сверх идентификатора и цели, игнорируется.

Каталог берётся из /usr/local/lib/watchdog, а не из домашнего каталога проекта:
менять то, что исполняется от root, — это шаг установки, а не правка файла.

Каждый вызов, включая отвергнутый, попадает в журнал. Аудит нужен именно для
отказов: попытка провести мимо каталога что-то своё должна оставлять след.

Использование:  watchdog-remedy <действие> [цель]
"""

import json
import os
import pwd
import sys
from datetime import datetime, timezone

LIB_DIR = "/usr/local/lib/watchdog"
AUDIT_LOG = "/var/log/watchdog-remedy.log"

sys.path.insert(0, LIB_DIR)
try:
    import catalog
except ImportError:
    print(json.dumps({"ok": False, "output": f"Каталог действий не установлен в {LIB_DIR}"},
                     ensure_ascii=False))
    sys.exit(2)


def audit(entry):
    """Пишет строку аудита. Сбой записи не должен прятать результат действия,
    но и молчать о нём нельзя — уходит в stderr."""
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        caller = os.environ.get("SUDO_UID")
        entry["caller"] = pwd.getpwuid(int(caller)).pw_name if caller else f"uid={os.getuid()}"
    except (KeyError, ValueError, TypeError):
        entry["caller"] = "неизвестен"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(AUDIT_LOG, 0o600)
    except OSError as exc:
        print(f"аудит не записан: {exc}", file=sys.stderr)


def main(argv):
    if not 1 <= len(argv) <= 2:
        print(json.dumps({"ok": False, "output": "Ожидается: <действие> [цель]"},
                         ensure_ascii=False))
        return 2

    action = argv[0]
    target = argv[1] if len(argv) == 2 else None

    try:
        result = catalog.execute(action, target)
    except catalog.RemedyError as exc:
        # Отказ — штатный исход, а не авария: так каталог и работает.
        result = {"ok": False, "action": action, "target": target,
                  "output": f"Отклонено: {exc}", "rejected": True}
    except Exception as exc:
        result = {"ok": False, "action": action, "target": target,
                  "output": f"Сбой исполнителя: {type(exc).__name__}: {exc}"}

    audit(dict(result))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
