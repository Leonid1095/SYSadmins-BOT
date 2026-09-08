#!/bin/bash

# Останавливаем выполнение скрипта при любой ошибке
set -e

# --- Переменные ---
# URL вашего agent.py на GitHub. Убедитесь, что он верный.
REPO_RAW="https://raw.githubusercontent.com/Leonid1095/SYSadmins-BOT/main"

# Директория для установки
INSTALL_DIR="/root/telegram-server-bot"

# Имя сервиса systemd
SERVICE_NAME="bot-agent.service"

# --- Функции ---
echo_info() {
    echo "INFO: $1"
}

echo_success() {
    echo "✅ SUCCESS: $1"
}

echo_error() {
    echo "❌ ERROR: $1" >&2
    exit 1
}

# --- Логика скрипта ---

# 1. Парсинг аргументов командной строки для получения ключа
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --key) SECRET_KEY="$2"; shift ;;
        *) echo_error "Неизвестный параметр: $1" ;;
    esac
    shift
done

if [ -z "$SECRET_KEY" ]; then
    echo_error "Необходимо указать секретный ключ. Пример: --key ВАШ_КЛЮЧ"
fi

echo_info "Начало установки агента мониторинга..."

# 2. Установка зависимостей
echo_info "Обновление пакетов и установка зависимостей (python3-venv, wget)..."
apt-get update > /dev/null
apt-get install -y python3-venv wget > /dev/null
echo_success "Зависимости установлены."

# 3. Создание директории и скачивание агента
echo_info "Создание директории $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR" || exit

echo_info "Скачивание агента из репозитория..."
# agent_auth.py обязателен: агент проверяет подпись запроса, а не принимает
# секрет заголовком — иначе ключ уходил бы по HTTP открытым текстом.
for f in agent.py agent_auth.py; do
    wget -q --timeout=30 --tries=3 -O "$INSTALL_DIR/$f" "$REPO_RAW/$f" \
        || echo_error "Не удалось скачать $f"
done
echo_success "Скрипт агента скачан."

# 4. Настройка виртуального окружения и установка библиотек
echo_info "Создание виртуального окружения..."
python3 -m venv venv
echo_info "Установка Flask, psutil и gunicorn (фиксированные версии)..."
venv/bin/pip install --quiet --no-cache-dir "Flask==3.0.3" "psutil==6.0.0" "gunicorn==22.0.0"
echo_success "Виртуальное окружение настроено."

# 5. Создание файла сервиса systemd
echo_info "Создание сервиса systemd ($SERVICE_NAME)..."

# Секретный ключ — в отдельном файле с правами 0600 (не виден в самом юните)
ENV_FILE="/etc/bot-agent.env"
umask 077
echo "SECRET_KEY=$SECRET_KEY" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Используем cat с HEREDOC для создания файла. Это удобно и наглядно.
# Отдельный пользователь: агент только читает телеметрию, root ему не нужен,
# а порт 5000 смотрит наружу — это первое, что попробуют сломать.
AGENT_USER="botagent"
id "$AGENT_USER" >/dev/null 2>&1 || useradd --system --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "$AGENT_USER"
chown -R "$AGENT_USER":"$AGENT_USER" "$INSTALL_DIR"
chown root:"$AGENT_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

cat <<EOF > /etc/systemd/system/$SERVICE_NAME
[Unit]
Description=Telegram Bot Agent for Server Monitoring (Gunicorn)
After=network.target

[Service]
User=$AGENT_USER
Group=$AGENT_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/gunicorn --workers 1 --bind 0.0.0.0:5000 agent:app
Restart=always
RestartSec=3
# Агент только читает телеметрию — привилегии ему не нужны ни в каком виде.
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF

echo_success "Файл сервиса создан."

# 6. Запуск сервиса
echo_info "Перезагрузка systemd и запуск сервиса..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" > /dev/null
systemctl restart "$SERVICE_NAME"
echo_success "Сервис агента запущен и добавлен в автозагрузку."

echo_info "🎉 Установка успешно завершена! Ваш сервер теперь под наблюдением."
echo_info "Агент работает под пользователем $AGENT_USER и проверяет подпись запросов."
echo_info "Ограничьте порт 5000 фаерволом на IP центрального сервера — метрики"
echo_info "по-прежнему идут открытым текстом, хотя ключ больше не передаётся."
