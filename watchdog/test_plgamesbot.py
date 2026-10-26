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
import time
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
                "updated_at TEXT, last_cmd TEXT, last_cmd_at TEXT)")
    con.execute("CREATE TABLE stream_statistics (id INTEGER PRIMARY KEY, streamer_id INTEGER, "
                "stream_started_at TEXT, stream_ended_at TEXT, platform TEXT)")
    for r in rows:
        con.execute("INSERT INTO streamers VALUES (?,?,?,?)",
                    (r["id"], r["name"], r.get("bot_on", 1), r.get("live", 0)))
        if "state" in r:
            con.execute("INSERT INTO bot_status VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["state"], r.get("detail"), r.get("send_error"),
                         r.get("is_mod", 1), r.get("heartbeat"), r.get("updated"),
                         r.get("last_cmd"), r.get("last_cmd_at")))
        if "started" in r:
            con.execute("INSERT INTO stream_statistics (streamer_id, stream_started_at, platform) "
                        "VALUES (?,?,'twitch')", (r["id"], r["started"]))
        if "ended_stream" in r:          # прошлый эфир, уже закрытый
            con.execute("INSERT INTO stream_statistics (streamer_id, stream_started_at, "
                        "stream_ended_at, platform) VALUES (?,?,?,'twitch')",
                        (r["id"], r["ended_stream"][0], r["ended_stream"][1]))
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


class Проснулся(unittest.TestCase):
    """26.10.2026, cors0vod: бот проснулся и записал «connected» в 18:44:27, первая
    отметка — через полминуты, сторож заглянул в 18:44:40 и увидел отметку
    прошлого сна. Смена состояния — тоже признак жизни."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "plgamesbot.db")
        _make_db(self.db, [
            {"id": 21, "name": "tolko_prosnulsya", "live": 0, "state": "connected",
             "heartbeat": _hb(86400), "updated": _hb(13)},
            {"id": 22, "name": "davno_molchit", "live": 0, "state": "connected",
             "heartbeat": _hb(86400), "updated": _hb(86400)},
        ])
        self.old = collect.PLGAMESBOT_DB
        collect.PLGAMESBOT_DB = self.db

    def tearDown(self):
        collect.PLGAMESBOT_DB = self.old
        self.tmp.cleanup()

    def test_только_проснулся_не_молчит(self):
        self.assertEqual(collect.Collector().plgamesbot()["no_signal"], ["davno_molchit"])


#: С 25.10.2026 бот вне эфира спит: процесса и отметок нет, и это норма.
SLEEPERS = [
    {"id": 11, "name": "spit_vne_efira", "live": 0, "state": "sleeping", "is_mod": 1,
     "heartbeat": _hb(86400)},
    {"id": 12, "name": "spit_zabanen", "live": 0, "state": "sleeping", "is_mod": 0,
     "detail": "бот забанен в этом канале — снимите бан: /unban plgames_bot",
     "heartbeat": _hb(86400)},
    # Бесплатный канал без прав модератора спит и в эфире — так решил продукт
    {"id": 13, "name": "spit_bez_moda", "live": 1, "state": "sleeping", "is_mod": 0,
     "heartbeat": _hb(86400), "started": _hb(3600)},
    {"id": 14, "name": "prospal_efir", "live": 1, "state": "sleeping", "is_mod": 1,
     "heartbeat": _hb(86400), "started": _hb(3600)},
    {"id": 15, "name": "efir_tolko_nachalsya", "live": 1, "state": "sleeping", "is_mod": 1,
     "heartbeat": _hb(86400), "started": _hb(30)},
    # Таймаут, закрытый чат — продукт выставленного бота держит спящим и в эфире
    {"id": 16, "name": "v_taimaute", "live": 1, "state": "sleeping", "is_mod": 1,
     "detail": "бот в таймауте — снимите: /untimeout plgames_bot",
     "heartbeat": _hb(86400), "started": _hb(3600)},
    {"id": 17, "name": "chat_zakryt", "live": 1, "state": "sleeping", "is_mod": 1,
     "detail": "канал закрыт для сообщений бота", "heartbeat": _hb(86400), "started": _hb(3600)},
    # «В эфире» уже стоит, а запись нового эфира ещё не заведена: прошлый закрыт
    {"id": 18, "name": "zapis_esche_net", "live": 1, "state": "sleeping", "is_mod": 1,
     "heartbeat": _hb(86400), "ended_stream": (_hb(2 * 86400), _hb(86400))},
]


class ПлановыйПерезапуск(unittest.TestCase):
    """Выкладка PLGamesBot: `restart_prod.sh` ставит отметку и делает pkill.
    Перезапуск дольше трёх проб по 4 с — упавший в эти минуты сайт не авария."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_mark = collect.PLGAMESBOT_PLANNED_MARK
        collect.PLGAMESBOT_PLANNED_MARK = os.path.join(self.tmp.name, "mark")
        self.old_targets = collect.Collector._configured_endpoints
        self.old_probe = collect.Collector._probe_endpoint
        self.old_cert = collect.Collector._cert_days_left
        collect.Collector._configured_endpoints = lambda self: [
            "https://plgamesbot.ru", "https://other.example"]
        collect.Collector._probe_endpoint = lambda self, url: {"http": 502}
        collect.Collector._cert_days_left = lambda self, url: None

    def tearDown(self):
        collect.PLGAMESBOT_PLANNED_MARK = self.old_mark
        collect.Collector._configured_endpoints = self.old_targets
        collect.Collector._probe_endpoint = self.old_probe
        collect.Collector._cert_days_left = self.old_cert
        self.tmp.cleanup()

    def test_без_отметки_сайт_лежит(self):
        self.assertIn("https://plgamesbot.ru", collect.Collector().endpoints())

    def test_с_отметкой_сайт_не_известен_а_чужой_лежит(self):
        open(collect.PLGAMESBOT_PLANNED_MARK, "w").close()
        facts = {"endpoints": collect.Collector().endpoints()}
        self.assertTrue(facts["endpoints"]["https://plgamesbot.ru"].get("planned_restart"))
        builder = delta.DeltaBuilder({"http.https://plgamesbot.ru": "ok",
                                      "http.https://other.example": "ok"})
        builder.http(facts)
        self.assertEqual([e["key"] for e in builder.events], ["https://other.example"])
        self.assertEqual(builder.bands["http.https://plgamesbot.ru"], "ok")

    def test_авария_до_выкладки_не_теряется(self):
        open(collect.PLGAMESBOT_PLANNED_MARK, "w").close()
        builder = delta.DeltaBuilder({"http.https://plgamesbot.ru": "crit"})
        builder.http({"endpoints": collect.Collector().endpoints()})
        self.assertEqual(builder.bands["http.https://plgamesbot.ru"], "crit")

    def test_старая_отметка_не_прячет(self):
        open(collect.PLGAMESBOT_PLANNED_MARK, "w").close()
        old = time.time() - collect.PLGAMESBOT_PLANNED_SEC - 60
        os.utime(collect.PLGAMESBOT_PLANNED_MARK, (old, old))
        self.assertNotIn("planned_restart", collect.Collector().endpoints()["https://plgamesbot.ru"])


class СпящийБот(unittest.TestCase):
    """26.10.2026 сторож будил владельца критом после каждого эфира: бот
    засыпал, отметки прекращались — «не подаёт признаков жизни»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "plgamesbot.db")
        _make_db(self.db, SLEEPERS)
        self.old = collect.PLGAMESBOT_DB
        collect.PLGAMESBOT_DB = self.db

    def tearDown(self):
        collect.PLGAMESBOT_DB = self.old
        self.tmp.cleanup()

    def facts(self):
        return collect.Collector().plgamesbot()

    def test_сон_вне_эфира_не_поломка(self):
        facts = self.facts()
        everywhere = set(facts["banned"]) | set(facts["nomod"]) | set(facts["no_signal"])
        self.assertNotIn("spit_vne_efira", everywhere)

    def test_спящий_забаненный_остаётся_забаненным(self):
        self.assertIn("spit_zabanen", self.facts()["banned"])

    def test_спящий_без_модератора_остаётся_безмодным(self):
        self.assertEqual(self.facts()["nomod"], ["spit_bez_moda"])

    def test_проспал_эфир_это_нет_сигнала(self):
        self.assertEqual(self.facts()["no_signal"], ["prospal_efir"])

    def test_минута_на_пробуждение_не_тревога(self):
        self.assertNotIn("efir_tolko_nachalsya", self.facts()["no_signal"])

    def test_таймаут_и_закрытый_чат_как_бан(self):
        facts = self.facts()
        self.assertIn("v_taimaute", facts["banned"])
        self.assertIn("chat_zakryt", facts["banned"])
        self.assertNotIn("v_taimaute", facts["no_signal"])

    def test_начало_эфира_только_из_открытой_записи(self):
        """Прошлый эфир позавчера — не начало этого: менеджеру ещё не дали минуты."""
        self.assertNotIn("zapis_esche_net", self.facts()["no_signal"])


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
    def test_новый_бан_не_будит_модель(self):
        """Решение владельца 18.09.2026, отменяет прежнее «бан — это крит».

        Бан снимает чужой человек в своём чате, и ни владелец, ни сторож на
        это повлиять не могут. Продукт это уже признал: забаненному каналу не
        пишут (`channel_health.py`) и в публичных списках его не показывают
        (`routers/stats.py`). Сторож остался последним, кто просил о
        невозможном, — а красный пункт, по которому не действуют, обесценивает
        соседние.

        Наша доля той же беды не потерялась: она в `advertised_silent` выше —
        канал, который мы рекламируем вопреки правилу.
        """
        was = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        now = {"plgamesbot": {"banned": ["zabanili"], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual(builder.events, [])

    def test_снятый_бан_тоже_молчит(self):
        """Симметрия: о чём не сообщали как о беде, о том не сообщают и как о
        починке. Иначе владелец получал бы «бан снят» про событие, которого
        для него не было."""
        was = {"plgamesbot": {"banned": ["zabanili"], "nomod": [], "no_signal": []}}
        now = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual(builder.events, [])

    def test_замолчавший_без_внешней_причины_бот_будит_модель(self):
        """А это наша поломка: бота никто не выгонял, он просто молчит —
        две проверки подряд."""
        was = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": [],
                              "no_signal_raw": ["molchit"]}}
        now = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": ["molchit"]}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([(e["kind"], e["key"], e["severity"]) for e in builder.events],
                         [("plgamesbot", "molchit", "crit")])

    def test_одна_проверка_не_тревога(self):
        """26.10.2026: четыре раза за вечер сторож заглядывал в полминуты между
        «проснулся» и первой отметкой. Один взгляд — не тревога; второй проход
        уже видит бота живым, и владелец ничего не получает."""
        quiet = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": [], "no_signal_raw": []}}
        glimpse = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": ["cors0vod"]}}
        builder = delta.DeltaBuilder({})
        builder.lists(quiet, glimpse, delta.ignored())
        self.assertEqual(builder.events, [])
        self.assertEqual((glimpse["plgamesbot"]["no_signal"],
                          glimpse["plgamesbot"]["no_signal_raw"]), ([], ["cors0vod"]))
        later = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(glimpse, later, delta.ignored())
        self.assertEqual(builder.events, [])                # и «вернулось» не шлём

    def test_подтверждённая_тревога_снимается_с_первого_чистого_прохода(self):
        was = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": ["molchit"],
                              "no_signal_raw": ["molchit"]}}
        still = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": ["molchit"]}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, still, delta.ignored())
        self.assertEqual(builder.events, [])                # не повторяем
        fixed = {"plgamesbot": {"banned": [], "nomod": [], "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(still, fixed, delta.ignored())
        self.assertEqual([(e["key"], e["severity"]) for e in builder.events],
                         [("molchit", "resolved")])

    def test_давно_забаненный_молчит(self):
        """Сегодняшние молчащие каналы не превращаются в крит каждые пять минут."""
        same = {"plgamesbot": {"banned": ["zabanili"], "nomod": ["bez_moda"],
                               "no_signal": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(same, same, delta.ignored())
        self.assertEqual(builder.events, [])

    def test_показ_молчащего_канала_это_крит(self):
        was = {"plgamesbot_public": {"advertised_silent": [], "unreachable": [],
                                     "advertised_silent_raw": ["zabanili"]}}
        now = {"plgamesbot_public": {"advertised_silent": ["zabanili"], "unreachable": []}}
        builder = delta.DeltaBuilder({})
        builder.lists(was, now, delta.ignored())
        self.assertEqual([(e["key"], e["severity"]) for e in builder.events],
                         [("zabanili", "crit")])

    def test_сломанная_проверка_не_крит_но_и_не_тишина(self):
        was = {"plgamesbot_public": {"advertised_silent": [], "unreachable": [],
                                     "unreachable_raw": ["/api/streams/live"]}}
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
