# Переименование проекта в «PLGames Admin»

Каталог переименовывается **не сам по себе**: путь `/home/plg/telegram-server-bot`
зашит в трёх установленных systemd-юнитах и в root-кроне. Просто `mv` оставит
неработающий бот и молчащий мониторинг.

## Про имя с пробелом

«PLGames Admin» как имя каталога создаёт проблемы на ровном месте:

* `ExecStart=` в systemd без кавычек ломается на пробеле;
* строка cron рвётся по пробелу;
* `$(dirname "$0")` в скриптах требует кавычек везде, где сейчас их может не быть.

**Рекомендация:** каталог `plgames-admin`, а «PLGames Admin» — отображаемое имя:
в заголовке README, в `Description=` юнитов, в приветствии бота. Ниже исходим
из этого; если нужен именно пробел — то же самое, но каждый путь придётся взять
в кавычки, включая cron.

## Порядок

Выполнять целиком, за один заход. Между шагами 2 и 6 бот и сторож не работают.

```bash
OLD=/home/plg/telegram-server-bot
NEW=/home/plg/plgames-admin
```

### 1. Остановить то, что держит каталог

```bash
sudo systemctl stop telegram-server-bot.service watchdog.timer watchdog.service
```

### 2. Убрать строку монитора из root-крона на время переезда

```bash
sudo crontab -l | grep -v 'telegram-server-bot/monitor.sh' | sudo crontab -
```

### 3. Переименовать

```bash
mv "$OLD" "$NEW"
```

### 4. Починить venv

Виртуальное окружение хранит абсолютный путь — после переезда оно нерабочее.
Пересоздать, а не править вручную:

```bash
cd "$NEW"
rm -rf venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-watchdog.txt
```

### 5. Обновить пути в репозитории

```bash
cd "$NEW"
grep -rl '/home/plg/telegram-server-bot' \
     --include='*.service' --include='*.timer' --include='*.md' --include='*.sh' . \
  | xargs sed -i 's|/home/plg/telegram-server-bot|/home/plg/plgames-admin|g'
```

Проверить, что не осталось: `grep -rn 'telegram-server-bot' . | grep -v venv/`
— останутся только имена самих юнитов (`telegram-server-bot.service`) и путь
env-файла `/etc/telegram-server-bot.env`. **Их менять не нужно** — см. ниже.

### 6. Переустановить юниты и вернуть крон

```bash
sudo "$NEW/watchdog/install.sh"
sudo crontab -l | { cat; echo "*/5 * * * * $NEW/monitor.sh 2>/dev/null"; } | sudo crontab -
```

### 7. Проверить

```bash
systemctl is-active telegram-server-bot.service   # active
systemctl list-timers watchdog.timer --no-pager
sudo -u tgbot "$NEW/venv/bin/python" -c 'import config; print(bool(config.OWNER_ID))'
"$NEW/monitor.sh" && echo "монитор отработал"
```

И нажать в боте «📊 Статус активного сервера» — это единственная проверка,
которая покрывает всю цепочку целиком.

## Что НЕ переименовывать

| Что | Почему |
|---|---|
| `/etc/telegram-server-bot.env` | Путь зашит в `config.py`, `monitor.sh`, обоих юнитах. Переименование даёт нулевую выгоду и три места для ошибки. Если очень хочется — менять вместе с `ENV_FILE` в `config.py:18`, `watchdog/install.sh:14` и списком в `monitor.sh`. |
| Имена юнитов `telegram-server-bot.service`, `watchdog.service` | Завязаны на `systemctl`, на строки в `watchdog/install.sh` и на привычку. Переименование — отдельная задача с `systemctl disable` старых. |
| Имена пользователей `tgbot`, `botagent` | Владеют файлами в `/var/lib/telegram-bot` и на наблюдаемых серверах. |
| GitHub-репозиторий `SYSadmins-BOT` | Зашит в `install.sh`, `install-monitor.sh` и в `post_init()` бота, откуда берётся ссылка в инструкции по установке агента. Переименование репозитория в GitHub оставляет редирект, но `raw.githubusercontent.com` по редиректу **не ходит** — инструкция установки сломается молча. Менять только вместе с этими тремя местами. |

## Отображаемое имя

После переезда «PLGames Admin» проставляется в:

* `README.md` — заголовок;
* `watchdog/systemd/telegram-server-bot.service` — `Description=PLGames Admin — Telegram-бот администрирования`;
* `watchdog/systemd/watchdog.service` — `Description=PLGames Admin · сторож сервера`;
* приветствие бота (`start_command`).
