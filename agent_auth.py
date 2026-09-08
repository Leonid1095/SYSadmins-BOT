"""Подпись запросов к агенту. Общий модуль для обеих сторон.

Раньше клиент присылал сам секрет в заголовке `X-Secret-Key`. Агент отдаёт
метрики по обычному HTTP, поэтому любой, кто видел трафик — провайдер, транзит,
владелец Wi-Fi, — получал готовый ключ и мог опрашивать сервер сколько угодно.
TLS на разбросанных серверах поднять не всегда возможно, поэтому убрана сама
причина: секрет больше не передаётся.

Вместо него уходит подпись HMAC-SHA256 от метода, пути, времени и разового
числа. Перехватчик получает подпись, годную ровно для этого запроса и ровно на
две минуты, а повторить её мешает запомненный nonce. Ключ при этом не покидает
машину.

Что это НЕ чинит: сами метрики по-прежнему идут открытым текстом, и наблюдатель
видит загрузку CPU и диска. Это заметно менее ценно, чем ключ, дающий доступ
навсегда, но если сервер стоит за доменом — TLS всё равно лучше.
"""

import hashlib
import hmac
import os
import time

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
NONCE_HEADER = "X-Nonce"

# Окно, в котором подпись считается свежей. Две минуты — запас на расхождение
# часов между машинами; больше давать незачем, это окно для повтора.
MAX_SKEW_SECONDS = 120
NONCE_BYTES = 16


def canonical(method, path, timestamp, nonce):
    """Строка, которую подписывают обе стороны.

    Метод и путь входят в подпись, чтобы перехваченную подпись нельзя было
    приложить к другому запросу; перевод строки как разделитель не встречается
    ни в одном из полей.
    """
    return "\n".join((method.upper(), path, str(timestamp), nonce))


def sign(secret, method, path, timestamp, nonce):
    return hmac.new(
        secret.encode("utf-8"),
        canonical(method, path, timestamp, nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_headers(secret, method, path):
    """Заголовки для исходящего запроса."""
    timestamp = int(time.time())
    nonce = os.urandom(NONCE_BYTES).hex()
    return {
        TIMESTAMP_HEADER: str(timestamp),
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: sign(secret, method, path, timestamp, nonce),
    }


class NonceCache:
    """Помнит недавние nonce, чтобы подпись нельзя было повторить.

    Хранит только те, что ещё внутри окна свежести: за его пределами запрос
    отбраковывается по времени, и помнить его больше незачем.

    Размер ограничен жёстко. Чистки по возрасту недостаточно: поток запросов
    успевает набить кэш быстрее, чем записи стареют, и агент выел бы память —
    то есть отказ в обслуживании ценой одного цикла curl. При переполнении
    выбрасываем самые старые: их подписи и так ближе всех к истечению.
    """

    MAX_ENTRIES = 4096

    def __init__(self, window=MAX_SKEW_SECONDS):
        self.window = window
        self._seen = {}

    def _make_room(self, cutoff):
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if len(self._seen) >= self.MAX_ENTRIES:
            keep = sorted(self._seen.items(), key=lambda kv: kv[1])
            self._seen = dict(keep[-(self.MAX_ENTRIES // 2):])

    def check_and_store(self, nonce, now=None):
        now = now if now is not None else time.time()
        cutoff = now - self.window * 2
        if len(self._seen) >= self.MAX_ENTRIES:
            self._make_room(cutoff)
        if self._seen.get(nonce, 0) >= cutoff:
            return False
        self._seen[nonce] = now
        return True


def verify(secret, method, path, headers, nonce_cache, now=None):
    """Проверяет подпись. Возвращает (ок, причина отказа).

    Порядок проверок — от дешёвых к дорогим, но сравнение подписи всегда
    константное по времени: иначе по задержке ответа её можно подобрать.
    """
    now = now if now is not None else time.time()

    timestamp = headers.get(TIMESTAMP_HEADER, "")
    nonce = headers.get(NONCE_HEADER, "")
    provided = headers.get(SIGNATURE_HEADER, "")
    if not (timestamp and nonce and provided):
        return False, "запрос не подписан"

    try:
        ts = int(timestamp)
    except ValueError:
        return False, "некорректная метка времени"
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return False, "подпись просрочена или из будущего"

    if len(nonce) != NONCE_BYTES * 2 or not all(c in "0123456789abcdef" for c in nonce):
        return False, "некорректный nonce"

    expected = sign(secret, method, path, ts, nonce)
    if not hmac.compare_digest(expected, provided):
        return False, "подпись не совпала"

    # Nonce запоминаем последним: до проверки подписи любой прохожий мог бы
    # засорять кэш и вытеснять из него настоящие значения.
    if not nonce_cache.check_and_store(nonce, now):
        return False, "повтор запроса"

    return True, ""
