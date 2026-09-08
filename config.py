"""
Конфигурация. Секретов в этом файле НЕТ и быть не должно.

Единственное место, где живёт токен — env-файл вне репозитория:
    /etc/telegram-server-bot.env   (root:root, 0600)

Кто как его получает:
  * bot.py            — systemd передаёт переменные через EnvironmentFile;
  * monitor.sh (root) — читает env-файл напрямую (см. _load_env_file ниже);
  * ручной запуск     — export TELEGRAM_TOKEN=... перед стартом.

Файл читается только если переменной ещё нет в окружении, поэтому обычный
(непривилегированный) запуск не требует доступа к /etc и молча работает
на том, что дал systemd.
"""
import os

ENV_FILE = os.getenv("BOT_ENV_FILE", "/etc/telegram-server-bot.env")


def _load_env_file(path: str) -> dict:
    """Разбирает простой KEY=VALUE файл. Нечитаемый файл — не ошибка."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        # Нет прав или файла — работаем на том, что уже есть в окружении.
        pass
    return values


_file_env = _load_env_file(ENV_FILE)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or _file_env.get("TELEGRAM_TOKEN", "")
OWNER_ID = os.getenv("OWNER_ID") or _file_env.get("OWNER_ID", "")
