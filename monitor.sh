#!/bin/bash
# ============================================================
# Proactive Server Monitor — Telegram Alerts (владельцу)
# Следит за железом И за всеми проектами: Docker-контейнеры,
# systemd-сервисы, HTTP-эндпоинты, диск/RAM/CPU/GPU/SMART/SSL.
# Cron: */5 * * * * /home/plg/telegram-server-bot/monitor.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Секреты: env-файл вне репозитория (0600), первый читаемый из списка. ---
# Токен НЕ хранится ни в репозитории, ни в config.py: только в env-файле.
for _envf in "${BOT_ENV_FILE:-}" /etc/telegram-server-bot.env /etc/server-monitor.env; do
    [ -n "$_envf" ] && [ -r "$_envf" ] && . "$_envf" && break
done
BOT_TOKEN="${TELEGRAM_TOKEN:-}"
OWNER_ID="${OWNER_ID:-}"
# Совместимость со старыми установками, где секреты лежали в config.py.
if [ -z "$BOT_TOKEN" ] && [ -f "$SCRIPT_DIR/config.py" ]; then
    BOT_TOKEN=$(cd "$SCRIPT_DIR" && python3 -c "import config; print(config.TELEGRAM_TOKEN)" 2>/dev/null)
    OWNER_ID=$(cd "$SCRIPT_DIR" && python3 -c "import config; print(config.OWNER_ID)" 2>/dev/null)
fi
# Проверка «настроен хотя бы один канал» — ниже, после загрузки monitor.local.conf

# Состояние антиспама. Раньше лежало в /tmp по предсказуемому имени: каталог
# общий и доступен на запись всем, а скрипт крутится в root-кроне и делает по
# этому пути append, mv и обнуление (> "$STATE_FILE" в полночь). Ядро Ubuntu
# такой подлог сейчас гасит (fs.protected_symlinks/protected_regular), но
# опираться на чужой sysctl в root-скрипте незачем, и /tmp вдобавок чистится при
# загрузке. Держим состояние в приватном каталоге; /tmp остаётся запасным путём
# для запуска без прав на /var/lib.
STATE_DIR="${MONITOR_STATE_DIR:-/var/lib/server-monitor}"
if ! mkdir -p "$STATE_DIR" 2>/dev/null || [ ! -w "$STATE_DIR" ]; then
    STATE_DIR="${TMPDIR:-/tmp}/server-monitor-$(id -u)"
    mkdir -p "$STATE_DIR" 2>/dev/null
fi
chmod 700 "$STATE_DIR" 2>/dev/null
STATE_FILE="$STATE_DIR/state-$(id -u)"
HOSTNAME=$(hostname)

# --- Пороги и цели по умолчанию (можно переопределить в monitor.local.conf) ---
DISK_WARN=80          # % диска / — предупреждение
DISK_CRIT=90          # % диска / — критично
HDD_MOUNT="/mnt/hdd"  # точка монтирования HDD бэкапов ("" чтобы отключить)
HDD_WARN=85
RAM_WARN=90           # % RAM
SWAP_WARN_KB=1048576  # КБ swap (>1 ГБ)
CPU_WARN=85           # % утилизации CPU — предупреждение
CPU_CRIT=95           # % утилизации CPU — критично
LOAD_WARN=150         # load average в % на ядро (150 = 1.5×ядер)
LOAD_CRIT=300         # load average в % на ядро — критично
GPU_TEMP_WARN=75
GPU_TEMP_CRIT=85
GPU_LOAD_WARN=95
SMART_DISK="/dev/sda"
F2B_JAIL="sshd"
F2B_BAN_WARN=20

# Критичные сервисы (должны быть active). Остальные упавшие ловятся авто по state=failed.
CRITICAL_SERVICES="nginx postgresql docker fail2ban ssh"
# systemd-юниты, которые НЕ алертить как failed (частый безобидный шум), через пробел
SERVICES_IGNORE="fwupd.service fwupd-refresh.service"
# Контейнеры-исключения (одноразовые/намеренно остановленные), через пробел
# Комнаты Civ4Col поднимаются по заказу и гасятся сами через 30 мин без игроков —
# остановленная комната это НОРМА, а не авария. Без исключения каждое автогашение давало
# три алерта «Контейнер не запущен», и на их фоне терялись настоящие поломки.
DOCKER_IGNORE="civ4col-pitboss*"
# HTTP-эндпоинты для проверки доступности проектов, напр. ENDPOINTS=("https://site.ru" ...)
ENDPOINTS=()

# --- Каналы уведомлений (кроме Telegram). Пусто = выключено. ---
NTFY_URL=""        # self-hosted ntfy, напр. http://127.0.0.1:2586
NTFY_TOPIC=""      # тема, напр. server-alerts
NTFY_TOKEN=""      # опц., если ntfy требует авторизацию
WEBHOOK_URL=""     # generic POST plain-text (на будущее: интеграции/SaaS)
ALERT_EMAIL=""     # адрес получателя email-алертов
# SMTP-релей (если прямой :25 заблокирован провайдером). Пусто = системный mail.
SMTP_HOST=""       # напр. smtp.yandex.ru
SMTP_PORT="465"    # 465 (SSL) или 587
SMTP_USER=""       # логин = полный email
SMTP_PASS=""       # ПАРОЛЬ ПРИЛОЖЕНИЯ (не основной пароль аккаунта)
SMTP_FROM=""       # от кого (по умолчанию = SMTP_USER)

# Переопределения без правки скрипта (файл в .gitignore)
[ -f "$SCRIPT_DIR/monitor.local.conf" ] && source "$SCRIPT_DIR/monitor.local.conf"

# Должен быть настроен хотя бы один канал уведомлений
if [ -z "$BOT_TOKEN" ] && [ -z "$NTFY_URL" ] && [ -z "$WEBHOOK_URL" ] && [ -z "$ALERT_EMAIL" ]; then
    echo "monitor.sh: не настроен ни один канал уведомлений (Telegram/ntfy/webhook/email)" >&2
    exit 1
fi

# --- Вспомогательные функции ---
html_escape() {
    local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"
}

# Отправка во ВСЕ настроенные каналы (гибко, не завязано на один Telegram)
notify() {
    local msg="$1"

    # 1) Telegram
    if [ -n "$BOT_TOKEN" ] && [ -n "$OWNER_ID" ]; then
        curl -s --max-time 15 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${OWNER_ID}" \
            --data-urlencode "text=${msg}" \
            --data-urlencode "parse_mode=HTML" >/dev/null 2>&1
    fi

    # 2) ntfy (self-hosted; работает, даже когда Telegram недоступен)
    if [ -n "$NTFY_URL" ] && [ -n "$NTFY_TOPIC" ]; then
        local plain args
        plain=$(printf '%s' "$msg" | sed -E 's/<[^>]+>//g; s/&lt;/</g; s/&gt;/>/g; s/&amp;/\&/g')
        args=(-s --max-time 15 -H "Title: Server Monitor" -H "Priority: high" -H "Tags: warning")
        [ -n "$NTFY_TOKEN" ] && args+=(-H "Authorization: Bearer ${NTFY_TOKEN}")
        curl "${args[@]}" -d "${plain}" "${NTFY_URL%/}/${NTFY_TOPIC}" >/dev/null 2>&1
    fi

    # 3) Generic webhook (на будущее: интеграции/SaaS)
    if [ -n "$WEBHOOK_URL" ]; then
        curl -s --max-time 15 -H "Content-Type: text/plain; charset=utf-8" \
            --data-binary "${msg}" "$WEBHOOK_URL" >/dev/null 2>&1
    fi

    # 4) Email (работает, когда Telegram недоступен)
    if [ -n "$ALERT_EMAIL" ]; then
        local plain subject from
        plain=$(printf '%s' "$msg" | sed -E 's/<[^>]+>//g; s/&lt;/</g; s/&gt;/>/g; s/&amp;/\&/g')
        subject="Server Monitor: ${HOSTNAME}"
        if [ -n "$SMTP_HOST" ] && [ -n "$SMTP_USER" ] && [ -n "$SMTP_PASS" ]; then
            # Аутентифицированный релей через curl SMTP — не трогаем системный MTA,
            # обходит блокировку исходящего :25 и работает на любом сервере
            from="${SMTP_FROM:-$SMTP_USER}"
            printf 'From: %s\r\nTo: %s\r\nSubject: %s\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n%s\r\n' \
                "$from" "$ALERT_EMAIL" "$subject" "$plain" | \
            curl -s --max-time 25 --ssl-reqd "smtps://${SMTP_HOST}:${SMTP_PORT:-465}" \
                --mail-from "$from" --mail-rcpt "$ALERT_EMAIL" \
                --user "${SMTP_USER}:${SMTP_PASS}" -T - >/dev/null 2>&1
        elif command -v mail >/dev/null 2>&1; then
            printf '%s\n' "$plain" | mail -s "$subject" "$ALERT_EMAIL"
        fi
    fi
}

# Антиспам: состояние по точному совпадению строки (fixed-string, whole-line)
was_sent()  { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
mark_sent() { was_sent "$1" || echo "$1" >> "$STATE_FILE"; }
clear_mark() {
    [ -f "$STATE_FILE" ] || return 0
    was_sent "$1" || return 0                       # нечего убирать — выходим
    local tmp
    tmp=$(mktemp "${STATE_FILE}.XXXXXX" 2>/dev/null) || return 0
    grep -vxF "$1" "$STATE_FILE" > "$tmp" 2>/dev/null   # результат может стать пустым — это ок
    mv "$tmp" "$STATE_FILE"
}

# Две очереди. ALERTS — то, о чём сторож докладывает лучше нас (с разбором и
# кнопками): при живом стороже молчим. OWN_ALERTS — то, чего сторож не видит
# вовсе: видеокарта, массовые баны, служба в inactive (а не failed) и
# сертификаты доменов вне списка эндпоинтов. Эти шлём всегда.
ALERTS=()
OWN_ALERTS=()
add_alert()     { ALERTS+=("$1"); }
add_alert_own() { OWN_ALERTS+=("$1"); }

# Совпадение имени с одним из шаблонов в списке исключений.
# Раньше сравнение шло подстрокой по всему списку, поэтому шаблоны не работали:
# civ4col-pitboss4/5/6 завелись позже, в списке остались только 1-3, и каждое
# самогашение комнаты давало тревогу. Теперь список — это шаблоны через пробел,
# и "civ4col-pitboss*" закрывает их все, включая будущие.
matches_ignore() {
    local name="$1" pattern
    for pattern in $2; do
        # shellcheck disable=SC2254 — шаблон намеренно раскрывается как glob
        case "$name" in $pattern) return 0 ;; esac
    done
    return 1
}

# --- Разделение ролей со сторожем -------------------------------------------
#
# Сторож (watchdog/) следит за тем же самым, но умеет объяснить и дать кнопки.
# Пока он жив, дублировать его текстовыми алертами незачем: владелец получал по
# два сообщения о каждой поломке, в двух разных форматах, и переставал читать оба.
#
# Поэтому monitor.sh становится страховкой, а не вторым докладчиком. Он молчит о
# том, что покрывает сторож, ровно пока сторож свеж. Как только снимок протух
# (сторож не отработал, упал, машине плохо) — monitor.sh снова докладывает обо
# всём. Это его сильная сторона: он простой, крутится в root-кроне и работает
# тогда, когда сложный сторож уже не может.
WATCHDOG_STATE="${WATCHDOG_STATE_FILE:-/var/lib/watchdog/state.json}"
WATCHDOG_MAX_AGE="${WATCHDOG_MAX_AGE:-900}"   # 15 мин = три пропущенных цикла

watchdog_is_alive() {
    [ "${DEFER_TO_WATCHDOG:-auto}" != "no" ] || return 1
    [ -r "$WATCHDOG_STATE" ] || return 1
    local age now mtime
    now=$(date +%s)
    mtime=$(stat -c %Y "$WATCHDOG_STATE" 2>/dev/null) || return 1
    age=$(( now - mtime ))
    [ "$age" -le "$WATCHDOG_MAX_AGE" ]
}

if watchdog_is_alive; then
    WATCHDOG_COVERS=1
    clear_mark "watchdog_stale"
else
    WATCHDOG_COVERS=0
    if [ -e "$WATCHDOG_STATE" ] && [ "${DEFER_TO_WATCHDOG:-auto}" != "no" ]; then
        # Сторож молчит — об этом надо сказать: он и есть основной канал.
        if ! was_sent "watchdog_stale"; then
            add_alert "🔴 <b>Сторож не отвечает.</b> Он не обновлял состояние больше $((WATCHDOG_MAX_AGE / 60)) минут, поэтому разборы и кнопки починки сейчас не приходят. Проверьте: <code>systemctl status watchdog.timer</code>"
            mark_sent "watchdog_stale"
        fi
    fi
fi

# Сторож уже докладывает об этой категории — молчим, пока он жив.
covered_by_watchdog() { [ "$WATCHDOG_COVERS" = "1" ]; }

# Удалённые серверы владельца теперь опрашивает сторож (watchdog/collect.py):
# он делает это подписанными запросами, сравнивает с прошлым снимком и присылает
# разбор с кнопками. Прежний monitor_remote.py удалён — он по устройству
# пропускал владельца, то есть как раз те серверы и не проверял.

# --- 1. ДИСК (/) ---
DISK_PERCENT=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
if [ -n "$DISK_PERCENT" ] && [ "$DISK_PERCENT" -ge "$DISK_CRIT" ]; then
    if ! was_sent "disk_critical"; then
        add_alert "🔴 <b>Диск почти полон:</b> занято ${DISK_PERCENT}% корневого раздела. Когда места не останется, начнут падать базы и логи. Смотрите, чем занято: <code>docker system df</code>, <code>journalctl --disk-usage</code>"
        mark_sent "disk_critical"
    fi
elif [ -n "$DISK_PERCENT" ] && [ "$DISK_PERCENT" -ge "$DISK_WARN" ]; then
    if ! was_sent "disk_warning"; then
        add_alert "🟡 <b>Диск заполняется:</b> занято ${DISK_PERCENT}%. Пока не срочно, но стоит посмотреть, что растёт."
        mark_sent "disk_warning"
    fi
else
    clear_mark "disk_warning"; clear_mark "disk_critical"
fi

# --- 2. HDD бэкапов ---
if [ -n "$HDD_MOUNT" ]; then
    HDD_PERCENT=$(df "$HDD_MOUNT" 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')
    if [ -n "$HDD_PERCENT" ] && [ "$HDD_PERCENT" -ge "$HDD_WARN" ]; then
        if ! was_sent "hdd_warning"; then
            add_alert "🟡 <b>Диск с бэкапами заполняется:</b> занято ${HDD_PERCENT}%. Когда он кончится, новые бэкапы перестанут сохраняться — молча."
            mark_sent "hdd_warning"
        fi
    else
        clear_mark "hdd_warning"
    fi
fi

# --- 3. RAM ---
RAM_PERCENT=$(free | awk '/Mem:/ {printf "%.0f", $3/$2 * 100}')
if [ -n "$RAM_PERCENT" ] && [ "$RAM_PERCENT" -ge "$RAM_WARN" ]; then
    if ! was_sent "ram_critical"; then
        RAM_USED=$(free -h | awk '/Mem:/ {print $3}')
        RAM_TOTAL=$(free -h | awk '/Mem:/ {print $2}')
        add_alert "🔴 <b>Память на исходе:</b> занято ${RAM_PERCENT}% (${RAM_USED} из ${RAM_TOTAL}). При нехватке ядро начнёт убивать процессы — обычно самый крупный, то есть чей-то рабочий сервис."
        mark_sent "ram_critical"
    fi
elif [ -n "$RAM_PERCENT" ] && [ "$RAM_PERCENT" -lt $((RAM_WARN - 10)) ]; then
    clear_mark "ram_critical"
fi

# --- 4. SWAP ---
SWAP_USED=$(free | awk '/Swap:/ {print $3}')
if [ -n "$SWAP_USED" ] && [ "$SWAP_USED" -gt "$SWAP_WARN_KB" ]; then
    if ! was_sent "swap_warning"; then
        SWAP_H=$(free -h | awk '/Swap:/ {print $3}')
        add_alert "🟡 <b>Система ушла в подкачку:</b> занято ${SWAP_H}. Всё, что попало в подкачку, работает с диска и потому заметно медленнее."
        mark_sent "swap_warning"
    fi
else
    clear_mark "swap_warning"
fi

# --- 5. CPU: реальная утилизация + load average (без bc) ---
cpu_snapshot() { awk '/^cpu /{idle=$5+$6; tot=$2+$3+$4+$5+$6+$7+$8+$9; print idle" "tot; exit}' /proc/stat; }
read -r I1 T1 <<< "$(cpu_snapshot)"
sleep 1
read -r I2 T2 <<< "$(cpu_snapshot)"
CPU_PCT=$(awk -v di=$((I2 - I1)) -v dt=$((T2 - T1)) 'BEGIN{ if (dt <= 0) print 0; else printf "%d", (1 - di/dt) * 100 }')
LOAD=$(awk '{print $1}' /proc/loadavg)
CORES=$(nproc)
LOAD_PCT=$(awk -v l="$LOAD" -v c="$CORES" 'BEGIN{ if (c <= 0) c = 1; printf "%d", (l / c) * 100 }')

if { [ -n "$CPU_PCT" ] && [ "$CPU_PCT" -ge "$CPU_CRIT" ]; } || { [ -n "$LOAD_PCT" ] && [ "$LOAD_PCT" -ge "$LOAD_CRIT" ]; }; then
    if ! was_sent "cpu_critical"; then
        add_alert "🔴 <b>Процессор не справляется:</b> загружен на ${CPU_PCT}%, очередь ${LOAD} на ${CORES} ядер. Задачи стоят и ждут."
        mark_sent "cpu_critical"
    fi
    clear_mark "cpu_warning"
elif { [ -n "$CPU_PCT" ] && [ "$CPU_PCT" -ge "$CPU_WARN" ]; } || { [ -n "$LOAD_PCT" ] && [ "$LOAD_PCT" -ge "$LOAD_WARN" ]; }; then
    if ! was_sent "cpu_warning"; then
        add_alert "🟡 <b>Процессор нагружен:</b> ${CPU_PCT}%, очередь ${LOAD} на ${CORES} ядер. Если это надолго — стоит посмотреть, кто грузит."
        mark_sent "cpu_warning"
    fi
else
    clear_mark "cpu_warning"; clear_mark "cpu_critical"
fi

# --- 5.1 GPU ---
if command -v nvidia-smi &> /dev/null; then
    GPU_LOAD=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    GPU_TEMP=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    GPU_NAME=$(html_escape "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)")

    if [ -n "$GPU_TEMP" ] && [ "$GPU_TEMP" -ge "$GPU_TEMP_CRIT" ]; then
        if ! was_sent "gpu_temp_critical"; then
            add_alert_own "🔴 <b>Видеокарта перегревается:</b> ${GPU_NAME} — ${GPU_TEMP}°C, нагрузка ${GPU_LOAD}%. Выше 85°C она сбрасывает частоты, чтобы не сгореть."
            mark_sent "gpu_temp_critical"
        fi
    elif [ -n "$GPU_TEMP" ] && [ "$GPU_TEMP" -ge "$GPU_TEMP_WARN" ]; then
        if ! was_sent "gpu_temp_warning"; then
            add_alert_own "🟡 <b>Видеокарта греется:</b> ${GPU_NAME} — ${GPU_TEMP}°C, нагрузка ${GPU_LOAD}%."
            mark_sent "gpu_temp_warning"
        fi
    else
        clear_mark "gpu_temp_warning"; clear_mark "gpu_temp_critical"
    fi

    if [ -n "$GPU_LOAD" ] && [ "$GPU_LOAD" -ge "$GPU_LOAD_WARN" ]; then
        if ! was_sent "gpu_load_high"; then
            add_alert_own "🟡 <b>Видеокарта под полной нагрузкой:</b> ${GPU_NAME} — ${GPU_LOAD}%, ${GPU_TEMP}°C."
            mark_sent "gpu_load_high"
        fi
    elif [ -n "$GPU_LOAD" ] && [ "$GPU_LOAD" -lt 80 ]; then
        clear_mark "gpu_load_high"
    fi
fi

# --- 6. HDD SMART (нужен sudo -n) ---
if command -v smartctl &> /dev/null; then
    SMART_STATUS=$(sudo -n smartctl -n standby -H "$SMART_DISK" 2>/dev/null | grep -i "result" | awk '{print $NF}')
    if [ -n "$SMART_STATUS" ] && [ "$SMART_STATUS" != "PASSED" ]; then
        if ! was_sent "smart_fail"; then
            add_alert "🔴 <b>Диск ${SMART_DISK} умирает.</b> Он не прошёл собственную самопроверку — это предвестник отказа, а не сбой измерения. Планируйте замену и проверьте, что бэкапы уезжают с этого диска."
            mark_sent "smart_fail"
        fi
    fi
    REALLOC=$(sudo -n smartctl -n standby -A "$SMART_DISK" 2>/dev/null | grep "Reallocated_Sector" | awk '{print $NF}')
    if [ -n "$REALLOC" ] && [ "$REALLOC" -gt 0 ]; then
        if ! was_sent "smart_realloc"; then
            add_alert "🟡 <b>Диск начал сыпаться:</b> ${REALLOC} секторов переназначено. Пока данные целы, но число обычно только растёт — следите за ним."
            mark_sent "smart_realloc"
        fi
    fi
    PENDING=$(sudo -n smartctl -n standby -A "$SMART_DISK" 2>/dev/null | grep "Current_Pending" | awk '{print $NF}')
    if [ -n "$PENDING" ] && [ "$PENDING" -gt 0 ]; then
        if ! was_sent "smart_pending"; then
            add_alert "🟡 <b>На диске подозрительные секторы:</b> ${PENDING} ждут переназначения. Часто это первый признак скорого отказа."
            mark_sent "smart_pending"
        fi
    fi
fi

# --- 7. КРИТИЧНЫЕ СЕРВИСЫ (должны быть active) ---
for svc in $CRITICAL_SERVICES; do
    if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
        [ "$svc" = "ssh" ] && systemctl is-active --quiet sshd 2>/dev/null && { clear_mark "svc_down_ssh"; continue; }
        if ! was_sent "svc_down_${svc}"; then
            add_alert_own "🔴 <b>Служба остановлена:</b> $(html_escape "$svc"). Она в списке критичных и должна работать всегда. Сторож такое не ловит: он видит только упавшие (failed), а эта остановлена штатно."
            mark_sent "svc_down_${svc}"
        fi
    else
        clear_mark "svc_down_${svc}"
    fi
done

# --- 7.1 ЛЮБЫЕ упавшие systemd-юниты (авто-покрытие всех проектов) ---
while read -r unit; do
    [ -z "$unit" ] && continue
    matches_ignore "$unit" "$SERVICES_IGNORE" && continue
    if ! was_sent "failed_${unit}"; then
        add_alert "🔴 <b>Служба упала:</b> $(html_escape "$unit"). systemd пытался поднять её и сдался. Причина в журнале: <code>journalctl -u $(html_escape "$unit") -n 50</code>"
        mark_sent "failed_${unit}"
    fi
done < <(systemctl list-units --type=service --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}')

# --- 8. DOCKER: все контейнеры (авто-покрытие всех проектов) ---
if command -v docker &> /dev/null; then
    while IFS=$'\t' read -r cname cstate cstatus; do
        [ -z "$cname" ] && continue
        matches_ignore "$cname" "$DOCKER_IGNORE" && continue
        # Exited (0) — контейнер отработал и завершился сам, успешно. Так живут
        # разовые задачи (миграции, джобы): для них остановка — это финиш, а не
        # авария. Раньше svod-migrate-1 попадал в тревоги каждые сутки именно так.
        case "$cstatus" in
            "Exited (0)"*) clear_mark "docker_down_${cname}"; continue ;;
        esac
        if [ "$cstate" != "running" ]; then
            if ! was_sent "docker_down_${cname}"; then
                add_alert "🔴 <b>Контейнер не работает:</b> $(html_escape "$cname") — ${cstate}. Проверьте: <code>docker logs --tail 50 $(html_escape "$cname")</code>"
                mark_sent "docker_down_${cname}"
            fi
        else
            clear_mark "docker_down_${cname}"
            if printf '%s' "$cstatus" | grep -q "(unhealthy)"; then
                if ! was_sent "docker_unhealthy_${cname}"; then
                    add_alert "🟡 <b>Контейнер нездоров:</b> $(html_escape "$cname") работает, но его проверка здоровья не проходит — снаружи сервис уже может не отвечать."
                    mark_sent "docker_unhealthy_${cname}"
                fi
            else
                clear_mark "docker_unhealthy_${cname}"
            fi
        fi
    done < <(docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null)
fi

# --- 9. fail2ban: массовая атака ---
if command -v fail2ban-client &> /dev/null; then
    BANNED=$(sudo -n fail2ban-client status "$F2B_JAIL" 2>/dev/null | grep "Currently banned" | awk '{print $NF}')
    if [ -n "$BANNED" ] && [ "$BANNED" -ge "$F2B_BAN_WARN" ]; then
        if ! was_sent "f2b_mass_ban"; then
            add_alert_own "⚠️ <b>Сервер щупают активнее обычного:</b> fail2ban заблокировал ${BANNED} адресов в правиле ${F2B_JAIL}. Само по себе это не поломка — защита работает, — но всплеск стоит заметить."
            mark_sent "f2b_mass_ban"
        fi
    elif [ -n "$BANNED" ] && [ "$BANNED" -lt $((F2B_BAN_WARN / 2)) ]; then
        clear_mark "f2b_mass_ban"
    fi
fi

# --- 10. HTTP-эндпоинты проектов ---
for url in "${ENDPOINTS[@]}"; do
    [ -z "$url" ] && continue
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$url" 2>/dev/null)
    key="http_$(printf '%s' "$url" | tr -c 'a-zA-Z0-9' '_')"
    if [ "$code" = "000" ] || [ "${code:0:1}" = "5" ]; then
        if ! was_sent "$key"; then
            add_alert "🔴 <b>Сайт не отвечает:</b> $(html_escape "$url") — HTTP ${code}. Проверка идёт с самого сервера, так что посетители видят то же самое."
            mark_sent "$key"
        fi
    else
        clear_mark "$key"
    fi
done

# --- 11. SSL сертификаты ---
for cert in /etc/letsencrypt/live/*/fullchain.pem; do
    [ -f "$cert" ] || continue
    DOMAIN=$(basename "$(dirname "$cert")")
    EXPIRY=$(sudo -n openssl x509 -enddate -noout -in "$cert" 2>/dev/null | cut -d= -f2)
    [ -z "$EXPIRY" ] && EXPIRY=$(openssl x509 -enddate -noout -in "$cert" 2>/dev/null | cut -d= -f2)
    [ -z "$EXPIRY" ] && continue
    EXPIRY_EPOCH=$(date -d "$EXPIRY" +%s 2>/dev/null)
    NOW_EPOCH=$(date +%s)
    [ -z "$EXPIRY_EPOCH" ] && continue
    DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))
    dkey=$(printf '%s' "$DOMAIN" | tr -c 'a-zA-Z0-9' '_')
    if [ "$DAYS_LEFT" -le 0 ]; then
        if ! was_sent "ssl_expired_${dkey}"; then
            add_alert_own "🔴 <b>Сертификат истёк:</b> $(html_escape "$DOMAIN"). Сайт открывается с предупреждением о небезопасности."
            mark_sent "ssl_expired_${dkey}"
        fi
    elif [ "$DAYS_LEFT" -le 14 ]; then
        if ! was_sent "ssl_${dkey}"; then
            add_alert_own "🟡 <b>Сертификат скоро истечёт:</b> $(html_escape "$DOMAIN"), осталось ${DAYS_LEFT} дн. Обычно Let's Encrypt продлевает сам за 30 дней — если счётчик не растёт, автопродление сломалось."
            mark_sent "ssl_${dkey}"
        fi
    else
        clear_mark "ssl_${dkey}"; clear_mark "ssl_expired_${dkey}"
    fi
done

# --- ОТПРАВКА (только владельцу, реальные переносы строк) ---
SEND=("${OWN_ALERTS[@]}")
if ! covered_by_watchdog; then
    # Сторож молчит — докладываем обо всём сами, как раньше.
    SEND+=("${ALERTS[@]}")
fi

if [ ${#SEND[@]} -gt 0 ]; then
    MSG="🖥 <b>Сервер $(html_escape "$HOSTNAME")</b>"$'\n\n'
    MSG+=$(printf '%s\n\n' "${SEND[@]}")
    if covered_by_watchdog; then
        MSG+=$'\n'"<i>Это то, чего сторож не видит. Об остальном он доложит сам, с разбором и кнопками.</i>"
    fi
    MSG+=$'\n'"⏰ $(date '+%d.%m.%Y %H:%M')"
    notify "$MSG"
fi

# Суточного обнуления состояния здесь больше нет. Оно задумывалось как уборка
# устаревших меток, но на деле означало, что каждую ночь заново приходят ВСЕ
# активные тревоги — те же самые, что вчера. Именно так «мониторинг» и
# превращается в спам, который перестают читать.
#
# Метка снимается тогда, когда пропадает её причина: за это отвечает clear_mark
# в каждой проверке. Осиротевшие метки (например, от удалённого контейнера)
# безвредны: они лишь означают, что о несуществующем объекте не напомнят.
