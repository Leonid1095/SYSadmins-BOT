# agent.py (Финальная версия для Gunicorn)

from flask import Flask, request, jsonify
import psutil
import os

# Gunicorn получит эту переменную из systemd сервиса
SECRET_KEY = os.getenv("SECRET_KEY")

# Эту переменную `app` ищет Gunicorn (из команды agent:app)
app = Flask(__name__)

def get_cpu_usage():
    """Возвращает использование CPU в процентах."""
    return psutil.cpu_percent(interval=1)

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

@app.before_request
def check_secret_key():
    """Проверяет секретный ключ перед каждым запросом."""
    # Выдаем ошибку 403 (Forbidden), если ключи не совпадают
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "Invalid secret key"}), 403

@app.route('/status', methods=['GET'])
def get_status():
    """Основной эндпоинт, который отдает всю статистику."""
    # Проверяем, задан ли ключ вообще
    if not SECRET_KEY:
        return jsonify({"error": "SECRET_KEY is not configured on the agent"}), 500

    return jsonify({
        "cpu": get_cpu_usage(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
    })
