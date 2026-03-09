#!/usr/bin/env python3
"""
Удалённый мониторинг серверов пользователей через agent API.
Запускается по cron каждые 5 минут.
Каждый пользователь получает алерты ТОЛЬКО о СВОИХ серверах.
"""

import json
import os
import requests
import sys

BOT_TOKEN = "***REVOKED_SECRET_REMOVED***"
OWNER_ID = "1148520376"
USERS_FILE = "/home/plg/telegram-server-bot/users.json"
SUBS_FILE = "/home/plg/telegram-server-bot/monitor_subscribers.json"
STATE_FILE = "/tmp/server-monitor-remote-state.json"


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def send_alert(chat_id, msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass


def main():
    users = load_json(USERS_FILE)
    subs = load_json(SUBS_FILE)
    state = load_json(STATE_FILE)

    for user_id, sub_cfg in subs.items():
        if not sub_cfg.get("enabled"):
            continue

        # Владелец получает локальные алерты от monitor.sh — пропускаем
        if user_id == OWNER_ID:
            continue

        user_data = users.get(user_id, {})
        servers = user_data.get("servers", {})
        if not servers:
            continue

        disk_warn = sub_cfg.get("disk_warn", 80)
        ram_warn = sub_cfg.get("ram_warn", 90)

        user_state = state.setdefault(user_id, {})

        for server_name, server_info in servers.items():
            ip = server_info.get("server_ip", "")
            key = server_info.get("secret_key", "")
            if not ip or not key:
                continue

            srv_state = user_state.setdefault(server_name, {})
            alerts = []

            try:
                resp = requests.get(
                    f"http://{ip}:5000/status",
                    headers={"X-Secret-Key": key},
                    timeout=10
                )
                resp.raise_for_status()
                data = resp.json()

                # Сервер доступен — убрать алерт недоступности
                if srv_state.get("offline"):
                    srv_state["offline"] = False

                # Диск
                disk_pct = float(data.get("disk", {}).get("percent", 0))
                if disk_pct >= disk_warn + 10:
                    if not srv_state.get("disk_crit"):
                        alerts.append(f"🔴 <b>ДИСК КРИТИЧНО:</b> {disk_pct}%")
                        srv_state["disk_crit"] = True
                elif disk_pct >= disk_warn:
                    if not srv_state.get("disk_warn"):
                        alerts.append(f"🟡 <b>ДИСК:</b> {disk_pct}% (порог {disk_warn}%)")
                        srv_state["disk_warn"] = True
                else:
                    srv_state["disk_crit"] = False
                    srv_state["disk_warn"] = False

                # RAM
                ram_pct = float(data.get("memory", {}).get("percent", 0))
                if ram_pct >= ram_warn:
                    if not srv_state.get("ram_warn"):
                        used = data.get("memory", {}).get("used", "?")
                        total = data.get("memory", {}).get("total", "?")
                        alerts.append(f"🔴 <b>RAM:</b> {ram_pct}% ({used}/{total} GB)")
                        srv_state["ram_warn"] = True
                elif ram_pct < ram_warn - 10:
                    srv_state["ram_warn"] = False

                # CPU
                cpu_pct = float(data.get("cpu", 0))
                if cpu_pct >= 90:
                    if not srv_state.get("cpu_warn"):
                        alerts.append(f"🔴 <b>CPU:</b> {cpu_pct}%")
                        srv_state["cpu_warn"] = True
                elif cpu_pct < 70:
                    srv_state["cpu_warn"] = False

            except requests.exceptions.RequestException:
                if not srv_state.get("offline"):
                    alerts.append("⛔️ <b>Сервер недоступен!</b> Агент не отвечает.")
                    srv_state["offline"] = True

            if alerts:
                msg = f"🖥 <b>Сервер «{server_name}»</b> ({ip})\n\n"
                msg += "\n".join(alerts)
                send_alert(user_id, msg)

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
