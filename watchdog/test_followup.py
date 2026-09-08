#!/usr/bin/env python3
"""Проверка дожима.

Тут важны две вещи, и обе — про молчание. Первая: дожим обязан замолкать по
каждому из трёх поводов (владелец отреагировал, поломка ушла, лимит исчерпан),
потому что напоминание, которое не умеет прекращаться, — это спам с таймером.
Вторая: он обязан заговорить, когда крит висит без ответа, иначе всей затеи
нет. Между ними — расписание, и его проверяем по часам, а не «примерно».

Запуск: python3 watchdog/test_followup.py
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import followup  # noqa: E402

T0 = 1_700_000_000.0   # момент инцидента во всех тестах


def make_incident(root, name="20260908T120000Z", severity="crit", events=None,
                  facts=None, created=T0):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    events = events if events is not None else [{
        "kind": "disk", "key": "root", "from": "warn", "to": "crit",
        "severity": "crit", "slot": "disk.root.pct", "detail": "занято 94%",
    }]
    verdict = {
        "analysed": True,
        "incident": name,
        "incident_dir": path,
        "created_at": "2023-11-14T22:13:20+00:00",
        "events": events,
        "verdict": {
            "severity": severity,
            "headline": "Диск заканчивается",
            "explanation": "Осталось мало места.",
            "options": [{"action": "nothing", "target": None,
                         "label": "Ничего не делать", "why": "Принять к сведению."}],
        },
    }
    with open(os.path.join(path, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump(verdict, f)
    with open(os.path.join(path, "facts.json"), "w", encoding="utf-8") as f:
        json.dump({"facts": facts if facts is not None else {"disk": {"root": {"pct": 94}}}}, f)
    # Момент создания берётся из created_at; mtime — запасной путь.
    os.utime(os.path.join(path, "verdict.json"), (created, created))
    return path


def track_of(path):
    with open(os.path.join(path, "followup.json"), encoding="utf-8") as f:
        return json.load(f)


class Sent(list):
    """Подменяет отправку и запоминает, что ушло бы владельцу."""

    def __call__(self, token, owner, text, reply_to=None, keyboard=None):
        self.append({"text": text, "reply_to": reply_to, "keyboard": keyboard})
        return 1000 + len(self)


class FollowupTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.sent = Sent()
        patch = mock.patch.object(followup, "send", self.sent)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, path, at, bands=None, facts=None):
        return followup.process(
            path,
            bands if bands is not None else {"disk.root.pct": "crit"},
            facts if facts is not None else {"disk": {"root": {"pct": 96}}},
            at, "token", "owner")

    # --- Расписание ---------------------------------------------------------

    def test_раньше_срока_молчит(self):
        path = make_incident(self.root)
        self.assertEqual(self.run_once(path, T0 + 9 * 60), "ждём")
        self.assertEqual(self.sent, [])

    def test_через_десять_минут_напоминает(self):
        path = make_incident(self.root)
        self.assertEqual(self.run_once(path, T0 + 10 * 60), "напомнили")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Без ответа 10 мин", self.sent[0]["text"])

    def test_вторая_пауза_длиннее_первой(self):
        """10 мин → 30 мин: через 20 минут после первого напоминания рано."""
        path = make_incident(self.root)
        self.run_once(path, T0 + 10 * 60)
        self.assertEqual(self.run_once(path, T0 + 30 * 60), "ждём")
        self.assertEqual(self.run_once(path, T0 + 40 * 60), "напомнили")
        self.assertEqual(len(self.sent), 2)

    def test_не_больше_шести_напоминаний(self):
        path = make_incident(self.root)
        at = T0
        for pause in followup.SCHEDULE:
            at += pause
            self.assertEqual(self.run_once(path, at), "напомнили")
        self.assertEqual(len(self.sent), len(followup.SCHEDULE))

        # Дальше — тишина, сколько бы времени ни прошло.
        self.assertEqual(self.run_once(path, at + 10 * 86400), "exhausted")
        self.assertEqual(len(self.sent), len(followup.SCHEDULE))

    def test_последнее_напоминание_предупреждает_что_оно_последнее(self):
        path = make_incident(self.root)
        at = T0
        for pause in followup.SCHEDULE:
            at += pause
            self.run_once(path, at)
        self.assertIn("последнее напоминание", self.sent[-1]["text"])
        self.assertNotIn("последнее напоминание", self.sent[0]["text"])

    # --- Поводы замолчать ---------------------------------------------------

    def test_нажатие_кнопки_прекращает_дожим(self):
        path = make_incident(self.root)
        with open(os.path.join(path, "choices.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"choice": "nothing"}\n')
        self.assertEqual(self.run_once(path, T0 + 3600), "acknowledged")
        self.assertEqual(self.sent, [])

    def test_вопрос_аналитику_тоже_считается_ответом(self):
        path = make_incident(self.root)
        with open(os.path.join(path, "dialogue.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"q": "что это?", "a": "вот это"}\n')
        self.assertEqual(self.run_once(path, T0 + 3600), "acknowledged")
        self.assertEqual(self.sent, [])

    def test_пустой_файл_ответом_не_считается(self):
        """Пустой choices.jsonl мог остаться от неудачной записи."""
        path = make_incident(self.root)
        open(os.path.join(path, "choices.jsonl"), "w").close()
        self.assertEqual(self.run_once(path, T0 + 10 * 60), "напомнили")

    def test_поломка_ушла_сама_закрывает_инцидент(self):
        path = make_incident(self.root)
        result = self.run_once(path, T0 + 10 * 60,
                               bands={"disk.root.pct": "ok"},
                               facts={"disk": {"root": {"pct": 40}}})
        self.assertEqual(result, "closed")
        self.assertEqual(track_of(path)["state"], "closed")

    def test_закрытие_без_единого_напоминания_молчит(self):
        """Про возврат в норму владелец и так получит обычный разбор.
        Второе сообщение об одном и том же — снова спам."""
        path = make_incident(self.root)
        self.run_once(path, T0 + 60, bands={"disk.root.pct": "ok"}, facts={})
        self.assertEqual(self.sent, [])

    def test_закрытие_после_напоминаний_сообщается(self):
        path = make_incident(self.root)
        self.run_once(path, T0 + 10 * 60)
        self.run_once(path, T0 + 3600, bands={"disk.root.pct": "ok"},
                      facts={"disk": {"root": {"pct": 40}}})
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Закрылось само", self.sent[-1]["text"])

    def test_старый_инцидент_не_дожимается_задним_числом(self):
        """Окно напоминаний прошло целиком — начинать его поздно.

        Ровно этот случай нашёлся на живых данных перед включением: два крита
        с прошлой ночи получили бы по шесть напоминаний о том, что владелец
        уже знает."""
        path = make_incident(self.root)
        self.assertEqual(self.run_once(path, T0 + followup.STALE_AFTER + 60),
                         "просрочен")
        self.assertEqual(self.sent, [])
        self.assertEqual(track_of(path)["state"], "stale")

    def test_начатый_вовремя_дожим_доводится_до_конца(self):
        """Просрочка судит только по первому взгляду: если напоминания уже
        пошли, они идут по расписанию, даже когда общий срок вышел."""
        path = make_incident(self.root)
        at = T0
        for pause in followup.SCHEDULE:
            at += pause
            self.assertEqual(self.run_once(path, at), "напомнили")
        self.assertEqual(len(self.sent), len(followup.SCHEDULE))

    def test_warn_не_дожимается(self):
        """Дожим только для крита: напоминать о жёлтом — вернуть спам."""
        path = make_incident(self.root, severity="warn")
        self.assertEqual(self.run_once(path, T0 + 10 * 3600), "не крит")
        self.assertEqual(self.sent, [])

    # --- Что считается «всё ещё сломано» ------------------------------------

    def test_состояние_берётся_из_полос(self):
        event = {"kind": "disk", "key": "root", "slot": "disk.root.pct", "severity": "crit"}
        self.assertTrue(followup.still_broken(event, {"disk.root.pct": "crit"}, {}))
        self.assertFalse(followup.still_broken(event, {"disk.root.pct": "ok"}, {}))

    def test_списочные_события_проверяются_по_фактам(self):
        """У упавшего юнита нет полосы — есть присутствие в списке failed."""
        event = {"kind": "systemd", "key": "foo.service",
                 "list_field": "failed", "severity": "crit"}
        self.assertTrue(followup.still_broken(
            event, {}, {"systemd": {"failed": ["foo.service"]}}))
        self.assertFalse(followup.still_broken(
            event, {}, {"systemd": {"failed": []}}))

    def test_событие_без_адреса_считается_незакрытым(self):
        """Инциденты, записанные до появления адресов. Молчание опаснее
        лишнего напоминания, поэтому трактуем в пользу напоминания."""
        event = {"kind": "disk", "key": "root", "severity": "crit"}
        self.assertTrue(followup.still_broken(event, {}, {}))

    def test_событие_о_возврате_в_норму_не_держит_инцидент(self):
        event = {"kind": "disk", "key": "root", "severity": "resolved",
                 "slot": "disk.root.pct"}
        self.assertFalse(followup.still_broken(event, {"disk.root.pct": "crit"}, {}))

    # --- Адресация значений -------------------------------------------------

    def test_адрес_находит_значение_в_снимке(self):
        facts = {
            "disk": {"root": {"pct": 91}},
            "memory": {"used_pct": 80, "swap_pct": 65},
            "smart": {"/dev/sda": "PASSED"},
            "endpoints": {"https://a.b.c/x": {"http": 502, "cert_days": 12}},
            "remote": {"DE сервер": {"reachable": False, "disk_pct": 70}},
        }
        cases = [
            ("disk.root.pct", 91),
            ("memory.memory.used_pct", 80),
            ("memory.memory.swap_pct", 65),
            ("smart./dev/sda", "PASSED"),
            ("http.https://a.b.c/x", 502),
            ("cert.https://a.b.c/x", 12),
            ("remote.DE сервер.reachable", False),
            ("remote.DE сервер.disk_pct", 70),
        ]
        for slot, expected in cases:
            with self.subTest(slot=slot):
                self.assertEqual(followup.value_at(slot, facts)[0], expected)

    def test_адрес_с_точками_в_url_не_разваливается(self):
        """URL внутри адреса — единственная причина, по которой адрес нельзя
        просто разрезать по точкам."""
        facts = {"endpoints": {"https://plg-messedge.duckdns.org/": {"cert_days": 5}}}
        value, field = followup.value_at("cert.https://plg-messedge.duckdns.org/", facts)
        self.assertEqual((value, field), (5, "cert_days"))

    def test_исчезнувший_объект_не_роняет_разбор(self):
        self.assertEqual(followup.value_at("disk.root.pct", {}), (None, None))
        self.assertEqual(followup.value_at("ерунда", {}), (None, None))

    # --- Что именно сдвинулось ----------------------------------------------

    def test_движение_показывает_было_стало(self):
        event = {"kind": "disk", "key": "root", "slot": "disk.root.pct"}
        text = followup.movement(event, {"disk": {"root": {"pct": 94}}},
                                 {"disk": {"root": {"pct": 96}}})
        self.assertIn("было 94%", text)
        self.assertIn("сейчас 96%", text)

    def test_движение_замечает_что_ничего_не_изменилось(self):
        event = {"kind": "disk", "key": "root", "slot": "disk.root.pct"}
        text = followup.movement(event, {"disk": {"root": {"pct": 94}}},
                                 {"disk": {"root": {"pct": 94}}})
        self.assertIn("без изменений", text)

    def test_движение_замечает_что_контейнер_поднялся_сам(self):
        event = {"kind": "docker", "key": "svod-bot-1", "list_field": "stopped"}
        text = followup.movement(event, {}, {"docker": {"stopped": []}})
        self.assertIn("вернулся в норму сам", text)

    # --- Форма сообщения ----------------------------------------------------

    def test_напоминание_экранирует_содержимое(self):
        """Заголовок пишет модель, читавшая логи, куда пишет посторонний."""
        path = make_incident(self.root)
        with open(os.path.join(path, "verdict.json"), encoding="utf-8") as f:
            result = json.load(f)
        result["verdict"]["headline"] = '<b>злой</b> & "тег"'
        text = followup.render_reminder(result, ["диск <script>"], 600, False)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<b>злой</b>", text)
        self.assertIn("&lt;b&gt;злой", text)

    def test_напоминание_несёт_те_же_кнопки(self):
        """Смысл дожима — не «мы всё ещё здесь», а возможность починить."""
        path = make_incident(self.root)
        self.run_once(path, T0 + 10 * 60)
        keyboard = self.sent[0]["keyboard"]
        self.assertTrue(keyboard["inline_keyboard"])
        self.assertTrue(keyboard["inline_keyboard"][0][0]["callback_data"]
                        .startswith("wd:20260908T120000Z:"))

    def test_напоминание_уходит_веткой_к_исходному_сообщению(self):
        path = make_incident(self.root)
        with open(os.path.join(path, "message.json"), "w", encoding="utf-8") as f:
            json.dump({"message_id": 777}, f)
        self.run_once(path, T0 + 10 * 60)
        self.assertEqual(self.sent[0]["reply_to"], 777)

    def test_время_словами(self):
        self.assertEqual(followup.humanize(600), "10 мин")
        self.assertEqual(followup.humanize(3600), "1 ч")
        self.assertEqual(followup.humanize(3600 + 600), "1 ч 10 мин")
        self.assertEqual(followup.humanize(2 * 86400), "2 сут")

    # --- Устойчивость -------------------------------------------------------

    def test_битый_вердикт_не_роняет_проход(self):
        path = os.path.join(self.root, "20260908T130000Z")
        os.makedirs(path)
        with open(os.path.join(path, "verdict.json"), "w", encoding="utf-8") as f:
            f.write("{это не json")
        self.assertEqual(self.run_once(path, T0 + 3600), "нет вердикта")

    def test_недоставленное_напоминание_не_считается_отправленным(self):
        """Иначе отвалившийся мост «съел» бы весь лимит за один проход."""
        path = make_incident(self.root)
        with mock.patch.object(followup, "send", return_value=None):
            self.assertEqual(self.run_once(path, T0 + 10 * 60), "не доставлено")
        self.assertFalse(os.path.exists(os.path.join(path, "followup.json")))
        self.assertEqual(self.run_once(path, T0 + 10 * 60), "напомнили")


if __name__ == "__main__":
    unittest.main(verbosity=2)
