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
import tempfile

# Токен и OWNER_ID берём из единого источника config.py (в .gitignore),
# с фолбэком на переменные окружения. Никакого хардкода секретов в этом файле.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import config
    BOT_TOKEN = config.TELEGRAM_TOKEN
    OWNER_ID = str(config.OWNER_ID)
except Exception:
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    OWNER_ID = os.getenv("OWNER_ID", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
SUBS_FILE = os.path.join(BASE_DIR, "monitor_subscribers.json")
STATE_FILE = f"/tmp/server-monitor-remote-state-{os.getuid()}.json"


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    """Атомарная запись: пишем во временный файл и переименовываем."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        gpu_temp_warn = sub_cfg.get("gpu_temp_warn", 80)
        cpu_warn = int(sub_cfg.get("cpu_warn", 90))
        # Миграция старых «load-style» значений (100..500) в проценты 0..100
        if cpu_warn > 100:
            cpu_warn = 90

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

                # CPU — используем пользовательский порог cpu_warn (в процентах)
                cpu_pct = float(data.get("cpu", 0))
                if cpu_pct >= cpu_warn:
                    if not srv_state.get("cpu_warn"):
                        alerts.append(f"🔴 <b>CPU:</b> {cpu_pct}% (порог {cpu_warn}%)")
                        srv_state["cpu_warn"] = True
                elif cpu_pct < cpu_warn - 10:
                    srv_state["cpu_warn"] = False

                # GPU
                gpu = data.get("gpu")
                if gpu:
                    gpu_temp = gpu.get("temp", 0)
                    gpu_load = gpu.get("load", 0)
                    gpu_name = gpu.get("name", "GPU")
                    if gpu_temp >= gpu_temp_warn:
                        if not srv_state.get("gpu_temp_warn"):
                            alerts.append(
                                f"🔴 <b>GPU ПЕРЕГРЕВ:</b> {gpu_name} — {gpu_temp}°C "
                                f"(нагрузка {gpu_load}%)"
                            )
                            srv_state["gpu_temp_warn"] = True
                    elif gpu_temp < gpu_temp_warn - 10:
                        srv_state["gpu_temp_warn"] = False

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
