#!/bin/bash
# ============================================================
# install-monitor.sh — ставит проактивный monitor.sh на сервер
# (Вариант A: каждый сервер мониторит себя и шлёт алерты).
#
# Пример (Telegram):
#   wget -qO- https://raw.githubusercontent.com/Leonid1095/SYSadmins-BOT/main/install-monitor.sh \
#     | sudo bash -s -- --token 123:ABC --owner 1148520376
#
# С резервным каналом ntfy:
#   ... --ntfy-url http://ntfy.example.com --ntfy-topic server-alerts
# ============================================================
set -e

REPO_RAW="https://raw.githubusercontent.com/Leonid1095/SYSadmins-BOT/main"
INSTALL_DIR="/opt/server-monitor"
TOKEN=""; OWNER=""; NTFY_URL=""; NTFY_TOPIC=""; NTFY_TOKEN=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --token)      TOKEN="$2"; shift ;;
        --owner)      OWNER="$2"; shift ;;
        --ntfy-url)   NTFY_URL="$2"; shift ;;
        --ntfy-topic) NTFY_TOPIC="$2"; shift ;;
        --ntfy-token) NTFY_TOKEN="$2"; shift ;;
        --dir)        INSTALL_DIR="$2"; shift ;;
        *) echo "Неизвестный параметр: $1" >&2; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "❌ Запустите под root (sudo)"; exit 1; }
if [ -z "$TOKEN" ] && [ -z "$NTFY_URL" ]; then
    echo "❌ Нужен хотя бы один канал: --token (Telegram) или --ntfy-url"; exit 1
fi
if [ -n "$TOKEN" ] && [ -z "$OWNER" ]; then
    echo "❌ С --token обязателен --owner (ваш Telegram id)"; exit 1
fi

# Зависимости
command -v curl    >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; }

echo "INFO: установка в $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

# monitor.sh из репозитория (с таймаутами, чтобы не зависнуть на сети/прокси)
if ! curl -fsSL --connect-timeout 15 --max-time 60 --retry 2 \
        "$REPO_RAW/monitor.sh" -o "$INSTALL_DIR/monitor.sh"; then
    echo "❌ Не удалось скачать monitor.sh (сеть/прокси). Повторите позже." >&2
    exit 1
fi
chmod 700 "$INSTALL_DIR/monitor.sh"

# config.py с секретами (0600)
umask 077
cat > "$INSTALL_DIR/config.py" <<EOF
import os
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "${TOKEN}")
OWNER_ID = os.getenv("OWNER_ID", "${OWNER}")
EOF
chmod 600 "$INSTALL_DIR/config.py"

# monitor.local.conf (создаём один раз, не затираем существующий)
if [ ! -f "$INSTALL_DIR/monitor.local.conf" ]; then
    {
        [ -n "$NTFY_URL" ]   && echo "NTFY_URL=\"$NTFY_URL\""
        [ -n "$NTFY_TOPIC" ] && echo "NTFY_TOPIC=\"$NTFY_TOPIC\""
        [ -n "$NTFY_TOKEN" ] && echo "NTFY_TOKEN=\"$NTFY_TOKEN\""
        echo '# ENDPOINTS=("https://ваш-домен")'
        echo '# CRITICAL_SERVICES="nginx postgresql docker fail2ban ssh"'
    } > "$INSTALL_DIR/monitor.local.conf"
    chmod 600 "$INSTALL_DIR/monitor.local.conf"
fi

# cron (root, каждые 5 минут), идемпотентно
CRON_LINE="*/5 * * * * $INSTALL_DIR/monitor.sh >> /var/log/server-monitor.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "$INSTALL_DIR/monitor.sh"; echo "$CRON_LINE" ) | crontab -

echo "✅ Установлено. Тестовый прогон (если найдёт проблему — придёт алерт):"
"$INSTALL_DIR/monitor.sh" || true
echo "✅ Готово. Cron: $CRON_LINE"
echo "   Правьте пороги/эндпоинты в $INSTALL_DIR/monitor.local.conf"
