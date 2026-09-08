#!/bin/bash
# Установка сторожа. Идемпотентна: повторный запуск обновляет то, что изменилось.
#
# Ставит три вещи, и каждая — осознанно отдельно:
#   * исполнитель починок и каталог действий в /usr/local (root-owned, потому
#     что менять исполняемое от root должно быть шагом установки);
#   * правила sudoers — один бинарник на починки, три команды на чтение;
#   * таймер systemd, который ходит раз в пять минут под plg.
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SERVICE_USER="${WATCHDOG_USER:-plg}"   # под кем работает сторож (нужны credentials подписки)
BOT_USER="${BOT_USER:-tgbot}"          # под кем работает бот (выделенный, без sudo)
ENV_FILE="/etc/telegram-server-bot.env"

[ "$(id -u)" -eq 0 ] || { echo "❌ Запустите под root: sudo $0" >&2; exit 1; }
id "$SERVICE_USER" >/dev/null 2>&1 || { echo "❌ Нет пользователя $SERVICE_USER" >&2; exit 1; }

echo "▸ Проверка предпосылок"
[ -r "$ENV_FILE" ] || { echo "❌ Нет $ENV_FILE — сначала положите туда TELEGRAM_TOKEN и OWNER_ID" >&2; exit 1; }
grep -q '^TELEGRAM_TOKEN=.' "$ENV_FILE" || { echo "❌ В $ENV_FILE пуст TELEGRAM_TOKEN" >&2; exit 1; }
grep -q '^OWNER_ID=.'       "$ENV_FILE" || { echo "❌ В $ENV_FILE пуст OWNER_ID" >&2; exit 1; }
[ -x "$REPO/venv/bin/python" ] || { echo "❌ Нет venv в $REPO — создайте и поставьте зависимости" >&2; exit 1; }
"$REPO/venv/bin/python" -c 'import claude_agent_sdk' 2>/dev/null \
  || { echo "❌ В venv нет claude-agent-sdk: $REPO/venv/bin/pip install claude-agent-sdk" >&2; exit 1; }

echo "▸ Пользователь бота и общая группа"
# Бот смотрит в интернет, сторож — нет. Под общим пользователем компрометация
# бота означала бы всё, что может этот пользователь; выделенный tgbot ограничен
# одним исполнителем починок. Группа watchdog связывает их ровно там, где нужно:
# сторож пишет вердикты, бот их читает.
groupadd -f watchdog
id "$BOT_USER" >/dev/null 2>&1 || useradd --system --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin --gid watchdog "$BOT_USER"
usermod -aG watchdog "$SERVICE_USER"

echo "▸ Данные бота вне репозитория"
# Право записи в каталог с кодом позволило бы скомпрометированному боту
# переписать самого себя, поэтому изменяемые файлы живут отдельно.
install -d -m 2770 -o "$BOT_USER" -g watchdog /var/lib/telegram-bot
for f in users.json monitor_subscribers.json; do
    if [ -f "$REPO/$f" ] && [ ! -f "/var/lib/telegram-bot/$f" ]; then
        install -m 660 -o "$BOT_USER" -g watchdog "$REPO/$f" "/var/lib/telegram-bot/$f"
        echo "   перенесён $f"
    fi
done

echo "▸ Исполнитель починок и каталог действий"
install -d -m 755 -o root -g root /usr/local/lib/watchdog
install -m 644 -o root -g root "$REPO/watchdog/catalog.py"       /usr/local/lib/watchdog/catalog.py
install -m 755 -o root -g root "$REPO/watchdog/remedy-helper.py" /usr/local/sbin/watchdog-remedy

echo "▸ Правила sudoers"
TMP_SUDO="$(mktemp)"
cat > "$TMP_SUDO" <<EOF
# Сторож. Два вида доступа, оба узкие.
#
# 1) Исполнитель починок: один бинарник, который сам сверяет заявку с каталогом
#    действий и перепроверяет живое состояние системы. Доступен только боту —
#    кнопки нажимают в нём.
$BOT_USER ALL=(root) NOPASSWD: /usr/local/sbin/watchdog-remedy

# 2) Чтение телеметрии, недоступной обычному пользователю. Нужно сторожу, а не
#    боту. Всё остальное (df, docker, systemctl, journalctl) берётся без sudo.
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/smartctl -H /dev/sd[a-z], /usr/sbin/smartctl -H /dev/nvme[0-9]n[0-9]
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/smartctl -a /dev/sd[a-z], /usr/sbin/smartctl -a /dev/nvme[0-9]n[0-9]
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/cscli decisions list -o json
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/fail2ban-client status
EOF
# Битый файл sudoers ломает sudo целиком, поэтому проверяем ДО установки.
visudo -c -f "$TMP_SUDO" >/dev/null || { echo "❌ sudoers не проходит проверку" >&2; rm -f "$TMP_SUDO"; exit 1; }
install -m 440 -o root -g root "$TMP_SUDO" /etc/sudoers.d/watchdog
rm -f "$TMP_SUDO"

echo "▸ Каталог состояния"
# setgid: всё созданное внутри достаётся группе watchdog, иначе бот перестал бы
# читать вердикты после первого же нового инцидента.
install -d -m 2750 -o "$SERVICE_USER" -g watchdog /var/lib/watchdog
install -d -m 2770 -o "$SERVICE_USER" -g watchdog /var/lib/watchdog/incidents

echo "▸ Юниты systemd"
install -m 644 -o root -g root "$REPO/watchdog/systemd/watchdog.service" /etc/systemd/system/watchdog.service
install -m 644 -o root -g root "$REPO/watchdog/systemd/watchdog.timer"   /etc/systemd/system/watchdog.timer
install -m 644 -o root -g root "$REPO/watchdog/systemd/telegram-server-bot.service" \
        /etc/systemd/system/telegram-server-bot.service
systemctl daemon-reload
systemctl try-restart telegram-server-bot.service || true

echo "▸ Первый проход: запоминаем базу"
# База нужна до включения таймера: иначе первое же срабатывание доложит обо всём,
# что накопилось за годы, как будто это случилось только что.
systemctl start watchdog.service
systemctl enable --now watchdog.timer >/dev/null

echo
echo "✅ Сторож установлен."
systemctl list-timers watchdog.timer --no-pager | head -3
echo
echo "   Журнал:    journalctl -u watchdog.service -f"
echo "   Аудит:     /var/log/watchdog-remedy.log"
echo "   Инциденты: /var/lib/watchdog/incidents/"
