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

# 1. Получение секретного ключа.
#
# Основной путь — переменная окружения SECRET_KEY. Ключ в аргументе (--key)
# попадает в /proc/<pid>/cmdline, а он читается ЛЮБЫМ пользователем машины: пока
# идёт установка, ключ агента видно в обычном `ps`. Окружение процесса лежит в
# /proc/<pid>/environ, доступном только владельцу и root. Тот же урок уже
# разобран в ops/README.md про токен в строке curl.
#
# --key оставлен ради старых инструкций, но предупреждает; --key-file читает
# ключ из файла и не светит его нигде.
SECRET_KEY="${SECRET_KEY:-}"
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --key)
            SECRET_KEY="$2"; shift
            echo "⚠️  ВНИМАНИЕ: ключ передан аргументом — он виден в 'ps' любому" >&2
            echo "    пользователю этой машины. Предпочтительно: SECRET_KEY=... bash install.sh" >&2
            ;;
        --key-file)
            [ -r "$2" ] || echo_error "Не читается файл ключа: $2"
            SECRET_KEY="$(tr -d '\r\n' < "$2")"; shift ;;
        --allow-from)
            # Можно указать несколько раз или через запятую: центральный сервер
            # ходит к агенту двумя маршрутами (напрямую и через мост), и наружу
            # они выходят с РАЗНЫХ адресов. Один адрес закрыл бы второй маршрут.
            ALLOW_FROM="${ALLOW_FROM:+$ALLOW_FROM,}$2"; shift ;;
        *) echo_error "Неизвестный параметр: $1" ;;
    esac
    shift
done

if [ -z "$SECRET_KEY" ]; then
    echo_error "Не задан секретный ключ. Пример: SECRET_KEY=ВАШ_КЛЮЧ bash install.sh"
fi

# Адрес центрального сервера, которому разрешён доступ к метрикам.
# Раньше скрипт лишь советовал закрыть порт последней строкой вывода — и совет,
# разумеется, не выполнялся: агент на Риге оказался за фаерволом, который не
# пускал вообще никого, и сервер полгода не наблюдался, хотя формально «агент
# установлен». Правило по умолчанию надёжнее совета.
ALLOW_FROM="${ALLOW_FROM:-}"

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
# gunicorn 23.0.0, а не 22: в версиях ниже CVE-2024-6827 (TE.CL request smuggling).
venv/bin/pip install --quiet --no-cache-dir "Flask==3.0.3" "psutil==6.0.0" "gunicorn==23.0.0"
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

# 7. Фаервол: пускаем к метрикам только центральный сервер.
if [ -n "$ALLOW_FROM" ]; then
    if command -v ufw >/dev/null 2>&1; then
        echo_info "Открываю порт 5000 только для $ALLOW_FROM..."
        IFS=',' read -ra ALLOW_LIST <<< "$ALLOW_FROM"
        for SRC in "${ALLOW_LIST[@]}"; do
            SRC="$(echo "$SRC" | tr -d '[:space:]')"
            [ -n "$SRC" ] || continue
            # Именно `insert 1`, а не `allow`: ufw проверяет правила по порядку и
            # добавляет новые в КОНЕЦ. Если на сервере уже стоит запрет на 5000 —
            # а на Риге он и стоял, — разрешение в конце списка не сработало бы
            # никогда, и «правка применена» означало бы ровно ничего.
            ufw --force delete allow from "$SRC" to any port 5000 proto tcp >/dev/null 2>&1 || true
            ufw insert 1 allow from "$SRC" to any port 5000 proto tcp >/dev/null
            echo_success "Разрешён доступ к порту 5000 с $SRC."
        done
        # Всем остальным — запрет. Без него разрешение выше ничего не сужает,
        # если политика по умолчанию разрешающая.
        ufw deny 5000 >/dev/null 2>&1 || true
    else
        echo_info "ufw не установлен — правило не добавлено. Закройте порт 5000 сами."
    fi
else
    echo_info "⚠️  Порт 5000 открыт всем. Ограничьте его: --allow-from <IP центрального сервера>"
fi

echo_info "🎉 Установка успешно завершена! Ваш сервер теперь под наблюдением."
echo_info "Агент работает под пользователем $AGENT_USER и проверяет подпись запросов."
echo_info "Метрики идут открытым текстом, поэтому порт 5000 стоит держать закрытым"
echo_info "для всех, кроме центрального сервера."
