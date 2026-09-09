#!/usr/bin/env python3
"""Проверка наблюдения за PLGamesBot.

Смысл наблюдения — в одном правиле: канал, где наш бот молчит (забанен, не дали
прав модератора, не подаёт признаков жизни), не должен попадать в публичные
списки, пока стример в эфире. 04.09.2026 `k1sume_qq` забанила бота, и пять дней
мы приводили зрителей туда, где нашего продукта нет. Сам продукт с 09.09 такие
каналы прячет — сторож проверяет, что прячет на самом деле, а не в тестах:
читает базу и дёргает боевую ручку, сверяя одно с другим.

Вывод детектора нарочно независим от кода PLGamesBot. Второе мнение имеет смысл
только тогда, когда получено своим путём: сломайся определение «молчит» внутри
продукта — самопроверка продукта об этом не скажет, а сверка со стороны скажет.

Запуск: python3 watchdog/test_plgamesbot.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import deepen  # noqa: E402
import delta  # noqa: E402


def _make_db(path, rows):
    """База с тем минимумом столбцов PLGamesBot, который читает детектор."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE streamers (twitch_id INTEGER PRIMARY KEY, "
                "twitch_username TEXT, is_bot_active INTEGER, is_stream_live INTEGER)")
    # Столбцы те же, что в боевой базе PLGamesBot: расхождение схемы должно
    # ловиться тестом, а не молчаливым «сбор контекста не удался» на проде.
    con.execute("CREATE TABLE bot_status (streamer_id INTEGER PRIMARY KEY, state TEXT, "
                "detail TEXT, send_error TEXT, is_mod INTEGER, heartbeat_at TEXT, "
                "last_cmd TEXT, last_cmd_at TEXT)")
    for r in rows:
        con.execute("INSERT INTO streamers VALUES (?,?,?,?)",
                    (r["id"], r["name"], r.get("bot_on", 1), r.get("live", 0)))
        if "state" in r:
            con.execute("INSERT INTO bot_status VALUES (?,?,?,?,?,?,?,?)",
                        (r["id"], r["state"], r.get("detail"), r.get("send_error"),
                         r.get("is_mod", 1), r.get("heartbeat"),
                         r.get("last_cmd"), r.get("last_cmd_at")))
    con.commit()
    con.close()


def _hb(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(
        tzinfo=None).isoformat(sep=" ")


#: Шесть каналов — по одному на каждый случай, как в тесте самого продукта.
ROWS = [
    {"id": 1, "name": "otvechaet", "live": 1, "state": "connected", "is_mod": 1,
     "heartbeat": _hb(5)},
    {"id": 2, "name": "zabanili", "live": 1, "state": "error", "is_mod": 0,
     "detail": "бот забанен в этом канале — снимите бан: /unban plgames_bot",
     "heartbeat": _hb(5)},
    {"id": 3, "name": "bez_moda", "live": 1, "state": "connected", "is_mod": 0,
     "heartbeat": _hb(5)},
    {"id": 4, "name": "bot_vyklyuchen", "live": 1, "bot_on": 0},
    {"id": 5, "name": "net_signala", "live": 0, "state": "connected", "is_mod": 1,
     "heartbeat": _hb(3600)},
    {"id": 6, "name": "ne_v_efire", "live": 0, "state": "connected", "is_mod": 1,
     "heartbeat": _hb(5)},
]


class ДетекторБазы(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "plgamesbot.db")
        _make_db(self.db, ROWS)
        self.old = collect.PLGAMESBOT_DB
        collect.PLGAMESBOT_DB = self.db

    def tearDown(self):
        collect.PLGAMESBOT_DB = self.old
        self.tmp.cleanup()

    def facts(self):
        return collect.Collector().plgamesbot()

    def test_забаненный_найден(self):
        self.assertEqual(self.facts()["banned"], ["zabanili"])

    def test_без_прав_модератора_найден(self):
        self.assertEqual(self.facts()["nomod"], ["bez_moda"])

    def test_без_признаков_жизни_найден(self):
        self.assertEqual(self.facts()["no_signal"], ["net_signala"])

    def test_здоровый_и_выключенный_не_попадают_никуда(self):
        """Выключенный бот — не поломка: награды за баллы идут мимо него."""
        facts = self.facts()
        everywhere = set(facts["banned"]) | set(facts["nomod"]) | set(facts["no_signal"])
        self.assertNotIn("otvechaet", everywhere)
        self.assertNotIn("bot_vyklyuchen", everywhere)

    def test_забаненный_не_считается_ещё_и_безмодным(self):
        """Одна поломка — одна строка владельцу, а не две про один канал."""
        self.assertNotIn("zabanili", self.facts()["nomod"])

    def test_на_чужой_машине_детектор_молчит(self):
        collect.PLGAMESBOT_DB = os.path.join(self.tmp.name, "нет-такого.db")
        self.assertIsNone(self.facts())


class СверкаСВитриной(unittest.TestCase):
    """Главное правило: молчащий канал не показываем, пока он в эфире."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "plgamesbot.db")
        _make_db(self.db, ROWS)
        self.old = collect.PLGAMESBOT_DB
        collect.PLGAMESBOT_DB = self.db

    def tearDown(self):
        collect.PLGAMESBOT_DB = self.old
        self.tmp.cleanup()

    def facts(self, answer):
        c = collect.Collector()
        c._http_json = lambda url: answer() if callable(answer) else answer
        return c.plgamesbot_public()

    def test_правило_соблюдено_молчание(self):
        """Ручка отдаёт только тех, у кого бот отвечает, — сказать нечего."""
        facts = self.facts([{"twitch_username": "otvechaet"}])
        self.assertEqual(facts["advertised_silent"], [])
        self.assertEqual(facts["unreachable"], [])

    def test_показали_канал_с_забаненным_ботом(self):
        facts = self.facts([{"twitch_username": "otvechaet"},
                            {"twitch_username": "zabanili"}])
        self.assertEqual(facts["advertised_silent"], ["zabanili"])

    def test_показали_канал_без_модератора(self):
        facts = self.facts([{"twitch_username": "bez_moda"}])
        self.assertEqual(facts["advertised_silent"], ["bez_moda"])

    def test_ручка_недоступна_это_не_всё_хорошо(self):
        """Молчание проверки не должно выглядеть как соблюдённое правило."""
        def boom(url=None):
            raise OSError("connection refused")
        facts = self.facts(boom)
        self.assertEqual(facts["unreachable"], ["/api/streams/live"])
        self.assertEqual(facts["advertised_silent"], [])


class ДельтаПоКаналам(unittest.TestCase):
    def test_новый_бан_будит_модель(self):
        was = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        now = {"plgamesbot": {"banned": ["zabanili"], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([(e["kind"], e["key"], e["severity"]) for e in builder.events],
                         [("plgamesbot", "zabanili", "crit")])

    def test_бан_сняли_и_это_видно(self):
        was = {"plgamesbot": {"banned": ["zabanili"], "nomod": [], "no_signal": []}}
        now = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([e["severity"] for e in builder.events], ["resolved"])

    def test_давно_забаненный_молчит(self):
        """Сегодняшние молчащие каналы не превращаются в крит каждые пять минут."""
        same = {"plgamesbot": {"banned": ["zabanili"], "nomod": ["bez_moda"],
                               "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(same, same, delta.ignored())
        self.assertEqual(builder.events, [])

    def test_показ_молчащего_канала_это_крит(self):
        was = {"plgamesbot_public": {"advertised_silent": [], "unreachable": []}}
        now = {"plgamesbot_public": {"advertised_silent": ["zabanili"], "unreachable": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([(e["key"], e["severity"]) for e in builder.events],
                         [("zabanili", "crit")])

    def test_сломанная_проверка_не_крит_но_и_не_тишина(self):
        was = {"plgamesbot_public": {"advertised_silent": [], "unreachable": []}}
        now = {"plgamesbot_public": {"advertised_silent": [],
                                     "unreachable": ["/api/streams/live"]}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([e["severity"] for e in builder.events], ["warn"])


class СообщениеВладельцу(unittest.TestCase):
    def test_канал_называется_каналом(self):
        import notify
        self.assertEqual(
            notify.subject_of({"kind": "plgamesbot", "key": "zabanili"}),
            "Канал zabanili")

    def test_витрина_названа_витриной(self):
        import notify
        self.assertEqual(
            notify.subject_of({"kind": "plgamesbot_public", "key": "zabanili"}),
            "Витрина zabanili")


class КонтекстДляРазбора(unittest.TestCase):
    """Модель должна получить факты о канале, а не одно его имя.

    Без этого разбор бесполезен: в каталоге действий сторожа нет и не может
    быть кнопки «попросить стримера снять бан», и модель начнёт перебирать
    рестарты юнитов. Полезный ответ здесь другой: что именно должен набрать
    стример и есть ли у нас вообще способ ему это сказать.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "plgamesbot.db")
        _make_db(self.db, ROWS)
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE streamers ADD COLUMN telegram_id INTEGER")
        con.execute("UPDATE streamers SET telegram_id = 111 WHERE twitch_username = 'bez_moda'")
        con.commit(); con.close()
        self.old = deepen.PLGAMESBOT_DB
        deepen.PLGAMESBOT_DB = self.db

    def tearDown(self):
        deepen.PLGAMESBOT_DB = self.old
        self.tmp.cleanup()

    def test_собран_разбор_по_каналу(self):
        files = deepen._plgamesbot_context("zabanili")
        body = "\n".join(files.values())
        self.assertIn("zabanili", body)
        self.assertIn("забанен", body)

    def test_видно_что_сказать_стримеру_нечем(self):
        """Главный вопрос владельца: можем ли мы вообще до него достучаться."""
        body = "\n".join(deepen._plgamesbot_context("zabanili").values())
        self.assertIn("Telegram не привязан", body)

    def test_видно_когда_достучаться_можно(self):
        body = "\n".join(deepen._plgamesbot_context("bez_moda").values())
        self.assertIn("Telegram привязан", body)

    def test_чужое_имя_не_даёт_файла(self):
        self.assertIsNone(deepen._plgamesbot_context("нет-такого-канала"))

    def test_сборщик_подключён_к_обоим_видам(self):
        self.assertIn("plgamesbot", deepen.GATHERERS)
        self.assertIn("plgamesbot_public", deepen.GATHERERS)


class ПромптЗнаетПроКаналы(unittest.TestCase):
    def test_аналитику_сказано_что_кнопки_тут_не_помогут(self):
        """Иначе модель предложит рестартовать юнит в ответ на бан в чужом чате."""
        import analyst
        self.assertIn("PLGamesBot", analyst.SYSTEM_PROMPT)


class НаБоевойБазе(unittest.TestCase):
    """Схему чужой базы мы не контролируем — проверяем на настоящей.

    Иначе переименование столбца в PLGamesBot превратило бы разбор в файл
    «сбор контекста не удался», и заметили бы это в момент аварии.
    """

    def setUp(self):
        if not os.path.exists(collect.PLGAMESBOT_DB):
            self.skipTest("PLGamesBot на этой машине не стоит")

    def test_детектор_читает_боевую_базу(self):
        facts = collect.Collector().plgamesbot()
        self.assertIsNotNone(facts)
        self.assertEqual(set(facts), {"banned", "nomod", "no_signal"})

    def test_контекст_собирается_по_живому_каналу(self):
        facts = collect.Collector().plgamesbot()
        silent = facts["banned"] + facts["nomod"] + facts["no_signal"]
        if not silent:
            self.skipTest("сейчас молчащих каналов нет — проверять нечего")
        body = "\n".join(deepen._plgamesbot_context(silent[0]).values())
        self.assertIn("связь со стримером", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
