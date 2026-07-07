#!/bin/bash
# ============================================================
# Proactive Server Monitor — Telegram Alerts (владельцу)
# Следит за железом И за всеми проектами: Docker-контейнеры,
# systemd-сервисы, HTTP-эндпоинты, диск/RAM/CPU/GPU/SMART/SSL.
# Cron: */5 * * * * /home/plg/telegram-server-bot/monitor.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Секреты: единый источник — config.py (в .gitignore) ---
BOT_TOKEN=$(cd "$SCRIPT_DIR" && python3 -c "import config; print(config.TELEGRAM_TOKEN)" 2>/dev/null)
OWNER_ID=$(cd "$SCRIPT_DIR" && python3 -c "import config; print(config.OWNER_ID)" 2>/dev/null)
# Проверка «настроен хотя бы один канал» — ниже, после загрузки monitor.local.conf

# Per-user, чтобы не конфликтовать по владельцу в sticky /tmp (root/plg запуски)
STATE_FILE="/tmp/server-monitor-state-$(id -u)"
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
DOCKER_IGNORE=""
# HTTP-эндпоинты для проверки доступности проектов, напр. ENDPOINTS=("https://site.ru" ...)
ENDPOINTS=()

# --- Каналы уведомлений (кроме Telegram). Пусто = выключено. ---
NTFY_URL=""        # self-hosted ntfy, напр. http://127.0.0.1:2586
NTFY_TOPIC=""      # тема, напр. server-alerts
NTFY_TOKEN=""      # опц., если ntfy требует авторизацию
WEBHOOK_URL=""     # generic POST plain-text (на будущее: интеграции/SaaS)

# Переопределения без правки скрипта (файл в .gitignore)
[ -f "$SCRIPT_DIR/monitor.local.conf" ] && source "$SCRIPT_DIR/monitor.local.conf"

# Должен быть настроен хотя бы один канал уведомлений
if [ -z "$BOT_TOKEN" ] && [ -z "$NTFY_URL" ] && [ -z "$WEBHOOK_URL" ]; then
    echo "monitor.sh: не настроен ни один канал уведомлений (Telegram/ntfy/webhook)" >&2
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

ALERTS=()
add_alert() { ALERTS+=("$1"); }

# --- Также запустить удалённый мониторинг серверов других пользователей ---
python3 "$SCRIPT_DIR/monitor_remote.py" 2>/dev/null &

# --- 1. ДИСК (/) ---
DISK_PERCENT=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
if [ -n "$DISK_PERCENT" ] && [ "$DISK_PERCENT" -ge "$DISK_CRIT" ]; then
    if ! was_sent "disk_critical"; then
        add_alert "🔴 <b>ДИСК КРИТИЧНО:</b> ${DISK_PERCENT}% занято на /"
        mark_sent "disk_critical"
    fi
elif [ -n "$DISK_PERCENT" ] && [ "$DISK_PERCENT" -ge "$DISK_WARN" ]; then
    if ! was_sent "disk_warning"; then
        add_alert "🟡 <b>ДИСК:</b> ${DISK_PERCENT}% занято на /"
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
            add_alert "🟡 <b>HDD бэкапов:</b> ${HDD_PERCENT}% занято"
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
        add_alert "🔴 <b>RAM:</b> ${RAM_PERCENT}% (${RAM_USED}/${RAM_TOTAL})"
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
        add_alert "🟡 <b>SWAP:</b> ${SWAP_H} используется"
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
        add_alert "🔴 <b>CPU КРИТИЧНО:</b> утилизация ${CPU_PCT}%, load ${LOAD} на ${CORES} ядер (${LOAD_PCT}%/ядро)"
        mark_sent "cpu_critical"
    fi
    clear_mark "cpu_warning"
elif { [ -n "$CPU_PCT" ] && [ "$CPU_PCT" -ge "$CPU_WARN" ]; } || { [ -n "$LOAD_PCT" ] && [ "$LOAD_PCT" -ge "$LOAD_WARN" ]; }; then
    if ! was_sent "cpu_warning"; then
        add_alert "🟡 <b>CPU:</b> утилизация ${CPU_PCT}%, load ${LOAD} на ${CORES} ядер (${LOAD_PCT}%/ядро)"
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
            add_alert "🔴 <b>GPU ПЕРЕГРЕВ:</b> ${GPU_NAME} — ${GPU_TEMP}°C (нагрузка ${GPU_LOAD}%)"
            mark_sent "gpu_temp_critical"
        fi
    elif [ -n "$GPU_TEMP" ] && [ "$GPU_TEMP" -ge "$GPU_TEMP_WARN" ]; then
        if ! was_sent "gpu_temp_warning"; then
            add_alert "🟡 <b>GPU температура:</b> ${GPU_NAME} — ${GPU_TEMP}°C (нагрузка ${GPU_LOAD}%)"
            mark_sent "gpu_temp_warning"
        fi
    else
        clear_mark "gpu_temp_warning"; clear_mark "gpu_temp_critical"
    fi

    if [ -n "$GPU_LOAD" ] && [ "$GPU_LOAD" -ge "$GPU_LOAD_WARN" ]; then
        if ! was_sent "gpu_load_high"; then
            add_alert "🟡 <b>GPU нагрузка:</b> ${GPU_NAME} — ${GPU_LOAD}% (${GPU_TEMP}°C)"
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
            add_alert "🔴🔴🔴 <b>HDD SMART FAILED!</b> Требуется замена ${SMART_DISK}!"
            mark_sent "smart_fail"
        fi
    fi
    REALLOC=$(sudo -n smartctl -n standby -A "$SMART_DISK" 2>/dev/null | grep "Reallocated_Sector" | awk '{print $NF}')
    if [ -n "$REALLOC" ] && [ "$REALLOC" -gt 0 ]; then
        if ! was_sent "smart_realloc"; then
            add_alert "🟡 <b>HDD:</b> ${REALLOC} переназначенных секторов"
            mark_sent "smart_realloc"
        fi
    fi
    PENDING=$(sudo -n smartctl -n standby -A "$SMART_DISK" 2>/dev/null | grep "Current_Pending" | awk '{print $NF}')
    if [ -n "$PENDING" ] && [ "$PENDING" -gt 0 ]; then
        if ! was_sent "smart_pending"; then
            add_alert "🟡 <b>HDD:</b> ${PENDING} ожидающих секторов"
            mark_sent "smart_pending"
        fi
    fi
fi

# --- 7. КРИТИЧНЫЕ СЕРВИСЫ (должны быть active) ---
for svc in $CRITICAL_SERVICES; do
    if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
        [ "$svc" = "ssh" ] && systemctl is-active --quiet sshd 2>/dev/null && { clear_mark "svc_down_ssh"; continue; }
        if ! was_sent "svc_down_${svc}"; then
            add_alert "🔴 <b>Сервис не активен:</b> $(html_escape "$svc")"
            mark_sent "svc_down_${svc}"
        fi
    else
        clear_mark "svc_down_${svc}"
    fi
done

# --- 7.1 ЛЮБЫЕ упавшие systemd-юниты (авто-покрытие всех проектов) ---
while read -r unit; do
    [ -z "$unit" ] && continue
    case " $SERVICES_IGNORE " in *" $unit "*) continue ;; esac
    if ! was_sent "failed_${unit}"; then
        add_alert "🔴 <b>Сервис упал (failed):</b> $(html_escape "$unit")"
        mark_sent "failed_${unit}"
    fi
done < <(systemctl list-units --type=service --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}')

# --- 8. DOCKER: все контейнеры (авто-покрытие всех проектов) ---
if command -v docker &> /dev/null; then
    while IFS=$'\t' read -r cname cstate cstatus; do
        [ -z "$cname" ] && continue
        case " $DOCKER_IGNORE " in *" $cname "*) continue ;; esac
        if [ "$cstate" != "running" ]; then
            if ! was_sent "docker_down_${cname}"; then
                add_alert "🔴 <b>Контейнер не запущен:</b> $(html_escape "$cname") — ${cstate}"
                mark_sent "docker_down_${cname}"
            fi
        else
            clear_mark "docker_down_${cname}"
            if printf '%s' "$cstatus" | grep -q "(unhealthy)"; then
                if ! was_sent "docker_unhealthy_${cname}"; then
                    add_alert "🟡 <b>Контейнер unhealthy:</b> $(html_escape "$cname")"
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
            add_alert "⚠️ <b>Массовая атака:</b> ${BANNED} IP забанено (${F2B_JAIL})"
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
            add_alert "🔴 <b>Эндпоинт недоступен:</b> $(html_escape "$url") (HTTP ${code})"
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
            add_alert "🔴 <b>SSL ИСТЁК:</b> $(html_escape "$DOMAIN")!"
            mark_sent "ssl_expired_${dkey}"
        fi
    elif [ "$DAYS_LEFT" -le 14 ]; then
        if ! was_sent "ssl_${dkey}"; then
            add_alert "🟡 <b>SSL:</b> $(html_escape "$DOMAIN") истекает через ${DAYS_LEFT} дн."
            mark_sent "ssl_${dkey}"
        fi
    else
        clear_mark "ssl_${dkey}"; clear_mark "ssl_expired_${dkey}"
    fi
done

# --- ОТПРАВКА (только владельцу, реальные переносы строк) ---
if [ ${#ALERTS[@]} -gt 0 ]; then
    MSG="🖥 <b>Сервер $(html_escape "$HOSTNAME")</b>"$'\n\n'
    MSG+=$(printf '%s\n' "${ALERTS[@]}")
    MSG+=$'\n'"⏰ $(date '+%d.%m.%Y %H:%M')"
    notify "$MSG"
fi

# --- Сброс state раз в сутки (00:00–00:06) — чистит устаревшие маркеры ---
if [ "$(date +%H)" = "00" ] && [ "$(date +%M)" -lt 6 ]; then
    > "$STATE_FILE"
fi
