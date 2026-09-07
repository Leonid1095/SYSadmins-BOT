#!/usr/bin/env python3
"""Проверка отсечки посторонних.

Бот управляет серверами и умеет исполнять починки, поэтому «кто угодно может
нажать /start» — не мелкая недоработка, а открытая дверь. Отсечка стоит в
группе -1 и обязана срабатывать раньше любого другого обработчика.

Запуск: ./venv/bin/python watchdog/test_bot_guard.py
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Токен боту для импорта не нужен, но config его ждёт — подставляем пустой.
os.environ.setdefault("TELEGRAM_TOKEN", "0:test")
os.environ.setdefault("OWNER_ID", "1148520376")

import bot  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

OWNER = "1148520376"


def make_update(user_id):
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(effective_user=user)


class OwnerGuardTest(unittest.TestCase):
    def call(self, user_id):
        with mock.patch.object(bot.config, "OWNER_ID", OWNER):
            return asyncio.run(bot.owner_only(make_update(user_id), None))

    def test_владелец_проходит(self):
        self.assertIsNone(self.call(int(OWNER)))

    def test_посторонний_останавливается(self):
        for intruder in (1, 999999999, 1148520377, -1001234567890):
            with self.subTest(intruder=intruder):
                with self.assertRaises(ApplicationHandlerStop):
                    self.call(intruder)

    def test_update_без_пользователя_останавливается(self):
        """Канальные посты и служебные апдейты не несут отправителя —
        пропускать их значило бы оставить лазейку."""
        with self.assertRaises(ApplicationHandlerStop):
            self.call(None)

    def test_id_сравнивается_как_строка_а_не_подстрока(self):
        """11485203760 не должен пройти как «начинается на 1148520376»."""
        with self.assertRaises(ApplicationHandlerStop):
            self.call(11485203760)

    def test_без_owner_id_бот_не_стартует(self):
        """Пустой OWNER_ID означал бы «пускать некого» — падать надо на старте,
        а не пускать всех."""
        with mock.patch.object(bot.config, "OWNER_ID", ""):
            with mock.patch.object(bot.logger, "error") as logged:
                self.assertIsNone(bot.main())
                self.assertTrue(logged.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
