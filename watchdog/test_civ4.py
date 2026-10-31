#!/usr/bin/env python3
"""Рассинхрон в комнатах Civilization — сбор факта, полоса, улики.

Запуск: venv/bin/python watchdog/test_civ4.py
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect   # noqa: E402
import deepen    # noqa: E402
import delta     # noqa: E402
import notify    # noqa: E402


def _room(tmp, name="game4", net=None):
    log = os.path.join(tmp, name, "log")
    os.makedirs(log, exist_ok=True)
    if net is not None:
        with open(os.path.join(log, "net.json"), "w", encoding="utf-8") as f:
            json.dump(net, f)
    return log


class Civ4CollectTest(unittest.TestCase):
    """Сборщик берёт уже разобранный факт из файла агента комнат."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_читает_эпизод_и_имя_игрока(self):
        _room(self.tmp.name, net={
            "people": [{"netid": 7, "name": "Альфа"}],
            "oos": [{"netid": 7, "since": 1, "dumps": 4, "sec": 240}],
        })
        with mock.patch.object(collect, "CIV4_ROOMS_DIR", self.tmp.name):
            facts = collect.Collector().civ4()
        self.assertEqual(facts["game4"]["episodes"][0]["name"], "Альфа")
        self.assertEqual(facts["game4"]["episodes"][0]["sec"], 240)

    def test_нет_имени_подставляем_заглушку(self):
        _room(self.tmp.name, net={"people": [], "oos": [{"netid": 9, "sec": 30}]})
        with mock.patch.object(collect, "CIV4_ROOMS_DIR", self.tmp.name):
            facts = collect.Collector().civ4()
        self.assertIn("игрок 9", facts["game4"]["episodes"][0]["name"])

    def test_битый_файл_не_роняет_сборщик(self):
        log = _room(self.tmp.name)
        with open(os.path.join(log, "net.json"), "w", encoding="utf-8") as f:
            f.write("{не json")
        with mock.patch.object(collect, "CIV4_ROOMS_DIR", self.tmp.name):
            facts = collect.Collector().civ4()
        self.assertEqual(facts, {})

    def test_нет_каталога_не_ошибка(self):
        with mock.patch.object(collect, "CIV4_ROOMS_DIR", "/nonexistent/путь"):
            self.assertIsNone(collect.Collector().civ4())


class Civ4DeltaTest(unittest.TestCase):
    """Рассинхрон — состояние, а не событие: полосы по времени."""

    def _builder(self, bands=None):
        return delta.DeltaBuilder(bands or {})

    def test_длинный_эпизод_это_крит(self):
        b = self._builder()
        b.civ4({"civ4": {"game4": {"episodes": [
            {"netid": 7, "name": "Альфа", "sec": delta.OOS_CRIT_SEC}]}}})
        self.assertEqual(b.events[0]["severity"], "crit")
        self.assertEqual(b.events[0]["kind"], "civ4")
        self.assertIn("Альфа", b.events[0]["detail"])

    def test_короткий_эпизод_это_предупреждение(self):
        b = self._builder()
        b.civ4({"civ4": {"game4": {"episodes": [
            {"netid": 7, "name": "Альфа", "sec": delta.OOS_WARN_SEC - 1}]}}})
        self.assertEqual(b.events[0]["severity"], "warn")

    def test_сошёл_сам_это_снятие_тревоги(self):
        b = self._builder({"civ4.game4": "crit"})
        b.civ4({"civ4": {"game4": {"episodes": []}}})
        self.assertEqual(b.events[0]["severity"], "resolved")

    def test_комната_исчезла_это_снятие_тревоги(self):
        """Молчать о исчезнувшей комнате нельзя: полоса навсегда осталась бы красной."""
        b = self._builder({"civ4.game4": "crit"})
        b.civ4({"civ4": {}})
        self.assertEqual(b.events[0]["severity"], "resolved")

    def test_без_изменений_событий_нет(self):
        b = self._builder({"civ4.game4": "crit"})
        b.civ4({"civ4": {"game4": {"episodes": [
            {"netid": 7, "name": "Альфа", "sec": delta.OOS_CRIT_SEC}]}}})
        self.assertEqual(b.events, [])

    def test_без_раздела_ничего_не_трогаем(self):
        """Раздела нет — значит, сборщик не смог; ломать полосы нельзя."""
        b = self._builder({"civ4.game4": "crit"})
        b.civ4({})
        self.assertEqual(b.events, [])
        self.assertEqual(b.bands, {})


class Civ4NotifyTest(unittest.TestCase):
    """Новый вид события должен быть назван по-человечески."""

    def test_у_вида_есть_человеческое_имя(self):
        self.assertIn("civ4", notify.KIND_NAME)
        self.assertTrue(notify.KIND_NAME["civ4"][0].isupper()
                        or notify.KIND_NAME["civ4"][0].isalpha())


class Civ4ContextTest(unittest.TestCase):
    """Улики: то, что агент комнат уже собрал, и ничего больше."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_собирает_факт_и_живость_комнаты(self):
        log = _room(self.tmp.name)
        with open(os.path.join(log, "net.json"), "w", encoding="utf-8") as f:
            json.dump({"oos": [], "people": []}, f)
        with open(os.path.join(log, "live.json"), "w", encoding="utf-8") as f:
            json.dump({"up": True, "delta": 0}, f)
        with mock.patch.object(deepen, "CIV4_ROOMS_DIR", self.tmp.name):
            files = deepen._civ4_context("game4")
        self.assertIn("civ4-game4-net.json", files)
        self.assertIn("civ4-game4-live.json", files)

    def test_подтягивает_журнал_движка(self):
        log = _room(self.tmp.name)
        engine = os.path.join(log, "engine-20261028-201855")
        os.makedirs(engine)
        with open(os.path.join(engine, "network-1.txt"), "w", encoding="utf-8") as f:
            f.write("*** Player 1 (SyncTurn 12): Sync=-42 ***\n")
        with mock.patch.object(deepen, "CIV4_ROOMS_DIR", self.tmp.name):
            files = deepen._civ4_context("game4")
        self.assertTrue(any("network-1.txt" in name for name in files))
        body = "".join(files.values())
        self.assertIn("Sync=-42", body)

    def test_чужая_комната_не_читается(self):
        with mock.patch.object(deepen, "CIV4_ROOMS_DIR", self.tmp.name):
            self.assertIsNone(deepen._civ4_context("../etc"))

    def test_путь_в_имя_файла_не_просачивается(self):
        """Имя файла контекста идёт в каталог инцидента — путь там быть не должен."""
        log = _room(self.tmp.name)
        with open(os.path.join(log, "net.json"), "w", encoding="utf-8") as f:
            json.dump({"oos": []}, f)
        with mock.patch.object(deepen, "CIV4_ROOMS_DIR", self.tmp.name):
            files = deepen._civ4_context("game4")
        for name in files:
            self.assertNotIn("/", name)
            self.assertNotIn("..", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
