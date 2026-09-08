#!/usr/bin/env python3
"""Проверка правил дельты.

Логика «что считать событием» глазами не проверяется: гистерезис и baseline
проявляются только в последовательности запусков. Тест гоняет снимки цепочкой,
как это делает таймер.

Запуск: python3 watchdog/test_delta.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DELTA = os.path.join(HERE, "delta.py")

BASE = {
    "collected_at": "2026-09-07T20:00:00+00:00",
    "host": "test",
    "facts": {
        "disk": {"root": {"pct": 69, "free_gb": 58.0}},
        "memory": {"used_pct": 32, "swap_pct": 10, "total_gb": 39.0},
        "cpu": {"cores": 16, "load_per_core_pct": 13, "load1": 1.9, "load15": 1.9},
        "systemd": {"failed": []},
        "docker": {"stopped": [], "unhealthy": [], "restarting": []},
        "smart": {"/dev/sda": "PASSED"},
        "endpoints": {"https://x.ru": {"http": 200, "cert_days": 80}},
        "remote": {"впн": {"ip": "10.0.0.1", "reachable": True, "via": "напрямую",
                           "disk_pct": 40.0, "mem_pct": 30.0, "cpu_pct": 5.0}},
    },
}


def remote(**changes):
    """Состояние удалённого сервера с точечными правками."""
    base = {"ip": "10.0.0.1", "reachable": True, "via": "напрямую",
            "disk_pct": 40.0, "mem_pct": 30.0, "cpu_pct": 5.0}
    base.update(changes)
    return {"впн": base}


def snapshot(**changes):
    """Копия базового снимка с точечными правками вида disk_pct=85."""
    snap = copy.deepcopy(BASE)
    facts = snap["facts"]
    if "disk_pct" in changes:
        facts["disk"]["root"]["pct"] = changes["disk_pct"]
    if "failed" in changes:
        facts["systemd"]["failed"] = changes["failed"]
    if "stopped" in changes:
        facts["docker"]["stopped"] = changes["stopped"]
    if "http" in changes:
        facts["endpoints"]["https://x.ru"]["http"] = changes["http"]
    if "cert_days" in changes:
        facts["endpoints"]["https://x.ru"]["cert_days"] = changes["cert_days"]
    if "smart" in changes:
        facts["smart"]["/dev/sda"] = changes["smart"]
    if "remote" in changes:
        facts["remote"] = changes["remote"]
    return snap


class DeltaTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="watchdog-test-")
        self.env = dict(os.environ, WATCHDOG_STATE_DIR=self.state_dir)

    def run_delta(self, snap):
        proc = subprocess.run(
            [sys.executable, DELTA], input=json.dumps(snap),
            capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def events_of(self, snap):
        return self.run_delta(snap)["events"]

    # --- База ---------------------------------------------------------------

    def test_первый_запуск_молчит(self):
        """Установка сторожа не должна оборачиваться залпом из всего сразу."""
        result = self.run_delta(snapshot(disk_pct=95, failed=["a.service"]))
        self.assertTrue(result["baseline"])
        self.assertEqual(result["events"], [])

    def test_повтор_без_изменений_молчит(self):
        self.run_delta(snapshot())
        self.assertEqual(self.events_of(snapshot()), [])

    # --- Полосы и гистерезис ------------------------------------------------

    def test_переход_в_warn_даёт_событие(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(disk_pct=85))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "warn")
        self.assertEqual(events[0]["kind"], "disk")

    def test_рост_внутри_полосы_молчит(self):
        """81% и 85% — одна и та же новость, второй раз её не сообщаем."""
        self.run_delta(snapshot())
        self.run_delta(snapshot(disk_pct=81))
        self.assertEqual(self.events_of(snapshot(disk_pct=85)), [])

    def test_гистерезис_держит_полосу_у_порога(self):
        """78% после 85% — это дрожание у порога, а не починка."""
        self.run_delta(snapshot())
        self.run_delta(snapshot(disk_pct=85))
        self.assertEqual(self.events_of(snapshot(disk_pct=78)), [])

    def test_уверенный_спад_даёт_resolved(self):
        self.run_delta(snapshot())
        self.run_delta(snapshot(disk_pct=85))
        events = self.events_of(snapshot(disk_pct=70))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "resolved")

    def test_переход_в_crit_даёт_событие(self):
        self.run_delta(snapshot())
        self.run_delta(snapshot(disk_pct=85))
        events = self.events_of(snapshot(disk_pct=93))
        self.assertEqual(events[0]["severity"], "crit")

    # --- Списки -------------------------------------------------------------

    def test_упавший_юнит_и_его_починка(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(failed=["nginx.service"]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key"], "nginx.service")
        self.assertEqual(events[0]["severity"], "crit")

        events = self.events_of(snapshot(failed=[]))
        self.assertEqual(events[0]["severity"], "resolved")

    def test_замена_одной_поломки_другой_не_проходит_молча(self):
        """Счётчик не изменился, но упал уже другой юнит — это две новости."""
        self.run_delta(snapshot())
        self.run_delta(snapshot(failed=["a.service"]))
        events = self.events_of(snapshot(failed=["b.service"]))
        keys = {(e["key"], e["severity"]) for e in events}
        self.assertEqual(keys, {("b.service", "crit"), ("a.service", "resolved")})

    # --- Прочие источники ---------------------------------------------------

    def test_эндпоинт_упал_и_поднялся(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(http=502))
        self.assertEqual(events[0]["kind"], "http")
        self.assertEqual(events[0]["severity"], "crit")
        self.assertEqual(self.events_of(snapshot(http=200))[0]["severity"], "resolved")

    def test_сертификат_предупреждает_пока_есть_время(self):
        self.run_delta(snapshot())
        self.assertEqual(self.events_of(snapshot(cert_days=25))[0]["severity"], "warn")
        self.assertEqual(self.events_of(snapshot(cert_days=5))[0]["severity"], "crit")

    def test_smart_ловит_отказ_диска(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(smart="FAILED"))
        self.assertEqual(events[0]["severity"], "crit")

    # --- Удалённые серверы --------------------------------------------------

    def test_замолчавший_агент_это_событие(self):
        """До сторожа удалённые серверы не проверял никто: monitor.sh следит за
        локальной машиной, а прежний monitor_remote.py пропускал владельца."""
        self.run_delta(snapshot())
        events = self.events_of(snapshot(remote=remote(
            reachable=False, error="агент отвечает 503 — у агента не настроен ключ")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "remote")
        self.assertEqual(events[0]["severity"], "crit")
        self.assertIn("503", events[0]["detail"])

    def test_вернувшийся_агент_даёт_resolved(self):
        self.run_delta(snapshot())
        self.events_of(snapshot(remote=remote(reachable=False, error="молчит")))
        events = self.events_of(snapshot(remote=remote()))
        self.assertEqual(events[0]["severity"], "resolved")

    def test_метрики_удалённого_сервера_в_полосах(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(remote=remote(disk_pct=93.0)))
        self.assertEqual([(e["kind"], e["severity"]) for e in events], [("remote", "crit")])

    def test_у_молчащего_агента_метрики_не_проверяем(self):
        """Цифры от недоступного сервера — вчерашние; сообщать по ним нечего,
        кроме самой недоступности."""
        self.run_delta(snapshot())
        events = self.events_of(snapshot(remote=remote(
            reachable=False, error="молчит", disk_pct=99.0)))
        self.assertEqual(len(events), 1)
        self.assertIn("молчит", events[0]["detail"])

    # --- Дребезг ------------------------------------------------------------

    def test_дребезжащий_объект_замолкает(self):
        """Контейнер, циклящий stopped→running, будил бы модель каждые пять
        минут. После четвёртой смены он сообщает о самом дребезге и молчит."""
        self.run_delta(snapshot())
        severities = []
        for i in range(6):
            stopped = ["flappy"] if i % 2 == 0 else []
            events = self.events_of(snapshot(stopped=stopped))
            severities.append([(e["severity"], e.get("flapping", False)) for e in events])

        # Первые три смены — обычные события.
        self.assertEqual(severities[0], [("warn", False)])
        self.assertEqual(severities[1], [("resolved", False)])
        self.assertEqual(severities[2], [("warn", False)])
        # Четвёртая объявляет дребезг...
        self.assertEqual(severities[3], [("warn", True)])
        # ...после чего объект молчит.
        self.assertEqual(severities[4], [])
        self.assertEqual(severities[5], [])

    def test_событие_про_дребезг_объясняет_себя(self):
        self.run_delta(snapshot())
        event = None
        for i in range(4):
            events = self.events_of(snapshot(stopped=["flappy"] if i % 2 == 0 else []))
            if events and events[0].get("flapping"):
                event = events[0]
        self.assertIsNotNone(event)
        self.assertEqual(event["to"], "flapping")
        self.assertIn("скачет", event["detail"])

    def test_после_истечения_тишины_события_возвращаются(self):
        """Молчание временное: объект, переставший дребезжать, снова слышен."""
        self.run_delta(snapshot())
        for i in range(4):
            self.events_of(snapshot(stopped=["flappy"] if i % 2 == 0 else []))
        self.assertEqual(self.events_of(snapshot(stopped=["flappy"])), [])

        state_path = os.path.join(self.state_dir, "state.json")
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        state["flap"]["docker.flappy"]["muted_until"] = 0   # тишина истекла
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)

        events = self.events_of(snapshot(stopped=[]))
        self.assertEqual([e["severity"] for e in events], ["resolved"])

    def test_дребезг_одного_не_глушит_другого(self):
        """Замолкает конкретный объект, а не весь класс событий."""
        self.run_delta(snapshot())
        for i in range(4):
            self.events_of(snapshot(stopped=["flappy"] if i % 2 == 0 else []))
        events = self.events_of(snapshot(stopped=["flappy", "другой"]))
        self.assertEqual([(e["key"], e["severity"]) for e in events], [("другой", "warn")])

    def test_события_идут_от_тяжёлых_к_лёгким(self):
        self.run_delta(snapshot())
        events = self.events_of(snapshot(disk_pct=85, failed=["x.service"]))
        self.assertEqual(events[0]["severity"], "crit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
