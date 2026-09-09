#!/usr/bin/env python3
"""Проверка того, как уведомление выглядит у владельца.

Здесь проверяется не доставка, а язык. Причина конкретная: строки вида
«Память: memory — swap_pct=80» и «Удалённый сервер DE сервер» выглядят выводом
отладчика, а не сообщением человеку, и владелец перестаёт их читать — ровно то
же, чем кончается любой спам-мониторинг.

Вторая половина — про безопасность: текст вердикта пишет модель, читавшая
логи nginx и fail2ban, куда пишет тот, кто ломится снаружи.

Запуск: venv/bin/python watchdog/test_notify.py
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402

TAGS = re.compile(r"<[^>]+>")


def plain(html_text):
    return TAGS.sub("", html_text)


def result(events, severity="crit", headline="Заголовок", meta=None):
    return {
        "incident": "20260908T120000Z",
        "events": events,
        "meta": meta or {},
        "verdict": {"severity": severity, "headline": headline,
                    "explanation": "Пояснение.",
                    "options": [{"action": "nothing", "target": None,
                                 "label": "Ничего не делать", "why": "—"}]},
    }


class LanguageTest(unittest.TestCase):
    """Ни одна строка не должна показывать внутреннее имя поля."""

    def line(self, event):
        return plain(notify.describe_event(event))

    def test_метрики_удалённого_сервера_словами(self):
        """Раньше сюда уходило «disk_pct=93» — та же болезнь, что лечили
        у локальных метрик, просто в ветке, до которой не дошли руки."""
        line = self.line({"kind": "remote", "key": "DE сервер", "to": "crit",
                          "severity": "crit", "detail": "диск занят на 93%"})
        self.assertIn("диск занят на 93%", line)
        self.assertNotIn("disk_pct", line)

    def test_имя_сервера_не_склеивается_с_видом(self):
        """«Удалённый сервер DE сервер» — не название, а склейка."""
        line = self.line({"kind": "remote", "key": "DE сервер",
                          "severity": "crit", "detail": "агент молчит"})
        self.assertTrue(line.startswith("🔴 DE сервер"), line)
        self.assertNotIn("Удалённый сервер DE", line)

    def test_состояния_из_списков_названы_по_русски(self):
        cases = [
            ({"kind": "docker", "key": "c", "list_field": "stopped",
              "severity": "crit", "detail": "не запущен"}, "не запущен", "stopped"),
            ({"kind": "systemd", "key": "a.service", "list_field": "failed",
              "severity": "crit", "detail": "упала"}, "упала", "failed"),
        ]
        for event, expect, leak in cases:
            with self.subTest(leak=leak):
                line = self.line(event)
                self.assertIn(expect, line)
                self.assertNotIn(leak, line)

    def test_направление_не_дублируется(self):
        """«стало плохо: упала» — сказано дважды. У списочных событий
        направление уже сидит в самом описании."""
        line = self.line({"kind": "systemd", "key": "a.service", "to": "crit",
                          "list_field": "failed", "severity": "crit",
                          "detail": "упала"})
        self.assertNotIn("стало плохо", line)

    def test_у_числовых_событий_направление_остаётся(self):
        """А вот «занято 91%» само по себе не говорит, стало хуже или лучше."""
        line = self.line({"kind": "disk", "key": "root", "to": "crit",
                          "severity": "crit", "detail": "занято 91%"})
        self.assertIn("стало плохо", line)

    def test_у_сайтов_и_сертификатов_видно_домен_а_не_ссылку(self):
        for kind, expect in (("http", "Сайт plgamesbot.ru"),
                             ("cert", "Сертификат plgamesbot.ru")):
            with self.subTest(kind=kind):
                line = self.line({"kind": kind, "key": "https://plgamesbot.ru/",
                                  "severity": "crit", "detail": "HTTP 502"})
                self.assertIn(expect, line)
                self.assertNotIn("https://", line)

    def test_память_и_процессор_не_повторяют_себя(self):
        line = self.line({"kind": "memory", "key": "memory", "to": "warn",
                          "severity": "warn", "detail": "подкачка занята на 80%"})
        self.assertNotIn("memory", line)


class SafetyTest(unittest.TestCase):
    """Текст вердикта составлен по содержимому логов, куда пишет атакующий."""

    def test_разметка_из_логов_обезврежена(self):
        text = notify.render(result(
            [{"kind": "docker", "key": "<b>злой</b>", "severity": "crit",
              "detail": "<a href='http://evil'>клик</a>", "list_field": "stopped"}],
            headline="<i>подделка</i>"))
        self.assertNotIn("<a href", text)
        self.assertNotIn("<i>подделка</i>", text)
        self.assertIn("&lt;i&gt;подделка", text)

    def test_кавычка_остаётся_кавычкой(self):
        """html.escape по умолчанию делает из неё &quot;, и владелец читает
        мнемонику вместо цитаты из лога. Экранировать надо три символа."""
        text = notify.render(result(
            [{"kind": "docker", "key": "c", "severity": "crit",
              "detail": 'ошибка "connection refused"'}]))
        self.assertIn('"connection refused"', text)
        self.assertNotIn("&quot;", text)

    def test_кнопки_несут_только_номер_варианта(self):
        keyboard = notify.keyboard(result([]))
        data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(data, "wd:20260908T120000Z:0")


class MarkTest(unittest.TestCase):

    def test_слабый_разбор_помечен(self):
        text = notify.render(result([], meta={"crit_budget_exhausted": True}))
        self.assertIn("запас усиленных разборов исчерпан", text)

    def test_обычный_разбор_не_помечен(self):
        text = notify.render(result([], meta={"crit_budget_exhausted": False}))
        self.assertNotIn("исчерпан", text)

    def test_подсказка_про_диалог_есть_всегда(self):
        """Без неё возможность не существует: владелец не догадается, что на
        сообщение бота можно отвечать."""
        self.assertIn("Ответьте на это сообщение", notify.render(result([])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
