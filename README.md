# telegram-server-bot

Телеграм-бот для мониторинга серверов и проектов с проактивными алертами.

## Архитектура

| Компонент | Где работает | Назначение |
|-----------|--------------|------------|
| `bot.py` | центральный сервер | Telegram-бот: добавление серверов, статус по кнопке, настройки алертов |
| `agent.py` | на каждом наблюдаемом сервере | Flask/Gunicorn API `:5000/status` — отдаёт метрики (CPU/RAM/диск/GPU) |
| `monitor.sh` | центральный сервер, cron `*/5` | Проактивный мониторинг **локального** сервера и всех его проектов (Docker, systemd, HTTP, SMART, SSL). Алерты — владельцу |
| `monitor_remote.py` | вызывается из `monitor.sh` | Проверяет серверы **других** пользователей через их агентов |

Данные: `users.json` (серверы пользователей), `monitor_subscribers.json` (подписки/пороги) — оба в `.gitignore`.

## Установка бота

```bash
git clone <repo> && cd telegram-server-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env            # или отредактируйте config.py
# впишите TELEGRAM_TOKEN (новый, от @BotFather) и OWNER_ID
```

`config.py` (в `.gitignore`) — единый источник токена и `OWNER_ID` для всех компонентов.

### systemd-сервис бота

```ini
# /etc/systemd/system/telegram-server-bot.service
[Service]
User=plg
WorkingDirectory=/home/plg/telegram-server-bot
ExecStart=/home/plg/telegram-server-bot/venv/bin/python bot.py
Restart=always
```

### cron для проактивного монитора

```cron
*/5 * * * * /home/plg/telegram-server-bot/monitor.sh
```

Без этой строки алерты **не приходят** — `monitor.sh` не запускается сам.

## Установка агента на наблюдаемый сервер

В боте: «Мои серверы» → выбрать сервер → «Показать инструкцию». Команда вида:

```bash
wget -qO- https://raw.githubusercontent.com/Leonid1095/SYSadmins-BOT/main/install.sh | bash -s -- --key <КЛЮЧ>
```

Агент ставится как `bot-agent.service`, слушает `:5000`, отвечает только на запросы с корректным `X-Secret-Key`.

> ⚠️ Агент отдаёт метрики по HTTP. Ограничьте доступ к порту 5000 фаерволом
> (только IP центрального сервера) — ключ передаётся в открытом виде.

## Что мониторит monitor.sh

- **Ресурсы**: диск `/`, HDD бэкапов, RAM, SWAP, CPU (утилизация + load average), GPU (темп./нагрузка)
- **Все проекты автоматически**:
  - все Docker-контейнеры — алерт, если не `running` или `unhealthy`
  - все systemd-юниты в состоянии `failed` + список критичных сервисов
  - HTTP-эндпоинты из `monitor.local.conf` (алерт при 000/5xx)
- **Железо/безопасность**: SMART диска, массовые баны fail2ban, истечение SSL

Пороги и цели настраиваются в `monitor.local.conf` (см. `monitor.local.conf.example`).

## Безопасность

- Токен и данные пользователей — только в `.gitignore`-файлах, не в репозитории.
- Агент отклоняет запросы без верного ключа (constant-time сравнение).
- Бот принимает только публичные IPv4 (защита от SSRF во внутреннюю сеть).
