#!/bin/bash
# ============================================
# Proactive Server Monitor — Telegram Alerts
# Читает подписчиков из monitor_subscribers.json
# Cron: */5 * * * * /home/plg/telegram-server-bot/monitor.sh
# ============================================

BOT_TOKEN="***REVOKED_SECRET_REMOVED***"
# Локальные алерты получает ТОЛЬКО владелец сервера
OWNER_ID="1148520376"
STATE_FILE="/tmp/server-monitor-state"
HOSTNAME=$(hostname)

send_alert() {
    local msg="$1"
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="$OWNER_ID" \
        -d text="$msg" \
        -d parse_mode="HTML" > /dev/null 2>&1
}

# Не спамить — трекаем что уже отправили
was_sent() {
    grep -qF "$1" "$STATE_FILE" 2>/dev/null
}
mark_sent() {
    echo "$1" >> "$STATE_FILE"
}
clear_mark() {
    sed -i "/$1/d" "$STATE_FILE" 2>/dev/null
}

# Также запустить удалённый мониторинг для других пользователей
python3 /home/plg/telegram-server-bot/monitor_remote.py 2>/dev/null &

ALERTS=""

# --- 1. ДИСК (SSD) ---
DISK_PERCENT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PERCENT" -ge 90 ]; then
    if ! was_sent "disk_critical"; then
        ALERTS+="🔴 <b>ДИСК КРИТИЧНО:</b> ${DISK_PERCENT}% занято на /\n"
        mark_sent "disk_critical"
    fi
elif [ "$DISK_PERCENT" -ge 80 ]; then
    if ! was_sent "disk_warning"; then
        ALERTS+="🟡 <b>ДИСК:</b> ${DISK_PERCENT}% занято на /\n"
        mark_sent "disk_warning"
    fi
else
    clear_mark "disk_warning"
    clear_mark "disk_critical"
fi

# --- 2. HDD бэкапов ---
HDD_PERCENT=$(df /mnt/hdd 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%')
if [ -n "$HDD_PERCENT" ] && [ "$HDD_PERCENT" -ge 85 ]; then
    if ! was_sent "hdd_warning"; then
        ALERTS+="🟡 <b>HDD бэкапов:</b> ${HDD_PERCENT}% занято\n"
        mark_sent "hdd_warning"
    fi
else
    clear_mark "hdd_warning"
fi

# --- 3. RAM ---
RAM_PERCENT=$(free | awk '/Mem:/ {printf "%.0f", $3/$2 * 100}')
if [ "$RAM_PERCENT" -ge 90 ]; then
    if ! was_sent "ram_critical"; then
        RAM_USED=$(free -h | awk '/Mem:/ {print $3}')
        RAM_TOTAL=$(free -h | awk '/Mem:/ {print $2}')
        ALERTS+="🔴 <b>RAM:</b> ${RAM_PERCENT}% (${RAM_USED}/${RAM_TOTAL})\n"
        mark_sent "ram_critical"
    fi
elif [ "$RAM_PERCENT" -lt 80 ]; then
    clear_mark "ram_critical"
fi

# --- 4. SWAP ---
SWAP_USED=$(free | awk '/Swap:/ {print $3}')
if [ "$SWAP_USED" -gt 1048576 ]; then
    if ! was_sent "swap_warning"; then
        SWAP_H=$(free -h | awk '/Swap:/ {print $3}')
        ALERTS+="🟡 <b>SWAP:</b> ${SWAP_H} используется\n"
        mark_sent "swap_warning"
    fi
else
    clear_mark "swap_warning"
fi

# --- 5. LOAD AVERAGE ---
LOAD=$(awk '{print $1}' /proc/loadavg)
CORES=$(nproc)
LOAD_INT=$(echo "$LOAD * 100 / $CORES" | bc 2>/dev/null || echo "0")
if [ "$LOAD_INT" -ge 200 ]; then
    if ! was_sent "load_critical"; then
        ALERTS+="🔴 <b>CPU:</b> Load ${LOAD} (${CORES} ядер)\n"
        mark_sent "load_critical"
    fi
elif [ "$LOAD_INT" -lt 150 ]; then
    clear_mark "load_critical"
fi

# --- 6. HDD SMART ---
if command -v smartctl &> /dev/null; then
    SMART_STATUS=$(sudo smartctl -H /dev/sda 2>/dev/null | grep -i "result" | awk '{print $NF}')
    if [ "$SMART_STATUS" != "PASSED" ] && [ -n "$SMART_STATUS" ]; then
        if ! was_sent "smart_fail"; then
            ALERTS+="🔴🔴🔴 <b>HDD SMART FAILED!</b> Требуется замена!\n"
            mark_sent "smart_fail"
        fi
    fi
    REALLOC=$(sudo smartctl -A /dev/sda 2>/dev/null | grep "Reallocated_Sector" | awk '{print $NF}')
    if [ -n "$REALLOC" ] && [ "$REALLOC" -gt 0 ]; then
        if ! was_sent "smart_realloc"; then
            ALERTS+="🟡 <b>HDD:</b> ${REALLOC} переназначенных секторов\n"
            mark_sent "smart_realloc"
        fi
    fi
    PENDING=$(sudo smartctl -A /dev/sda 2>/dev/null | grep "Current_Pending" | awk '{print $NF}')
    if [ -n "$PENDING" ] && [ "$PENDING" -gt 0 ]; then
        if ! was_sent "smart_pending"; then
            ALERTS+="🟡 <b>HDD:</b> ${PENDING} ожидающих секторов\n"
            mark_sent "smart_pending"
        fi
    fi
    TEMP=$(sudo smartctl -A /dev/sda 2>/dev/null | grep "Temperature_Celsius" | awk '{print $10}')
    if [ -n "$TEMP" ] && [ "$TEMP" -ge 50 ]; then
        if ! was_sent "hdd_temp"; then
            ALERTS+="🟡 <b>HDD температура:</b> ${TEMP}°C\n"
            mark_sent "hdd_temp"
        fi
    else
        clear_mark "hdd_temp"
    fi
fi

# --- 7. СЕРВИСЫ ---
SERVICES="nginx postgresql docker fail2ban crowdsec ssh"
DOWN_SERVICES=""
for svc in $SERVICES; do
    if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
        [ "$svc" = "ssh" ] && systemctl is-active --quiet sshd 2>/dev/null && continue
        DOWN_SERVICES+="$svc "
    fi
done
if [ -n "$DOWN_SERVICES" ]; then
    if ! was_sent "svc_down"; then
        ALERTS+="🔴 <b>СЕРВИСЫ УПАЛИ:</b> ${DOWN_SERVICES}\n"
        mark_sent "svc_down"
    fi
else
    clear_mark "svc_down"
fi

# --- 8. Docker ---
UNHEALTHY=$(docker ps --filter "health=unhealthy" --format "{{.Names}}" 2>/dev/null | tr '\n' ', ')
if [ -n "$UNHEALTHY" ]; then
    if ! was_sent "docker_unhealthy"; then
        ALERTS+="🟡 <b>Docker unhealthy:</b> ${UNHEALTHY}\n"
        mark_sent "docker_unhealthy"
    fi
else
    clear_mark "docker_unhealthy"
fi

# --- 9. fail2ban ---
BANNED=$(sudo fail2ban-client status sshd 2>/dev/null | grep "Currently banned" | awk '{print $NF}')
if [ -n "$BANNED" ] && [ "$BANNED" -ge 20 ]; then
    if ! was_sent "f2b_mass_ban"; then
        ALERTS+="⚠️ <b>Массовая атака:</b> ${BANNED} IP забанено\n"
        mark_sent "f2b_mass_ban"
    fi
elif [ -n "$BANNED" ] && [ "$BANNED" -lt 10 ]; then
    clear_mark "f2b_mass_ban"
fi

# --- 10. SSL ---
for cert in /etc/letsencrypt/live/*/fullchain.pem; do
    [ -f "$cert" ] || continue
    DOMAIN=$(basename $(dirname "$cert"))
    EXPIRY=$(openssl x509 -enddate -noout -in "$cert" 2>/dev/null | cut -d= -f2)
    EXPIRY_EPOCH=$(date -d "$EXPIRY" +%s 2>/dev/null)
    NOW_EPOCH=$(date +%s)
    DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))
    if [ "$DAYS_LEFT" -le 0 ]; then
        if ! was_sent "ssl_expired_${DOMAIN}"; then
            ALERTS+="🔴 <b>SSL ИСТЁК:</b> ${DOMAIN}!\n"
            mark_sent "ssl_expired_${DOMAIN}"
        fi
    elif [ "$DAYS_LEFT" -le 14 ]; then
        if ! was_sent "ssl_${DOMAIN}"; then
            ALERTS+="🟡 <b>SSL:</b> ${DOMAIN} истекает через ${DAYS_LEFT} дней\n"
            mark_sent "ssl_${DOMAIN}"
        fi
    else
        clear_mark "ssl_${DOMAIN}"
        clear_mark "ssl_expired_${DOMAIN}"
    fi
done

# --- ОТПРАВКА (только владельцу) ---
if [ -n "$ALERTS" ]; then
    MSG="🖥 <b>Сервер ${HOSTNAME}</b>\n\n${ALERTS}\n⏰ $(date '+%d.%m.%Y %H:%M')"
    send_alert "$MSG"
fi

# Очистить state файл раз в сутки (00:00-00:05)
HOUR=$(date +%H)
MIN=$(date +%M)
if [ "$HOUR" = "00" ] && [ "$MIN" -lt 6 ]; then
    > "$STATE_FILE"
fi
