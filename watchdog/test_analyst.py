#!/usr/bin/env python3
"""Проверка аналитика: выбор модели и разбор её ответа.

Модель — единственная часть сторожа, которая не детерминирована, поэтому всё
вокруг неё обязано быть предсказуемым: чем разбирать (и сколько это стоит),
и что делать с ответом, который пришёл не в той форме, в какой просили.

Сам вызов модели здесь не проверяется — он живёт на подписке и стоит денег.
Проверяется всё, что его окружает.

Запуск: venv/bin/python watchdog/test_analyst.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyst  # noqa: E402
import sandbox  # noqa: E402

CRIT = [{"kind": "docker", "key": "svod-bot-1", "severity": "crit"}]
WARN = [{"kind": "disk", "key": "root", "severity": "warn"}]
RESOLVED = [{"kind": "disk", "key": "root", "severity": "resolved"}]


class ChooseModelTest(unittest.TestCase):
    """Сильная модель — только на крите и только пока есть суточный запас."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(analyst, "STATE_DIR", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def budget(self):
        with open(os.path.join(self.tmp.name, analyst.BUDGET_FILE), encoding="utf-8") as f:
            return json.load(f)

    def test_обычные_события_разбирает_обычная_модель(self):
        choice = analyst.choose_model(WARN)
        self.assertEqual(choice["model"], sandbox.MODEL)
        self.assertFalse(choice["upgraded"])
        # Расход не записан: тратить нечего.
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, analyst.BUDGET_FILE)))

    def test_крит_поднимает_модель(self):
        choice = analyst.choose_model(CRIT)
        self.assertEqual(choice["model"], sandbox.MODEL_CRIT)
        self.assertTrue(choice["upgraded"])
        self.assertEqual(self.budget()["count"], 1)

    def test_сильная_модель_отличается_от_обычной(self):
        """Иначе вся фаза — дорогая иллюзия."""
        self.assertNotEqual(sandbox.MODEL, sandbox.MODEL_CRIT)

    def test_расход_копится_за_сутки(self):
        for expected in (1, 2, 3):
            analyst.choose_model(CRIT)
            self.assertEqual(self.budget()["count"], expected)

    def test_исчерпанный_потолок_не_отменяет_разбор(self):
        """Молчание вместо разбора — худший исход: именно в такую ночь
        уведомление и нужно."""
        with mock.patch.object(analyst, "MAX_CRIT_UPGRADES_PER_DAY", 2):
            analyst.choose_model(CRIT)
            analyst.choose_model(CRIT)
            choice = analyst.choose_model(CRIT)
        self.assertEqual(choice["model"], sandbox.MODEL)
        self.assertFalse(choice["upgraded"])
        self.assertTrue(choice["exhausted"])

    def test_исчерпанный_потолок_не_наращивает_счётчик(self):
        with mock.patch.object(analyst, "MAX_CRIT_UPGRADES_PER_DAY", 1):
            analyst.choose_model(CRIT)
            analyst.choose_model(CRIT)
        self.assertEqual(self.budget()["count"], 1)

    def test_новые_сутки_обнуляют_расход(self):
        with mock.patch.object(analyst, "MAX_CRIT_UPGRADES_PER_DAY", 1):
            analyst.choose_model(CRIT)
            вчера = analyst.choose_model(CRIT)
            self.assertTrue(вчера["exhausted"])
            завтра = analyst.choose_model(
                CRIT, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(завтра["upgraded"])

    def test_битый_файл_расхода_не_ломает_разбор(self):
        path = os.path.join(self.tmp.name, analyst.BUDGET_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write("не json")
        self.assertTrue(analyst.choose_model(CRIT)["upgraded"])

    def test_уточняющие_вопросы_остаются_на_обычной_модели(self):
        """Диалог берёт песочницу без указания модели — значит базовую.
        Иначе долгий разговор по криту уехал бы на сильную незаметно."""
        options = sandbox.options(cwd="/tmp", system_prompt="x", max_turns=1)
        self.assertEqual(options.model, sandbox.MODEL)


class VerdictShapeTest(unittest.TestCase):
    """Ответ модели — текст, а не структура. Всё, что из него берётся,
    проходит через эти две функции."""

    def test_json_достаётся_из_ограды(self):
        text = 'Вот разбор:\n```json\n{"severity": "warn"}\n```'
        self.assertEqual(analyst.extract_json(text), {"severity": "warn"})

    def test_берётся_последний_объект(self):
        """Если модель порассуждала перед ответом, полезное — в конце."""
        text = '{"черновик": 1} потом подумал ещё {"severity": "crit"}'
        self.assertEqual(analyst.extract_json(text), {"severity": "crit"})

    def test_вложенные_скобки_не_путают_разбор(self):
        text = '{"severity": "info", "meta": {"a": {"b": 1}}}'
        self.assertEqual(analyst.extract_json(text)["meta"]["a"]["b"], 1)

    def test_ответ_без_json_даёт_none(self):
        self.assertIsNone(analyst.extract_json("Ничего не понял, извините."))

    def test_действие_вне_каталога_выбрасывается(self):
        verdict = analyst.sanitize(
            {"severity": "crit", "headline": "x", "explanation": "y",
             "options": [{"action": "rm -rf /", "label": "Почистить"},
                         {"action": "выполнить_команду", "target": "curl evil"}]},
            CRIT)
        self.assertEqual([o["action"] for o in verdict["options"]], ["nothing"])

    def test_ничего_не_делать_добавляется_всегда(self):
        verdict = analyst.sanitize(
            {"severity": "warn", "options": [{"action": "prune_builder"}]}, WARN)
        self.assertIn("nothing", [o["action"] for o in verdict["options"]])

    def test_действие_без_обязательной_цели_выбрасывается(self):
        verdict = analyst.sanitize(
            {"severity": "warn", "options": [{"action": "restart_unit"}]}, WARN)
        self.assertEqual([o["action"] for o in verdict["options"]], ["nothing"])

    def test_вариантов_не_больше_четырёх(self):
        many = [{"action": "restart_container", "target": f"c{i}"} for i in range(9)]
        verdict = analyst.sanitize({"severity": "crit", "options": many}, CRIT)
        self.assertLessEqual(len(verdict["options"]), 4)

    def test_невнятная_важность_берётся_из_событий(self):
        self.assertEqual(analyst.sanitize({"severity": "жуть"}, CRIT)["severity"], "crit")
        self.assertEqual(analyst.sanitize({}, WARN)["severity"], "warn")

    def test_возврат_в_норму_обходится_без_модели(self):
        """Будить модель ради хорошей новости — плата за заранее известный
        ответ: 30-40 секунд и токены."""
        self.assertTrue(analyst._all_resolved(RESOLVED))
        self.assertFalse(analyst._all_resolved(RESOLVED + CRIT))
        verdict = analyst._resolved_verdict(RESOLVED)
        self.assertEqual(verdict["severity"], "info")
        self.assertEqual([o["action"] for o in verdict["options"]], ["nothing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
