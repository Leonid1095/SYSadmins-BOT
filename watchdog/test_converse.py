#!/usr/bin/env python3
"""Тесты диалога по инциденту и тишины сторожа.

Закрывают жалобы владельца от 08.09.2026: постоянный спам, машинный текст и
невозможность ответить боту.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(BASE))

import converse   # noqa: E402
import delta      # noqa: E402
import notify     # noqa: E402
import sandbox    # noqa: E402


class SandboxStaysReadOnly(unittest.TestCase):
    """Песочница одна на разбор и на диалог — разъехаться они не должны."""

    def test_no_write_tools_allowed(self):
        for tool in ("Bash", "Write", "Edit", "WebFetch", "Task"):
            self.assertNotIn(tool, sandbox.ALLOWED_TOOLS)
            self.assertIn(tool, sandbox.DISALLOWED_TOOLS)

    def test_only_reading_allowed(self):
        self.assertEqual(set(sandbox.ALLOWED_TOOLS), {"Read", "Grep", "Glob"})

    def test_analyst_and_converse_share_the_sandbox(self):
        """Обе половины обязаны брать настройки отсюда, а не заводить свои."""
        for name in ("analyst.py", "converse.py"):
            with open(os.path.join(BASE, name), encoding="utf-8") as f:
                body = f.read()
            with self.subTest(module=name):
                self.assertIn("sandbox.options", body)
                self.assertNotIn("ClaudeAgentOptions(", body)


class DialogueBudget(unittest.TestCase):
    """Подписка общая с работой владельца — переписка не должна её выедать."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.incident = os.path.join(self.tmp.name, "incidents", "20260908T120000Z")
        os.makedirs(self.incident)

    def tearDown(self):
        self.tmp.cleanup()

    def _fill_history(self, count):
        with open(os.path.join(self.incident, "dialogue.jsonl"), "w", encoding="utf-8") as f:
            for i in range(count):
                f.write(json.dumps({"q": f"в{i}", "a": "о"}) + "\n")

    def test_per_incident_limit_enforced(self):
        self._fill_history(converse.MAX_PER_INCIDENT)
        with self.assertRaises(converse.AskError) as ctx:
            converse.ask(self.incident, "ещё вопрос")
        self.assertIn("предел", str(ctx.exception).lower())

    def test_empty_question_rejected(self):
        with self.assertRaises(converse.AskError):
            converse.ask(self.incident, "   ")

    def test_overlong_question_rejected(self):
        with self.assertRaises(converse.AskError) as ctx:
            converse.ask(self.incident, "я" * (converse.MAX_QUESTION_LEN + 1))
        self.assertIn("сократите", str(ctx.exception).lower())

    def test_missing_incident_explained_not_crashed(self):
        with self.assertRaises(converse.AskError) as ctx:
            converse.ask(os.path.join(self.tmp.name, "нет-такого"), "почему?")
        self.assertIn("не найден", str(ctx.exception).lower())

    def test_daily_quota_blocks_and_explains(self):
        path = converse._day_counter_path(self.incident)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"day": today, today: converse.MAX_PER_DAY}, f)
        with self.assertRaises(converse.AskError) as ctx:
            converse.ask(self.incident, "почему?")
        self.assertIn("лимит", str(ctx.exception).lower())

    def test_lock_prevents_two_at_once(self):
        with converse._Lock(self.incident):
            with self.assertRaises(converse.AskError) as ctx:
                with converse._Lock(self.incident):
                    pass
        self.assertIn("обрабатывается", str(ctx.exception).lower())

    def test_answer_is_recorded_for_next_question(self):
        # Подменяем только запуск цикла: сама корутина _run не создаётся, иначе
        # остаётся неожиданная предупреждающая корутина в выводе тестов.
        with patch.object(converse, "_run", lambda *a, **kw: None), \
             patch.object(converse.asyncio, "run", lambda coro: "потому что"):
            answer = converse.ask(self.incident, "почему упало?")
        self.assertEqual(answer, "потому что")
        past = converse.history(self.incident)
        self.assertEqual(len(past), 1)
        self.assertEqual(past[0]["q"], "почему упало?")


class ExchangeCrossesThePrivilegeBoundary(unittest.TestCase):
    """Бот (tgbot) и служба (plg) общаются файлами.

    Прямой вызов модели из бота невозможен: credentials подписки принадлежат
    plg. Дать боту `sudo -u plg` нельзя — у plg беспарольный sudo, это то же
    самое, что дать root.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ask_dir = os.path.join(self.tmp.name, "ask")
        self.incidents = os.path.join(self.tmp.name, "incidents")
        self.incident = os.path.join(self.incidents, "20260908T120000Z")
        os.makedirs(self.incident)
        os.makedirs(self.ask_dir)
        self.patches = [
            patch.object(converse, "ASK_DIR", self.ask_dir),
            patch.object(converse, "INCIDENTS_DIR", self.incidents),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_round_trip(self):
        token = converse.submit(self.incident, "почему упало?")
        self.assertIsNone(converse.collect(token), "ответа быть ещё не должно")

        with patch.object(converse, "ask", return_value="потому что память"):
            self.assertEqual(converse.serve(), 1)

        self.assertEqual(converse.collect(token), "потому что память")

    def test_error_travels_back_to_the_owner(self):
        token = converse.submit(self.incident, "почему?")
        with patch.object(converse, "ask", side_effect=converse.AskError("лимит")):
            converse.serve()
        with self.assertRaises(converse.AskError):
            converse.collect(token)

    def test_service_survives_unexpected_failure(self):
        token = converse.submit(self.incident, "почему?")
        with patch.object(converse, "ask", side_effect=RuntimeError("бум")):
            self.assertEqual(converse.serve(), 1, "служба не должна падать целиком")
        with self.assertRaises(converse.AskError):
            converse.collect(token)

    def test_request_is_consumed_so_it_cannot_loop(self):
        converse.submit(self.incident, "почему?")
        with patch.object(converse, "ask", return_value="ответ"):
            converse.serve()
            self.assertEqual(converse.serve(), 0, "заявка обработана дважды")

    def test_foreign_token_gets_nothing(self):
        converse.submit(self.incident, "почему?")
        with patch.object(converse, "ask", return_value="ответ"):
            converse.serve()
        self.assertIsNone(converse.collect("0" * 16))
        self.assertIsNone(converse.collect("../../etc/passwd"))

    def test_incident_id_from_request_is_validated(self):
        """Заявку пишет бот, но служба не доверяет ей на слово."""
        for bad in ("../../etc", "не-инцидент", "", None):
            with self.subTest(incident=bad):
                with self.assertRaises(converse.AskError):
                    converse._incident_dir(bad)

    def test_stale_response_removed(self):
        token = converse.submit(self.incident, "почему?")
        with patch.object(converse, "ask", return_value="ответ"):
            converse.serve()
        path = os.path.join(self.ask_dir, f"{token}.response")
        os.utime(path, (0, 0))          # как будто ответ очень старый
        converse._prune_responses()
        self.assertFalse(os.path.exists(path))


class SwapNoLongerCriesWolf(unittest.TestCase):
    """Своп при свободной памяти — не событие. Это и был источник дребезга."""

    def test_swap_high_but_ram_free_is_silent(self):
        b = delta.DeltaBuilder({"memory.memory.swap_pct": "ok"})
        b.swap({"memory": {"swap_pct": 85, "used_pct": 34}})
        self.assertEqual(b.events, [])

    def test_swap_high_with_ram_pressure_reports(self):
        b = delta.DeltaBuilder({"memory.memory.swap_pct": "ok"})
        b.swap({"memory": {"swap_pct": 92, "used_pct": 88}})
        self.assertEqual(len(b.events), 1)
        self.assertEqual(b.events[0]["severity"], "warn")

    def test_swap_returns_to_ok_when_ram_frees_up(self):
        b = delta.DeltaBuilder({"memory.memory.swap_pct": "warn"})
        b.swap({"memory": {"swap_pct": 85, "used_pct": 20}})
        self.assertEqual(b.events[0]["severity"], "resolved")

    def test_detail_is_human_readable(self):
        b = delta.DeltaBuilder({"memory.memory.swap_pct": "ok"})
        b.swap({"memory": {"swap_pct": 92, "used_pct": 88}})
        detail = b.events[0]["detail"]
        self.assertIn("подкачка", detail)
        self.assertNotIn("swap_pct=", detail)


class SelfStoppingRoomsAreNotIncidents(unittest.TestCase):
    """Комнаты civ4col гасятся сами. Перечислять их поимённо значит однажды
    забыть — что и вышло: 4, 5 и 6 завелись позже списка."""

    def test_pattern_covers_future_rooms(self):
        patterns = delta.ignored()["docker"]
        rooms = {"civ4col-pitboss", "civ4col-pitboss2", "civ4col-pitboss7",
                 "civ4col-pitboss99"}
        self.assertEqual(delta._drop_ignored(rooms, patterns), set())

    def test_real_containers_survive_the_filter(self):
        patterns = delta.ignored()["docker"]
        real = {"plg-voice-api-1", "svod-web-1", "plgames-ai-api"}
        self.assertEqual(delta._drop_ignored(real, patterns), real)

    def test_defaults_exist_without_local_conf(self):
        """Раньше без monitor.local.conf исключений не было вовсе."""
        with patch.object(delta, "LOCAL_CONF", "/nonexistent/monitor.local.conf"):
            self.assertIn("civ4col-pitboss*", delta.ignored()["docker"])
            self.assertIn("fwupd.service", delta.ignored()["systemd"])


class MetricsSpeakRussian(unittest.TestCase):
    """«swap_pct=80» выглядело отладочным выводом, а не сообщением человеку."""

    def test_describe_covers_every_banded_metric(self):
        for (section, field) in delta.RISING:
            with self.subTest(field=field):
                text = delta.describe(field, section, 42)
                self.assertNotIn(f"{field}=", text,
                                 f"метрика {field} осталась без человеческой подписи")

    def test_event_line_has_no_internal_key_duplication(self):
        line = notify.describe_event({
            "kind": "memory", "key": "memory", "from": "ok", "to": "warn",
            "severity": "warn", "detail": "оперативной памяти занято 93%"})
        self.assertNotIn("<code>memory</code>", line)
        self.assertIn("Память", line)

    def test_event_line_names_direction(self):
        worse = notify.describe_event({"kind": "docker", "key": "web", "to": "crit",
                                       "severity": "crit", "detail": "unhealthy"})
        better = notify.describe_event({"kind": "docker", "key": "web", "to": "ok",
                                        "severity": "resolved", "detail": "работает"})
        self.assertIn("стало плохо", worse)
        self.assertIn("вернулось в норму", better)

    def test_all_event_kinds_have_russian_names(self):
        for kind in ("disk", "memory", "cpu", "temperature", "systemd",
                     "docker", "http", "cert", "smart", "remote"):
            with self.subTest(kind=kind):
                self.assertIn(kind, notify.KIND_NAME)


class GoodNewsCostsNothing(unittest.TestCase):
    """Будить модель ради «проблемы больше нет» — плата за известный ответ."""

    def setUp(self):
        import analyst
        self.analyst = analyst

    def test_all_resolved_detected(self):
        self.assertTrue(self.analyst._all_resolved(
            [{"severity": "resolved"}, {"severity": "resolved"}]))
        self.assertFalse(self.analyst._all_resolved(
            [{"severity": "resolved"}, {"severity": "warn"}]))
        self.assertFalse(self.analyst._all_resolved([]))

    def test_resolved_verdict_needs_no_model(self):
        v = self.analyst._resolved_verdict([{"kind": "http", "key": "site.ru"}])
        self.assertEqual(v["severity"], "info")
        self.assertEqual([o["action"] for o in v["options"]], ["nothing"])
        self.assertIn("site.ru", v["explanation"])

    def test_strong_model_only_on_crit(self):
        """Выбор модели целиком проверяется в test_analyst.py; здесь остаётся
        одно утверждение — что без крита подниматься не на что."""
        import sandbox
        self.assertEqual(self.analyst.choose_model([{"severity": "warn"}])["model"],
                         sandbox.MODEL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
