#!/usr/bin/env python3
"""Проверка каталога починок.

Ценность каталога не в том, что он умеет запускать, а в том, что он отказывается
запускать. Поэтому тест почти целиком про отказы: инъекция в имя цели, запретный
юнит, цель у действия, которое цели не принимает, действие не из списка.

Запуск: python3 watchdog/test_catalog.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402


class CatalogTest(unittest.TestCase):

    # --- Отказы: форма цели -------------------------------------------------

    def test_инъекция_в_имя_юнита_отвергнута(self):
        for target in ("; rm -rf /", "nginx.service && curl evil.sh",
                       "$(whoami).service", "../../etc/passwd", "nginx.service\nrm -rf /"):
            with self.subTest(target=target):
                with self.assertRaises(catalog.RemedyError):
                    catalog.resolve("restart_unit", target)

    def test_инъекция_в_имя_контейнера_отвергнута(self):
        for target in ("../../etc", "a; docker rm -f $(docker ps -aq)", "-v/:/host"):
            with self.subTest(target=target):
                with self.assertRaises(catalog.RemedyError):
                    catalog.resolve("restart_container", target)

    def test_пустая_цель_отвергнута(self):
        with self.assertRaises(catalog.RemedyError):
            catalog.resolve("restart_unit", None)

    # --- Отказы: постоянный запрет ------------------------------------------

    def test_запретные_юниты_не_трогаем_даже_если_они_упали(self):
        """Рестарт ssh при сетевой поломке — это потеря доступа к машине."""
        with mock.patch.object(catalog, "failed_units",
                               return_value={"ssh.service", "xray-bridge.service",
                                             "systemd-networkd.service", "fail2ban.service"}):
            for target in ("ssh.service", "xray-bridge.service",
                           "systemd-networkd.service", "fail2ban.service"):
                with self.subTest(target=target):
                    with self.assertRaises(catalog.RemedyError) as ctx:
                        catalog.resolve("restart_unit", target)
                    self.assertIn("запрет", str(ctx.exception))

    # --- Отказы: несоответствие живому состоянию ----------------------------

    def test_целый_юнит_перезапускать_нечего(self):
        """Между советом модели и нажатием кнопки юнит мог подняться сам."""
        with mock.patch.object(catalog, "failed_units", return_value=set()):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("restart_unit", "nginx.service")
            self.assertIn("не в состоянии failed", str(ctx.exception))

    def test_целый_контейнер_перезапускать_нечего(self):
        with mock.patch.object(catalog, "broken_containers", return_value=set()):
            with self.assertRaises(catalog.RemedyError):
                catalog.resolve("restart_container", "svod-bot-1")

    # --- Отказы: новые действия (фаза 11) -----------------------------------

    def test_поднимать_можно_только_остановленный_контейнер(self):
        """Работающий контейнер трогать незачем, а restarting уже пытается сам."""
        rows = {"svod-bot-1": ("running", "Up 2 hours")}
        with mock.patch.object(catalog, "_container_rows", return_value=rows):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("start_container", "svod-bot-1")
            self.assertIn("работает", str(ctx.exception))

    def test_отказ_называет_настоящую_причину(self):
        """Неверный диагноз владелец принимает за факт — сегодня это уже
        стоило ложного «у агента не настроен ключ». Отказ про завершившуюся
        разовую задачу не должен выглядеть как отказ про работающий контейнер."""
        rows = {"svod-migrate-1": ("exited", "Exited (0) 6 days ago")}
        with mock.patch.object(catalog, "_container_rows", return_value=rows), \
             mock.patch.object(catalog, "ignored_containers", return_value=[]):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("start_container", "svod-migrate-1")
        self.assertIn("кодом 0", str(ctx.exception))
        self.assertNotIn("работает", str(ctx.exception))

    def test_несуществующий_контейнер_так_и_называется(self):
        with mock.patch.object(catalog, "_container_rows", return_value={}), \
             mock.patch.object(catalog, "ignored_containers", return_value=[]):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("start_container", "no-such-container")
        self.assertIn("нет", str(ctx.exception))

    def test_контейнер_из_исключений_не_поднимаем(self):
        """Комнаты civ4col гасятся сами; поднять такую — спорить с её
        собственным устройством."""
        with mock.patch.object(catalog, "stopped_containers",
                               return_value={"civ4col-pitboss5"}), \
             mock.patch.object(catalog, "ignored_containers",
                               return_value=["civ4col-pitboss*"]):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("start_container", "civ4col-pitboss5")
            self.assertIn("штатно", str(ctx.exception))

    def test_разовая_задача_не_поднимается_повторно(self):
        """Вышел с кодом 0 — не сломался, а закончил. На этой машине под
        описание попадает svod-migrate-1: «поднять» её значит прогнать
        миграцию базы второй раз."""
        rows = {"svod-migrate-1": ("exited", "Exited (0) 6 days ago"),
                "svod-bot-1": ("exited", "Exited (137) 2 hours ago")}
        with mock.patch.object(catalog, "_container_rows", return_value=rows):
            self.assertEqual(catalog.stopped_containers(), {"svod-bot-1"})

    def test_перезапускающийся_контейнер_не_трогаем(self):
        """Он уже пытается подняться сам."""
        rows = {"c": ("restarting", "Restarting (1) 5 seconds ago")}
        with mock.patch.object(catalog, "_container_rows", return_value=rows):
            self.assertEqual(catalog.stopped_containers(), set())
            # Но как «сломанный» он виден: рестарт — другое действие.
            self.assertEqual(catalog.broken_containers(), {"c"})

    def test_инъекция_в_имя_поднимаемого_контейнера_отвергнута(self):
        for target in ("; docker run -v /:/host alpine", "../../etc", "-v/:/host"):
            with self.subTest(target=target):
                with self.assertRaises(catalog.RemedyError):
                    catalog.resolve("start_container", target)

    def test_отметку_снимаем_только_с_упавшего_юнита(self):
        with mock.patch.object(catalog, "failed_units", return_value=set()):
            with self.assertRaises(catalog.RemedyError) as ctx:
                catalog.resolve("reset_failed", "nginx.service")
            self.assertIn("снимать нечего", str(ctx.exception))

    def test_запретные_юниты_защищены_и_от_снятия_отметки(self):
        """Запрет должен держать все действия над юнитом, а не только рестарт:
        иначе новое действие тихо обходит старую защиту."""
        with mock.patch.object(catalog, "failed_units",
                               return_value={"ssh.service", "xray-bridge.service"}):
            for target in ("ssh.service", "xray-bridge.service"):
                with self.subTest(target=target):
                    with self.assertRaises(catalog.RemedyError) as ctx:
                        catalog.resolve("reset_failed", target)
                    self.assertIn("запрет", str(ctx.exception))

    def test_действия_без_цели_её_не_принимают(self):
        for action in ("rotate_logs", "renew_certs", "vacuum_journal", "prune_builder"):
            with self.subTest(action=action):
                with self.assertRaises(catalog.RemedyError):
                    catalog.resolve(action, "/var/log/nginx/error.log")

    # --- Разрешения: новые действия -----------------------------------------

    def test_остановленный_контейнер_поднимается(self):
        with mock.patch.object(catalog, "stopped_containers", return_value={"svod-bot-1"}), \
             mock.patch.object(catalog, "ignored_containers", return_value=[]):
            cmd = catalog.resolve("start_container", "svod-bot-1")
        # Именно start: compose up умеет тянуть образы и пересоздавать контейнер,
        # а это уже другой класс полномочий.
        self.assertEqual(cmd, ["docker", "start", "--", "svod-bot-1"])
        self.assertNotIn("compose", " ".join(cmd))

    def test_отметка_об_аварии_снимается(self):
        with mock.patch.object(catalog, "failed_units", return_value={"report_bot.service"}):
            cmd = catalog.resolve("reset_failed", "report_bot.service")
        self.assertEqual(cmd, ["systemctl", "reset-failed", "--", "report_bot.service"])

    def test_ротация_и_продление_целей_не_требуют(self):
        """Действие без цели невозможно направить не туда — это и есть
        причина, по которой три из четырёх новых её не принимают."""
        self.assertEqual(catalog.resolve("rotate_logs"), ["logrotate", "-f", "/etc/logrotate.conf"])
        self.assertEqual(catalog.resolve("renew_certs"), ["certbot", "renew"])

    def test_у_продления_свой_потолок_времени(self):
        """Общий потолок в 120 с продлению мал: оно ходит наружу по каждому
        домену отдельно."""
        self.assertGreater(catalog.ACTIONS["renew_certs"]["timeout"], catalog.CMD_TIMEOUT)

    def test_исключения_читаются_из_root_овского_файла(self):
        with mock.patch.object(catalog, "REMEDY_CONF", "/такого/файла/нет"):
            self.assertEqual(catalog.ignored_containers(),
                             list(catalog.DEFAULT_DOCKER_IGNORE))

    # --- Отказы: несуществующее действие ------------------------------------

    def test_действие_вне_каталога_отвергнуто(self):
        for action in ("rm_rf", "exec", "restart_unit; rm -rf /", "", "__import__"):
            with self.subTest(action=action):
                with self.assertRaises(catalog.RemedyError):
                    catalog.resolve(action, "nginx.service")

    def test_лишняя_цель_отвергнута(self):
        with self.assertRaises(catalog.RemedyError):
            catalog.resolve("prune_builder", "что-нибудь")

    # --- Разрешения ---------------------------------------------------------

    def test_упавший_юнит_перезапускается(self):
        with mock.patch.object(catalog, "failed_units", return_value={"report_bot.service"}):
            cmd = catalog.resolve("restart_unit", "report_bot.service")
        self.assertEqual(cmd, ["systemctl", "restart", "--", "report_bot.service"])

    def test_сломанный_контейнер_перезапускается(self):
        with mock.patch.object(catalog, "broken_containers", return_value={"svod-bot-1"}):
            cmd = catalog.resolve("restart_container", "svod-bot-1")
        self.assertEqual(cmd, ["docker", "restart", "--", "svod-bot-1"])

    def test_ничего_не_делать_ничего_не_запускает(self):
        self.assertIsNone(catalog.resolve("nothing"))

    def test_команды_собираются_списком_без_shell(self):
        """Ни один построитель не должен возвращать строку: строка означала бы
        shell, а shell означал бы, что имя цели снова становится кодом."""
        with mock.patch.object(catalog, "failed_units", return_value={"a.service"}), \
             mock.patch.object(catalog, "broken_containers", return_value={"c"}), \
             mock.patch.object(catalog, "stopped_containers", return_value={"c"}), \
             mock.patch.object(catalog, "ignored_containers", return_value=[]):
            for action_id, target in (("restart_unit", "a.service"),
                                      ("restart_container", "c"),
                                      ("prune_builder", None),
                                      ("vacuum_journal", None),
                                      ("reset_failed", "a.service"),
                                      ("rotate_logs", None),
                                      ("renew_certs", None)):
                with self.subTest(action_id=action_id):
                    cmd = catalog.resolve(action_id, target)
                    self.assertIsInstance(cmd, list)
                    self.assertTrue(all(isinstance(part, str) for part in cmd))

    def test_каталог_для_модели_не_раскрывает_команд(self):
        """Модель видит смысл действий, но не то, во что они разворачиваются."""
        text = catalog.describe_for_model()
        for leak in ("systemctl", "docker builder", "journalctl", "--vacuum",
                     "reset-failed", "docker start", "logrotate", "certbot",
                     "/etc/logrotate.conf"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
