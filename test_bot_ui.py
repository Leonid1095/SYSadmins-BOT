#!/usr/bin/env python3
"""Регрессии по разбору безопасности от 08.09.2026 и проверки экранов бота.

Каждый тест здесь закрывает конкретную находку. Смысл не в покрытии, а в том,
чтобы починенное не отъехало обратно: ровно так и появилась F1 — обработчик
остался на старой схеме, когда остальные три перевели на подпись.
"""

import asyncio
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import bot          # noqa: E402
import infra        # noqa: E402
import agent_auth   # noqa: E402

# Теги, которые Telegram принимает в parse_mode='HTML'.
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote"}


def assert_valid_telegram_html(testcase, text):
    """Непарный или чужой тег — это 400 от Telegram, то есть владелец не видит
    сообщения вообще. Проверяем так же строго, как проверит Telegram."""
    stack = []
    for match in re.finditer(r"<(/?)([a-zA-Z-]+)([^>]*)>", text):
        closing, tag = match.group(1), match.group(2).lower()
        testcase.assertIn(tag, ALLOWED_TAGS, f"недопустимый тег <{tag}> в {text[:120]!r}")
        if closing:
            testcase.assertTrue(stack and stack[-1] == tag,
                                f"непарный </{tag}> в {text[:120]!r}")
            stack.pop()
        else:
            stack.append(tag)
    testcase.assertEqual(stack, [], f"не закрыты теги {stack} в {text[:120]!r}")
    testcase.assertLessEqual(len(text), 4096, "сообщение длиннее лимита Telegram")


class SecretNeverTravels(unittest.TestCase):
    """F1: секрет агента не должен уходить по сети ни из одного места."""

    def test_no_legacy_secret_header_anywhere(self):
        """Заголовок X-Secret-Key нёс сам ключ по открытому HTTP.

        Проверяем весь код, а не один обработчик: копий опроса агента было три,
        и отъехала именно та, про которую забыли.
        """
        offenders = []
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in {"venv", ".git", "__pycache__"}]
            for name in files:
                if not name.endswith(".py") or name == os.path.basename(__file__):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        # Упоминание в комментарии-объяснении допустимо;
                        # ловим только реальную отправку заголовка.
                        if re.search(r'["\']X-Secret-Key["\']\s*:', line):
                            offenders.append(f"{path}:{lineno}")
        self.assertEqual(offenders, [],
                         "секрет снова передаётся заголовком: " + ", ".join(offenders))

    def test_status_request_is_signed(self):
        """Заголовки для агента — подпись, метка времени и nonce, но не ключ."""
        # Заведомо фиктивный ключ: настоящему в репозитории не место,
        # даже в тесте — он попал бы в историю навсегда.
        secret = "00000000-0000-4000-8000-000000000000"
        headers = agent_auth.build_headers(secret, "GET", "/status")
        self.assertEqual(set(headers), {"X-Signature", "X-Timestamp", "X-Nonce"})
        self.assertNotIn(secret, "".join(headers.values()))


class ThresholdCallbackIsValidated(unittest.TestCase):
    """F7 разбора: имя параметра и значение брались из нажатия как есть."""

    def _press(self, data):
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.from_user.id = 1148520376
        update = MagicMock()
        update.callback_query = query
        return query, update

    def _run(self, data):
        query, update = self._press(data)
        with patch.object(bot, "load_monitor_subs", return_value={}), \
             patch.object(bot, "save_monitor_subs") as save, \
             patch.object(bot, "show", new=AsyncMock()):
            asyncio.run(bot.monitor_set_value(update, MagicMock()))
        return query, save

    def test_unknown_parameter_rejected(self):
        query, save = self._run("monitor_val_evil_5")
        save.assert_not_called()
        query.answer.assert_awaited()

    def test_non_numeric_value_rejected(self):
        """Раньше здесь падал int() и обработчик умирал молча."""
        query, save = self._run("monitor_val_disk_warn_abc")
        save.assert_not_called()

    def test_out_of_range_value_rejected(self):
        query, save = self._run("monitor_val_disk_warn_1")
        save.assert_not_called()

    def test_valid_value_saved(self):
        query, save = self._run("monitor_val_disk_warn_85")
        save.assert_called_once()
        saved = save.call_args[0][0]
        self.assertEqual(saved["1148520376"]["disk_warn"], 85)


class VerdictTextIsEscaped(unittest.TestCase):
    """F3 разбора: текст вердикта пишет модель, читавшая логи атакующего."""

    def test_watchdog_option_escapes_model_text(self):
        hostile = '<a href="http://evil/">Срочно нажмите</a>'
        query = MagicMock()
        query.data = "wd:20260908T120000Z:0"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.text_html = "🔴 <b>Инцидент</b>"
        update = MagicMock()
        update.callback_query = query

        chosen = {"action": "restart_unit", "target": "nginx.service",
                  "label": hostile, "why": hostile}
        with patch.object(bot.incidents, "option", return_value=({}, chosen)):
            asyncio.run(bot.watchdog_option(update, MagicMock()))

        sent = query.edit_message_text.await_args[0][0]
        self.assertNotIn('<a href="http://evil/">', sent,
                         "ссылка из вердикта модели попала в сообщение как разметка")
        self.assertIn("&lt;a href=", sent)
        assert_valid_telegram_html(self, sent)


class InfraRendersSafely(unittest.TestCase):
    """Экраны инфраструктуры не должны ломаться на пустых и враждебных данных."""

    HOSTILE = '<script>x</script> & "кавычки" <b>жирный</b>'

    def _snapshot(self, facts):
        return {"updated_at": "2026-09-08T08:39:22+00:00", "facts": facts}

    def test_all_sections_survive_empty_snapshot(self):
        snap = self._snapshot({})
        for name, render in (("host", infra.render_host),
                             ("overview", infra.render_overview),
                             ("containers", infra.render_containers),
                             ("services", infra.render_services),
                             ("sites", infra.render_sites),
                             ("certs", infra.render_certs),
                             ("security", infra.render_security)):
            with self.subTest(section=name):
                assert_valid_telegram_html(self, render(snap))

    def test_hostile_container_name_is_escaped(self):
        """Имя контейнера задаёт тот, кто его запускал, — не обязательно владелец."""
        snap = self._snapshot({"docker": {"total": 2, "stopped": [self.HOSTILE],
                                          "unhealthy": [], "restarting": []}})
        text = infra.render_containers(snap)
        self.assertNotIn("<script>", text)
        assert_valid_telegram_html(self, text)

    def test_hostile_endpoint_is_escaped(self):
        snap = self._snapshot({"endpoints": {
            f"https://{self.HOSTILE}": {"http": 500, "cert_days": 5}}})
        for render in (infra.render_sites, infra.render_certs):
            with self.subTest(render=render.__name__):
                text = render(snap)
                self.assertNotIn("<script>", text)
                assert_valid_telegram_html(self, text)

    def test_missing_snapshot_is_explained_not_crashed(self):
        with patch.object(infra, "STATE_FILE", "/nonexistent/state.json"):
            with self.assertRaises(infra.SnapshotUnavailable) as ctx:
                infra.load_snapshot()
        self.assertIn("сторож", str(ctx.exception).lower())

    def test_bands_match_watchdog(self):
        """Пороги бота и сторожа обязаны совпадать: разошлись — и бот говорит
        «всё хорошо» там, где сторож уже прислал алерт."""
        sys.path.insert(0, os.path.join(BASE_DIR, "watchdog"))
        import delta
        self.assertEqual(infra.BANDS["disk"], delta.RISING[("disk", "pct")])
        self.assertEqual(infra.BANDS["memory"], delta.RISING[("memory", "used_pct")])
        self.assertEqual(infra.BANDS["swap"], delta.RISING[("memory", "swap_pct")])
        self.assertEqual(infra.BANDS["cpu"], delta.RISING[("cpu", "load_per_core_pct")])
        self.assertEqual(infra.BANDS["temp"], delta.RISING[("temperature", "cpu_c")])
        self.assertEqual(infra.CERT_DAYS, delta.CERT_DAYS)
        self.assertEqual(bot.REMOTE_BANDS["disk"], delta.REMOTE_RISING["disk_pct"])
        self.assertEqual(bot.REMOTE_BANDS["memory"], delta.REMOTE_RISING["mem_pct"])
        self.assertEqual(bot.REMOTE_BANDS["cpu"], delta.REMOTE_RISING["cpu_pct"])


class RemoteStatusExplainsFailures(unittest.TestCase):
    """Одно «не удалось подключиться» на все случаи не давало понять причину."""

    def _fetch(self, exc):
        with patch.object(bot.requests, "get", side_effect=exc):
            return asyncio.run(bot.fetch_server_status(
                "тестовый сервер", {"server_ip": "203.0.113.10", "secret_key": "k"}, "1"))

    def test_503_says_key_not_configured(self):
        response = MagicMock()
        response.status_code = 503
        error = bot.requests.exceptions.HTTPError(response=response)
        text = self._fetch(error)
        self.assertIn("ключ", text.lower())
        assert_valid_telegram_html(self, text)

    def test_403_points_at_old_agent(self):
        response = MagicMock()
        response.status_code = 403
        error = bot.requests.exceptions.HTTPError(response=response)
        text = self._fetch(error)
        self.assertIn("подпис", text.lower())
        assert_valid_telegram_html(self, text)

    def test_no_connection_lists_causes(self):
        text = self._fetch(bot.requests.exceptions.ConnectTimeout("timeout"))
        self.assertIn("5000", text)
        assert_valid_telegram_html(self, text)

    def test_status_text_is_valid_html(self):
        data = {"cpu": 95, "cpu_temp": 71,
                "memory": {"used": "30.1", "total": "39.0", "percent": 96},
                "disk": {"used": "180", "total": "200", "percent": 91},
                "gpu": {"name": "RTX <4090>", "load": 99, "temp": 88}}
        text = bot.get_status_text(data, 'Сервер <b>"тест"</b>')
        self.assertNotIn("<b>тест", text)
        assert_valid_telegram_html(self, text)


class LocalHostIsNotAddedAsRemote(unittest.TestCase):
    """Центральный сервер наблюдается через сторожа, а не через агента:
    защита от внутренних адресов должна остаться на месте."""

    def test_internal_addresses_rejected(self):
        for addr in ("127.0.0.1", "192.168.31.7", "10.0.0.1", "169.254.169.254",
                     "0.0.0.0", "::1", "не-адрес"):
            with self.subTest(addr=addr):
                self.assertFalse(bot.is_valid_ip(addr))

    def test_documentation_ranges_rejected(self):
        """Диапазоны RFC 5737 — для документации, маршрутизируемых машин там нет.
        Python помечает их зарезервированными, и проверка обязана их отбивать."""
        for addr in ("203.0.113.10", "198.51.100.7", "192.0.2.42"):
            with self.subTest(addr=addr):
                self.assertFalse(bot.is_valid_ip(addr))

    def test_public_addresses_accepted(self):
        # Общеизвестные публичные адреса вместо адресов владельца: репозиторий
        # публичный, и его инфраструктуре в тестах не место.
        for addr in ("8.8.8.8", "1.1.1.1", "9.9.9.9"):
            with self.subTest(addr=addr):
                self.assertTrue(bot.is_valid_ip(addr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
