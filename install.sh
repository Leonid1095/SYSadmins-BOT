#!/bin/bash

# Останавливаем выполнение скрипта при любой ошибке
set -e

# --- Переменные ---
# URL вашего agent.py на GitHub. Убедитесь, что он верный.
AGENT_RAW_URL="https://raw.githubusercontent.com/Leonid1095/telegram-server-bot/main/agent.py"

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

echo_info "Скачивание agent.py из репозитория..."
wget -q -O "$INSTALL_DIR/agent.py" "$AGENT_RAW_URL"
echo_success "Скрипт агента скачан."

# 4. Настройка виртуального окружения и установка библиотек
echo_info "Создание виртуального окружения..."
python3 -m venv venv
echo_info "Установка Flask, psutil и gunicorn..."
venv/bin/pip install --quiet --no-cache-dir Flask psutil gunicorn
echo_success "Виртуальное окружение настроено."

# 5. Создание файла сервиса systemd
echo_info "Создание сервиса systemd ($SERVICE_NAME)..."

# Используем cat с HEREDOC для создания файла. Это удобно и наглядно.
cat <<EOF > /etc/systemd/system/$SERVICE_NAME
[Unit]
Description=Telegram Bot Agent for Server Monitoring (Gunicorn)
After=network.target

[Service]
User=root
WorkingDirectory=$INSTALL_DIR
Environment="SECRET_KEY=$SECRET_KEY"
ExecStart=$INSTALL_DIR/venv/bin/gunicorn --workers 1 --bind 0.0.0.0:5000 agent:app
Restart=always
RestartSec=3

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
