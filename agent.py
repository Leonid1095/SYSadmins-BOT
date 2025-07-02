from flask import Flask, request, jsonify
import psutil
import os

SECRET_KEY = os.getenv("SECRET_KEY")

app = Flask(__name__)

def get_cpu_usage():
    return psutil.cpu_percent(interval=1)

def get_memory_info():
    memory = psutil.virtual_memory()
    return {
        "total": f"{memory.total / (1024**3):.2f}",
        "used": f"{memory.used / (1024**3):.2f}",
        "percent": memory.percent
    }

def get_disk_info():
    disk = psutil.disk_usage('/')
    return {
        "total": f"{disk.total / (1024**3):.2f}",
        "used": f"{disk.used / (1024**3):.2f}",
        "percent": disk.percent
    }

@app.before_request
def check_secret_key():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "Invalid secret key"}), 403

@app.route('/status', methods=['GET'])
def get_status():
    return jsonify({
        "cpu": get_cpu_usage(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
    })

if __name__ == '__main__':
    if SECRET_KEY is None:
        print("ОШИБКА: Переменная окружения SECRET_KEY не установлена.")
    else:
        app.run(host='0.0.0.0', port=5000)
