# agent.py (Финальная версия для Gunicorn)

from flask import Flask, request, jsonify
import psutil
import subprocess
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_auth import NonceCache, verify

# Gunicorn получит эту переменную из systemd сервиса
SECRET_KEY = os.getenv("SECRET_KEY")

# Помнит недавние nonce, чтобы перехваченную подпись нельзя было повторить.
# Гоняем агента одним воркером (см. install.sh) — кэш общий для всех запросов.
NONCE_CACHE = NonceCache()

# Эту переменную `app` ищет Gunicorn (из команды agent:app)
app = Flask(__name__)

def get_cpu_usage():
    """Возвращает использование CPU в процентах."""
    return psutil.cpu_percent(interval=1)

def get_cpu_temp():
    """Возвращает температуру CPU."""
    temps = psutil.sensors_temperatures()
    if not temps:
        return None
    # Пробуем разные источники: coretemp (Intel), k10temp (AMD), cpu_thermal (ARM)
    for key in ("coretemp", "k10temp", "cpu_thermal", "zenpower"):
        if key in temps:
            entries = temps[key]
            if entries:
                return round(max(e.current for e in entries))
    # Если не нашли по имени — берём первый доступный
    first = list(temps.values())[0]
    if first:
        return round(max(e.current for e in first))
    return None

def get_memory_info():
    """Возвращает информацию об оперативной памяти."""
    memory = psutil.virtual_memory()
    return {
        "total": f"{memory.total / (1024**3):.2f}",
        "used": f"{memory.used / (1024**3):.2f}",
        "percent": memory.percent
    }

def get_disk_info():
    """Возвращает информацию о дисковом пространстве."""
    disk = psutil.disk_usage('/')
    return {
        "total": f"{disk.total / (1024**3):.2f}",
        "used": f"{disk.used / (1024**3):.2f}",
        "percent": disk.percent
    }

def get_gpu_info():
    """Возвращает информацию о видеокарте (нагрузка и температура) через nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split('\n')[0]
        parts = [p.strip() for p in line.split(',')]
        return {
            "name": parts[0],
            "load": int(parts[1]),
            "temp": int(parts[2]),
        }
    except (FileNotFoundError, IndexError, ValueError, subprocess.TimeoutExpired):
        return None

@app.before_request
def check_signature():
    """Проверяет подпись запроса перед каждым маршрутом.

    Раньше здесь принимался сам секрет заголовком X-Secret-Key. Агент отвечает
    по обычному HTTP, поэтому любой на пути трафика получал готовый ключ и мог
    опрашивать сервер сколько угодно. Теперь приходит только подпись, годная для
    этого запроса и на две минуты; ключ машину не покидает.
    """
    # Ключ не сконфигурирован — не пускаем никого (fail closed)
    if not SECRET_KEY:
        return jsonify({"error": "SECRET_KEY is not configured on the agent"}), 503

    ok, reason = verify(SECRET_KEY, request.method, request.path,
                        request.headers, NONCE_CACHE)
    if not ok:
        # Причину называем: она не подсказывает ключ, а без неё разбирать
        # рассинхрон часов на чужом сервере невозможно.
        return jsonify({"error": reason}), 403

@app.route('/status', methods=['GET'])
def get_status():
    """Основной эндпоинт, который отдает всю статистику."""
    cpu_temp = get_cpu_temp()
    data = {
        "cpu": get_cpu_usage(),
        "cpu_temp": cpu_temp,
        "memory": get_memory_info(),
        "disk": get_disk_info(),
    }
    gpu = get_gpu_info()
    if gpu is not None:
        data["gpu"] = gpu
    return jsonify(data)
