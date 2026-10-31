#!/usr/bin/env python3
"""Проверка сменного ИИ-слоя сторожа.

Смысл слоя — владелец меняет модель без правки кода. Значит, проверять надо
не «какая модель сейчас», а «правила выбора и отката». Модель не вызывается:
это стоит денег, и проверяется всё вокруг неё.

Запуск: venv/bin/python watchdog/test_ai.py
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai          # noqa: E402
import ai_config   # noqa: E402
import sandbox     # noqa: E402


def _write_config(tmp, data):
    path = os.path.join(tmp, "ai.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


class HealthIsolationMixin:
    """Тесты не должны трогать боевой файл состояния провайдеров.

    `ai.run` записывает, кто ответил и кто отказал, — и без подмены пути тест
    писал бы в `/var/lib/watchdog/ai-health.json`. 30.10.2026 так и вышло:
    тестовые «не ответил» пометили MiniMax как упавший, и сторож на
    следующем проходе объявил бы его неработающим. Тесты не имеют права
    врать продовому состоянию.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(ai, "HEALTH_FILE",
                                  os.path.join(tmp.name, "ai-health.json"))
        patch.start()
        self.addCleanup(patch.stop)


class ConfigTest(unittest.TestCase):
    """Выбор модели — данные. Проверяем, как они читаются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WATCHDOG_AI_PROVIDER", None)

    def test_провайдер_берётся_из_файла(self):
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash"},
            "crit": {"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash",
                     "effort": "high"},
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            spec = ai_config.resolve("normal")
        self.assertEqual(spec["provider"], "minimax")
        self.assertEqual(spec["model"], "minimax/MiniMax-M3.1-Flash")

    def test_усилие_крита_не_теряется(self):
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "m", "effort": "medium"},
            "crit": {"provider": "minimax", "model": "m", "effort": "high"},
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            self.assertEqual(ai_config.resolve("crit")["effort"], "high")

    def test_переменная_фиксирует_провайдера(self):
        """Быстрый переключатель: одна переменная — и сторож говорит другой моделью."""
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "m"},
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path,
                                          "WATCHDOG_AI_PROVIDER": "anthropic"}):
            self.assertEqual(ai_config.resolve("normal")["provider"], "anthropic")

    def test_битый_файл_не_валит_сторож(self):
        path = os.path.join(self.tmp.name, "broken.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{не json")
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            spec = ai_config.resolve("normal")
        self.assertEqual(spec["provider"], "anthropic")
        self.assertEqual(spec["model"], "claude-sonnet-5")

    def test_модель_anthropic_берётся_из_переменной(self):
        """Прежнее поведение сохранено: WATCHDOG_MODEL по-прежнему значит то же."""
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": "/nonexistent/ai.json",
                                          "WATCHDOG_MODEL": "claude-sonnet-4-5"}):
            self.assertEqual(ai_config.resolve("normal")["model"], "claude-sonnet-4-5")

    def test_цепочка_начинается_с_основного(self):
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "m1"},
            "fallback": [{"provider": "anthropic", "model": "m2"},
                         {"provider": "openai", "model": "m3"}],
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            chain = ai_config.chain("normal")
        self.assertEqual([s["provider"] for s in chain], ["minimax", "anthropic", "openai"])

    def test_неизвестный_провайдер_пропускается(self):
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "m1"},
            "fallback": [{"provider": "выдуманный", "model": "m2"}],
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            chain = ai_config.chain("normal")
        self.assertNotIn("выдуманный", [s["provider"] for s in chain])

    def test_критический_уровень_берёт_свой_запас(self):
        path = _write_config(self.tmp.name, {
            "normal": {"provider": "minimax", "model": "m1"},
            "crit": {"provider": "minimax", "model": "m1", "effort": "high"},
            "fallback": [{"provider": "anthropic", "model": "обычный"}],
            "fallback_crit": [{"provider": "anthropic", "model": "сильный"}],
        })
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": path}):
            chain = ai_config.chain("crit")
        models = [s.get("model") for s in chain]
        self.assertIn("сильный", models)
        self.assertLess(models.index("сильный"), models.index("обычный"))

    def test_без_конфигурации_есть_во_что_откатиться(self):
        """Сторож без разбора хуже, чем разбор на старой модели."""
        with mock.patch.dict(os.environ, {"WATCHDOG_AI_CONFIG": "/nonexistent/ai.json"}):
            chain = ai_config.chain("normal")
        self.assertGreaterEqual(len(chain), 2)
        self.assertIn("anthropic", [s["provider"] for s in chain])


class SandboxTest(unittest.TestCase):
    """Граница read-only одна на всех провайдеров — и она не слабеет."""

    def test_инструменты_записи_запрещены(self):
        for tool in ("Bash", "Write", "Edit", "WebFetch", "Task"):
            self.assertNotIn(tool, sandbox.ALLOWED_TOOLS)
            self.assertIn(tool, sandbox.DISALLOWED_TOOLS)

    def test_у_minimax_инструментов_нет_вообще(self):
        """Там, где инструментов нет, модель не может ни прочитать файл сама,
        ни что-то выполнить — граница строже, чем запрет по списку."""
        self.assertNotIn("ALLOWED_TOOLS", inspect.getsource(ai._minimax))
        self.assertNotIn("ALLOWED_TOOLS", inspect.getsource(ai._openai))
        self.assertIn("ALLOWED_TOOLS", inspect.getsource(ai._anthropic))

    def test_текст_про_доступ_разный_у_разных_провайдеров(self):
        self.assertIn("Read и Grep", sandbox.access_text("anthropic"))
        self.assertNotIn("Read и Grep", sandbox.access_text("minimax"))
        self.assertIn("<<<FILE:", sandbox.access_text("minimax"))

    def test_плейсхолдер_подставляется(self):
        rendered = sandbox.render_system_prompt("файлы: {access}", "minimax")
        self.assertNotIn("{access}", rendered)
        self.assertIn("<<<FILE:", rendered)

    def test_промпт_разбора_собран_без_остатка_плейсхолдера(self):
        import analyst
        rendered = sandbox.render_system_prompt(analyst.SYSTEM_PROMPT, "minimax")
        self.assertNotIn("{access}", rendered)


class FallbackTest(HealthIsolationMixin, unittest.TestCase):
    """Отказ одного провайдера не должен оставлять владельца без разбора."""

    def _options(self, tmp):
        return sandbox.options(cwd=tmp, system_prompt="x", max_turns=1)

    def test_сломанный_первый_не_мешает_второму(self):
        calls = []

        async def broken(spec, prompt, options):
            calls.append(spec["provider"])
            raise RuntimeError("не ответил")

        async def working(spec, prompt, options):
            calls.append(spec["provider"])
            return "разбор есть", {}

        with tempfile.TemporaryDirectory() as tmp:
            options = self._options(tmp)
            with mock.patch.dict(ai.PROVIDERS, {"minimax": broken, "anthropic": working}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "m1"},
                    {"provider": "anthropic", "model": "m2"},
                ]):
                    text, meta = asyncio.run(ai.run("вопрос", options))
        self.assertEqual(text, "разбор есть")
        self.assertEqual(meta["provider"], "anthropic")
        self.assertTrue(meta["failed"])
        self.assertIn("minimax", meta["failed"][0])

    def test_все_отказали_это_понятная_ошибка(self):
        async def broken(spec, prompt, options):
            raise RuntimeError("не ответил")

        with tempfile.TemporaryDirectory() as tmp:
            options = self._options(tmp)
            with mock.patch.dict(ai.PROVIDERS, {"minimax": broken}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "m1"},
                ]):
                    with self.assertRaises(ai.AiUnavailable):
                        asyncio.run(ai.run("вопрос", options))

    def test_модель_вызывающего_идёт_в_первый_провайдер(self):
        """Сильная модель выбрана вызывающим — и именно она должна уйти в запуск."""
        seen = {}

        async def spy(spec, prompt, options):
            seen["model"] = spec.get("model")
            return "ok", {}

        with tempfile.TemporaryDirectory() as tmp:
            options = sandbox.options(cwd=tmp, system_prompt="x", max_turns=1,
                                      model="сильная-модель", tier="crit")
            with mock.patch.dict(ai.PROVIDERS, {"minimax": spy}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "обычная"},
                    {"provider": "anthropic", "model": "запасная"},
                ]):
                    asyncio.run(ai.run("вопрос", options))
        self.assertEqual(seen["model"], "сильная-модель")


class CircuitBreakerTest(HealthIsolationMixin, unittest.TestCase):
    """Мёртвый провайдер не должен съедать время каждого прохода.

    30.10.2026: Anthropic требует верификации и, скорее всего, её не пройдёт.
    Пока он стоял запасным, это было почти безобидно. Но стоило основному
    провайдеру споткнуться — и цепочка упиралась в Anthropic на все 600 секунд
    таймаута, чтобы услышать то же самое.
    """

    def setUp(self):
        super().setUp()
        self.health = ai.HEALTH_FILE

    def test_после_отказа_провайдер_пропускается(self):
        ai._mark({}, "anthropic", ok=False, error="403", cooldown_sec=3600)
        self.assertTrue(ai._in_cooldown(ai._read_health(), "anthropic"))

    def test_после_успеха_остывание_снимается(self):
        ai._mark({}, "anthropic", ok=False, error="403", cooldown_sec=3600)
        ai._mark(ai._read_health(), "anthropic", ok=True)
        self.assertFalse(ai._in_cooldown(ai._read_health(), "anthropic"))

    def test_остывшего_не_спрашивают_если_есть_кого(self):
        ai._mark({}, "anthropic", ok=False, error="403", cooldown_sec=3600)
        asked = []

        async def failing(spec, prompt, options):
            asked.append(spec["provider"])
            raise RuntimeError("не ответил")

        async def working(spec, prompt, options):
            asked.append(spec["provider"])
            return "ок", {}

        with tempfile.TemporaryDirectory() as tmp:
            options = sandbox.options(cwd=tmp, system_prompt="x", max_turns=1)
            with mock.patch.dict(ai.PROVIDERS, {"minimax": working,
                                               "anthropic": failing}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "m1"},
                    {"provider": "anthropic", "model": "m2"},
                ]):
                    text, meta = asyncio.run(ai.run("вопрос", options))
        self.assertEqual(text, "ок")
        self.assertNotIn("anthropic", asked, "остывающего не должны спрашивать")
        self.assertIn("anthropic", meta.get("skipped", []))

    def test_если_остыли_все_спрашивают_всех(self):
        """Молчать хуже, чем спросить и не дождаться."""
        ai._mark({}, "minimax", ok=False, error="упал", cooldown_sec=3600)
        ai._mark(ai._read_health(), "anthropic", ok=False, error="403",
                 cooldown_sec=3600)
        asked = []

        async def failing(spec, prompt, options):
            asked.append(spec["provider"])
            raise RuntimeError("не ответил")

        with tempfile.TemporaryDirectory() as tmp:
            options = sandbox.options(cwd=tmp, system_prompt="x", max_turns=1)
            with mock.patch.dict(ai.PROVIDERS, {"minimax": failing,
                                               "anthropic": failing}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "m1"},
                    {"provider": "anthropic", "model": "m2"},
                ]):
                    with self.assertRaises(ai.AiUnavailable):
                        asyncio.run(ai.run("вопрос", options))
        self.assertEqual(sorted(asked), ["anthropic", "minimax"])

    def test_состояние_пишется_на_диск(self):
        ai._mark({}, "minimax", ok=True)
        self.assertTrue(os.path.exists(self.health))
        self.assertIn("minimax", ai._read_health())


class ProviderModelTest(HealthIsolationMixin, unittest.TestCase):
    """Запасной провайдер должен зваться СВОЕЙ моделью.

    30.10.2026: бэкенды брали модель из общих настроек вызова. Общие настройки
    описывают выбранный уровень (обычный или сильный), а для запасного
    провайдера в цепочке они неверны — Anthropic вызывался с именем модели
    MiniMax и падал с «issue with the selected model». То есть запасной был
    нерабочим по построению.
    """

    def test_бэкенд_берёт_модель_из_своей_спецификации(self):
        import inspect
        for name in ("_anthropic", "_minimax", "_openai"):
            source = inspect.getsource(getattr(ai, name))
            self.assertIn("spec.get(\"model\")", source,
                          f"{name} должен брать модель из спецификации")

    def test_запасной_не_получает_модель_основного(self):
        seen = {}

        async def spy(spec, prompt, options):
            seen["model"] = spec.get("model")
            return "ок", {}

        with tempfile.TemporaryDirectory() as tmp:
            options = sandbox.options(cwd=tmp, system_prompt="x", max_turns=1,
                                      model="модель-основного")
            with mock.patch.dict(ai.PROVIDERS, {"anthropic": spy}):
                with mock.patch.object(ai_config, "chain", return_value=[
                    {"provider": "minimax", "model": "m1"},
                    {"provider": "anthropic", "model": "модель-Запасного"},
                ]):
                    asyncio.run(ai.run("вопрос", options))
        self.assertEqual(seen["model"], "модель-Запасного")


class SchemaRetryTest(HealthIsolationMixin, unittest.TestCase):
    """Схема — страховка, а не приговор.

    30.10.2026: модель ответила по форме, но не заполнила `why` у одного
    варианта. Жёсткая схема отвергла весь ответ, запасной провайдер был
    недоступен, и вместо разбора пришло «аналитик не отработал». Теперь схема
    мягкая, а отказ от неё не смертелен.
    """

    class _FakeProc:
        def __init__(self, returncode, answer=None):
            self.returncode = returncode
            self._answer = answer or ""

        async def communicate(self, _payload):
            return ("ошибка".encode(), b"")

        def kill(self):
            pass

    def test_ответ_не_по_схеме_не_остаётся_без_разбора(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            cmd = list(args)
            calls.append(cmd)
            out_file = cmd[cmd.index("-o") + 1]
            if len(calls) == 1:
                # Первый проход со схемой падает.
                return self._FakeProc(1)
            with open(out_file, "w", encoding="utf-8") as f:
                f.write('{"severity": "warn", "headline": "ок", "explanation": "ок", '
                        '"options": [{"action": "nothing"}]}')
            return self._FakeProc(0)

        options = sandbox.options(cwd=tempfile.gettempdir(), system_prompt="x",
                                  max_turns=1)
        options.output_schema = {"type": "object", "required": ["severity"]}
        options.model = "minimax/MiniMax-M3.1-Flash"

        with mock.patch.object(ai.asyncio, "create_subprocess_exec", fake_exec):
            text, _meta = asyncio.run(ai._minimax({}, "вопрос", options))

        self.assertEqual(len(calls), 2, "должен быть повтор без схемы")
        self.assertIn("--output-schema", calls[0])
        self.assertNotIn("--output-schema", calls[1])
        self.assertIn("severity", text)

    def test_схема_требует_только_action(self):
        """Иначе модель обязана заполнить каждое поле — и разбор опять пропадёт."""
        import analyst
        item = analyst.VERDICT_SCHEMA["properties"]["options"]["items"]
        self.assertEqual(item["required"], ["action"])


class BundleTest(unittest.TestCase):
    """Провайдеры без инструментов получают файлы инцидента от нас."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "context"))
        with open(os.path.join(self.tmp.name, "events.json"), "w", encoding="utf-8") as f:
            json.dump([{"kind": "docker", "key": "svod-bot-1"}], f)
        with open(os.path.join(self.tmp.name, "facts.json"), "w", encoding="utf-8") as f:
            json.dump({"disk": "ok"}, f)
        with open(os.path.join(self.tmp.name, "context", "unit.log"), "w", encoding="utf-8") as f:
            f.write("юнит упал\n")

    def test_файлы_попадают_в_разбор(self):
        bundle = ai.bundle_incident(self.tmp.name)
        self.assertIn("events.json", bundle)
        self.assertIn("facts.json", bundle)
        self.assertIn("unit.log", bundle)
        self.assertIn("юнит упал", bundle)

    def test_токены_вычищаются(self):
        with open(os.path.join(self.tmp.name, "context", "secret.log"), "w", encoding="utf-8") as f:
            f.write("authorization: Bearer mmoat_abcdefghijklmnopqrstuvwxyz0123456789\n")
        bundle = ai.bundle_incident(self.tmp.name)
        self.assertNotIn("mmoat_abcdefghijklmnopqrstuvwxyz0123456789", bundle)

    def test_бюджет_контекста_соблюдается(self):
        path = os.path.join(self.tmp.name, "context", "huge.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (ai.MAX_FILE_CHARS * 3))
        bundle = ai.bundle_incident(self.tmp.name)
        self.assertLess(len(bundle), ai.MAX_TOTAL_CHARS * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
