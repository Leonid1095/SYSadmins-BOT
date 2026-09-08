#!/usr/bin/env python3
"""Проверка подписи запросов к агенту.

Смысл перехода на подпись в том, что перехват трафика перестаёт давать
многоразовый доступ. Поэтому тест в основном про то, чего сделать НЕЛЬЗЯ:
переиграть подпись на другой путь, повторить её, отправить с чужим ключом.

Запуск: python3 test_agent_auth.py
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_auth  # noqa: E402

SECRET = "правильный-ключ-агента"


def headers_for(secret=SECRET, method="GET", path="/status"):
    return agent_auth.build_headers(secret, method, path)


class AgentAuthTest(unittest.TestCase):
    def setUp(self):
        self.cache = agent_auth.NonceCache()

    def verify(self, headers, secret=SECRET, method="GET", path="/status", now=None):
        return agent_auth.verify(secret, method, path, headers, self.cache, now)

    # --- Главное свойство ---------------------------------------------------

    def test_секрет_не_уходит_в_заголовках(self):
        """Ради этого всё и затевалось: по HTTP ключ больше не передаётся."""
        headers = headers_for()
        self.assertNotIn(SECRET, " ".join(headers.values()))
        self.assertNotIn("X-Secret-Key", headers)

    # --- Норма --------------------------------------------------------------

    def test_честная_подпись_проходит(self):
        ok, reason = self.verify(headers_for())
        self.assertTrue(ok, reason)

    # --- Отказы -------------------------------------------------------------

    def test_чужой_ключ_не_проходит(self):
        ok, reason = self.verify(headers_for(secret="подсмотренный-но-неверный"))
        self.assertFalse(ok)
        self.assertIn("подпись", reason)

    def test_повтор_перехваченного_запроса_не_проходит(self):
        """Перехватчик видит подпись целиком — и всё равно не может её применить."""
        headers = headers_for()
        self.assertTrue(self.verify(headers)[0])
        ok, reason = self.verify(headers)
        self.assertFalse(ok)
        self.assertIn("Повтор", reason.capitalize())

    def test_подпись_привязана_к_пути(self):
        """Подпись от /status нельзя приложить к другому маршруту."""
        headers = headers_for(path="/status")
        ok, _ = self.verify(headers, path="/admin")
        self.assertFalse(ok)

    def test_подпись_привязана_к_методу(self):
        headers = headers_for(method="GET")
        ok, _ = self.verify(headers, method="POST")
        self.assertFalse(ok)

    def test_просроченная_подпись_не_проходит(self):
        headers = headers_for()
        later = time.time() + agent_auth.MAX_SKEW_SECONDS + 5
        ok, reason = self.verify(headers, now=later)
        self.assertFalse(ok)
        self.assertIn("просрочена", reason)

    def test_подпись_из_будущего_не_проходит(self):
        headers = headers_for()
        earlier = time.time() - agent_auth.MAX_SKEW_SECONDS - 5
        ok, _ = self.verify(headers, now=earlier)
        self.assertFalse(ok)

    def test_неподписанный_запрос_не_проходит(self):
        for headers in ({}, {"X-Timestamp": "1"}, {"X-Signature": "deadbeef"}):
            with self.subTest(headers=headers):
                ok, reason = self.verify(headers)
                self.assertFalse(ok)
                self.assertIn("не подписан", reason)

    def test_мусор_вместо_nonce_не_проходит(self):
        headers = headers_for()
        for bad in ("", "ZZZ", "../../etc", "a" * 31, "0" * 100):
            with self.subTest(nonce=bad):
                spoiled = dict(headers, **{agent_auth.NONCE_HEADER: bad})
                self.assertFalse(self.verify(spoiled)[0])

    def test_мусор_вместо_времени_не_проходит(self):
        headers = dict(headers_for(), **{agent_auth.TIMESTAMP_HEADER: "позавчера"})
        ok, reason = self.verify(headers)
        self.assertFalse(ok)
        self.assertIn("метка времени", reason)

    # --- Кэш nonce ----------------------------------------------------------

    def test_кэш_не_растёт_бесконечно(self):
        """Поток запросов не должен выедать память агента: это был бы отказ
        в обслуживании ценой одного цикла curl."""
        for _ in range(20000):
            self.cache.check_and_store(os.urandom(16).hex())
        self.assertLessEqual(len(self.cache._seen), self.cache.MAX_ENTRIES)

    def test_переполнение_кэша_выбрасывает_самые_старые(self):
        """Вытесняются те, чьи подписи и так ближе всех к истечению."""
        свежий = os.urandom(16).hex()
        self.cache.check_and_store(свежий, now=time.time())
        for _ in range(self.cache.MAX_ENTRIES * 2):
            self.cache.check_and_store(os.urandom(16).hex(), now=time.time() + 1)
        self.assertNotIn(свежий, self.cache._seen)
        self.assertLessEqual(len(self.cache._seen), self.cache.MAX_ENTRIES)

    def test_чужой_nonce_не_вытесняет_настоящий_до_проверки_подписи(self):
        """Неверная подпись не должна засорять кэш: иначе прохожий вытеснял бы
        оттуда настоящие значения и открывал окно для повтора."""
        headers = headers_for()
        spoiled = dict(headers, **{agent_auth.SIGNATURE_HEADER: "00" * 32})
        self.assertFalse(self.verify(spoiled)[0])
        self.assertNotIn(headers[agent_auth.NONCE_HEADER], self.cache._seen)
        self.assertTrue(self.verify(headers)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
