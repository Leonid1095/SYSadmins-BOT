#!/usr/bin/env python3
"""Отвечает на вопросы владельца по инцидентам. Работает под plg.

Существует ровно потому, что подписка Claude привязана к `plg`, а бот работает
под `tgbot` и её credentials не видит. Дать боту `sudo -u plg` было бы проще
всего и означало бы root: у `plg` беспарольный sudo на всё. Поэтому стороны
общаются файлами в каталоге инцидента (см. converse.py).

Запускается path-юнитом systemd по появлению `ask.request`, а не по таймеру:
владелец не должен ждать до пяти минут ради ответа на вопрос.

Через эту границу ходит только текст. Ничего исполняемого, никаких команд:
модель по-прежнему заперта в каталоге инцидента с Read/Grep/Glob.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import converse  # noqa: E402

def main():
    answered = converse.serve()
    if answered:
        print(f"обработано вопросов: {answered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
