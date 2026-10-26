#!/usr/bin/env python3
"""Проба сайта повторяется, прежде чем объявить его упавшим.

18.09.2026 сторож прислал красное «502 на трёх сайтах: бэкенды на 127.0.0.1:8000
и :8001 не слушают». Ни один бэкенд не падал: в 06:11 unattended-upgrades
обновил libsqlite3 и perl, а needrestart перезапустил всех их потребителей —
vkadmin (:8000) и main_app (:8001) в том числе. Окно отказа длилось пять секунд
(06:11:43-48, 12 запросов с 502 в nginx), и одиночная проба сторожа попала ровно
в него.

Это не разовое совпадение: 502 в логах есть 9, 10, 11, 14, 15, 17 и 18 сентября —
по дням автообновлений, примерно раз в неделю. Красный алерт на плановом
обслуживании обесценивает все остальные, а владелец приучается их пролистывать.

Повторяем только то, что похоже на перезапуск: сетевую ошибку и 5xx. Ответ 4xx
сервер дал осознанно, и ждать от него другого — только тянуть время снимка.
"""
import unittest
import urllib.error
import urllib.request

import collect


class ФейковыйОтвет:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ФейковыйOpener:
    """Отдаёт заготовленные исходы по очереди; последний — сколько угодно раз."""

    def __init__(self, исходы):
        self.исходы = list(исходы)
        self.проб = 0

    def open(self, req, timeout=None):
        self.проб += 1
        исход = self.исходы.pop(0) if len(self.исходы) > 1 else self.исходы[0]
        if isinstance(исход, Exception):
            raise исход
        if исход >= 400:
            raise urllib.error.HTTPError(req.full_url, исход, "", None, None)
        return ФейковыйОтвет(исход)


class ПовторнаяПроба(unittest.TestCase):
    URL = "https://plgamesbot.ru"

    def setUp(self):
        self.collector = collect.Collector()
        self.collector._configured_endpoints = lambda: [self.URL]
        self.collector._cert_days_left = lambda url: None
        self.пауза = collect.ENDPOINT_RETRY_PAUSE
        collect.ENDPOINT_RETRY_PAUSE = 0

    def tearDown(self):
        collect.ENDPOINT_RETRY_PAUSE = self.пауза

    def пробуем(self, исходы):
        opener = ФейковыйOpener(исходы)
        настоящий = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: opener
        try:
            результат = self.collector.endpoints()
        finally:
            urllib.request.build_opener = настоящий
        return результат[self.URL], opener.проб

    def test_короткий_перезапуск_не_считается_падением(self):
        entry, проб = self.пробуем([502, 200])
        self.assertEqual(entry["http"], 200)
        self.assertEqual(проб, 2)

    def test_настоящее_падение_остаётся_падением(self):
        entry, проб = self.пробуем([502])
        self.assertEqual(entry["http"], 502)
        self.assertEqual(проб, collect.ENDPOINT_TRIES)

    def test_сетевая_ошибка_тоже_ждёт_повтора(self):
        entry, проб = self.пробуем([urllib.error.URLError("refused"), 200])
        self.assertEqual(entry["http"], 200)
        self.assertEqual(проб, 2)

    def test_ответ_4xx_сервер_дал_осознанно_и_повтора_не_ждёт(self):
        entry, проб = self.пробуем([404])
        self.assertEqual(entry["http"], 404)
        self.assertEqual(проб, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
