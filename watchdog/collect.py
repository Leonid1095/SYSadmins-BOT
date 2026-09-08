#!/usr/bin/env python3
"""Детекторы сторожа: снимок состояния машины в виде фактов.

Здесь нет ни модели, ни эвристик «важно/неважно» — только измерения. Решение о
том, что считать событием, принимает delta.py; разделение намеренное, чтобы
сравнивать снимки можно было без риска, что вчерашний порог поменялся сегодня.

Работает на голой stdlib и системном python3: запускается из-под root по таймеру,
где venv проекта недоступен. Каждый детектор изолирован — сломавшийся smartctl не
должен унести с собой весь снимок, поэтому его ошибка становится обычным фактом
в разделе "errors".
"""

import concurrent.futures
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Таймауты жёсткие сознательно: сторож ходит по таймеру каждые 5 минут, и
# зависший smartctl не должен наложиться на следующий запуск.
CMD_TIMEOUT = 15
NET_TIMEOUT = 8
# Обходной маршрут до удалённых агентов идёт через VPN и заметно медленнее:
# измеренный ответ приходил за ~17 секунд. Прямой путь оставляем быстрым, чтобы
# не ждать впустую там, где связи просто нет.
REMOTE_DIRECT_TIMEOUT = 8
REMOTE_PROXY_TIMEOUT = 40

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONF = os.path.join(BASE_DIR, "monitor.local.conf")
DATA_DIR = os.environ.get("BOT_DATA_DIR", BASE_DIR)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
OWNER_ID = os.environ.get("OWNER_ID", "")

sys.path.insert(0, BASE_DIR)
import agent_auth  # noqa: E402 — подпись запросов к агентам


def _run_priv(cmd, timeout=CMD_TIMEOUT):
    """Как _run, но для трёх команд, которым нужен root.

    Сборщик работает под plg — тем же пользователем, что и аналитик, потому что
    подписка Max привязана к его credentials. Ради smartctl поднимать весь
    сборщик до root не станем: узкие правила sudoers дешевле, чем root-процесс,
    ходящий в сеть за сертификатами.
    """
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    return _run(cmd, timeout)


def _run(cmd, timeout=CMD_TIMEOUT):
    """Запускает команду и возвращает stdout. Любой сбой — это None, не исключение."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


class Collector:
    """Собирает факты, складывая ошибки детекторов рядом с результатами."""

    def __init__(self):
        self.facts = {}
        self.errors = {}

    def detect(self, name, fn):
        """Выполняет детектор, превращая его падение в запись в errors."""
        try:
            value = fn()
        except Exception as exc:  # детектор не имеет права уронить снимок
            self.errors[name] = f"{type(exc).__name__}: {exc}"
            return
        if value is not None:
            self.facts[name] = value

    # --- Железо и ёмкости -------------------------------------------------

    def disk(self):
        """Занятость файловых систем в процентах, целыми числами."""
        out = {}
        for label, path in (("root", "/"), ("hdd", "/mnt/hdd")):
            if not os.path.ismount(path) and label != "root":
                continue
            usage = shutil.disk_usage(path)
            out[label] = {
                "pct": round(usage.used / usage.total * 100),
                "free_gb": round(usage.free / 1024**3, 1),
            }
        return out

    def memory(self):
        """RAM и swap из /proc/meminfo — без psutil, его на root-питоне нет."""
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # значения в кБ
        total = info["MemTotal"]
        available = info.get("MemAvailable", info["MemFree"])
        swap_total = info.get("SwapTotal", 0)
        swap_used = swap_total - info.get("SwapFree", 0)
        return {
            "used_pct": round((total - available) / total * 100),
            "total_gb": round(total / 1024**2, 1),
            "swap_pct": round(swap_used / swap_total * 100) if swap_total else 0,
        }

    def cpu(self):
        """Средняя нагрузка, нормированная на число ядер.

        Мгновенную утилизацию намеренно не берём: снимок раз в 5 минут поймал бы
        случайный всплеск и создал ложную дельту. loadavg усредняет сам.
        """
        cores = os.cpu_count() or 1
        load1, load5, load15 = os.getloadavg()
        return {
            "cores": cores,
            "load_per_core_pct": round(load5 / cores * 100),
            "load1": round(load1, 2),
            "load15": round(load15, 2),
        }

    def temperature(self):
        """Температура CPU из hwmon, если ядро её отдаёт."""
        best = None
        for base in sorted(os.listdir("/sys/class/hwmon")):
            path = os.path.join("/sys/class/hwmon", base)
            for entry in sorted(os.listdir(path)):
                if not re.fullmatch(r"temp\d+_input", entry):
                    continue
                try:
                    with open(os.path.join(path, entry), encoding="utf-8") as f:
                        value = int(f.read().strip()) / 1000
                except (OSError, ValueError):
                    continue
                if 0 < value < 150 and (best is None or value > best):
                    best = value
        return {"cpu_c": round(best)} if best is not None else None

    # --- Сервисы ----------------------------------------------------------

    def systemd(self):
        """Юниты в состоянии failed. Именно список, а не счётчик: дельте важно,
        какой именно юнит упал, иначе замена одной поломки другой пройдёт молча."""
        out = _run(["systemctl", "list-units", "--state=failed",
                    "--no-legend", "--plain", "--no-pager"])
        if out is None:
            return None
        failed = [line.split()[0] for line in out.splitlines() if line.strip()]
        return {"failed": sorted(failed)}

    def docker(self):
        """Контейнеры не в порядке, разложенные по типу беды."""
        out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
        if out is None:
            return None
        stopped, unhealthy, restarting = [], [], []
        total = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            total += 1
            name, state, status = parts
            if "unhealthy" in status.lower():
                unhealthy.append(name)
            elif state == "restarting":
                restarting.append(name)
            elif state != "running":
                stopped.append(name)
        return {
            # Общее число нужно, чтобы бот мог сказать «работают 47 из 54».
            # Само по себе оно события не рождает: delta.py сравнивает только
            # списки имён, а не счётчик, иначе один поднятый контейнер выглядел
            # бы изменением инфраструктуры.
            "total": total,
            "stopped": sorted(stopped),
            "unhealthy": sorted(unhealthy),
            "restarting": sorted(restarting),
        }

    # --- Диски и сеть -----------------------------------------------------

    def smart(self):
        """Вердикт SMART по физическим дискам."""
        listing = _run(["lsblk", "-dno", "NAME,TYPE"])
        if listing is None:
            return None
        verdicts = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[1] != "disk":
                continue
            dev = f"/dev/{parts[0]}"
            if not re.fullmatch(r"/dev/[a-z0-9]{1,16}", dev):
                continue  # имя уходит в привилегированный вызов — форму проверяем заранее
            out = _run_priv(["/usr/sbin/smartctl", "-H", dev], timeout=CMD_TIMEOUT)
            if out is None:
                continue
            match = re.search(r"(?:overall-health self-assessment test result|SMART Health Status):\s*(\S+)", out)
            if match:
                verdicts[dev] = match.group(1)
        return verdicts or None

    def endpoints(self):
        """HTTP-код и остаток дней у TLS-сертификата для доменов из конфига."""
        targets = self._configured_endpoints()
        if not targets:
            return None
        # Прокси обходим намеренно. Юниту он задан ради Telegram и модели, но
        # свои же домены надо проверять так, как их видит обычный посетитель, —
        # иначе сторож будет докладывать о доступности чужого выходного узла.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        out = {}
        for url in targets:
            entry = {}
            try:
                req = urllib.request.Request(url, method="GET",
                                             headers={"User-Agent": "watchdog/1.0"})
                with opener.open(req, timeout=NET_TIMEOUT) as resp:
                    entry["http"] = resp.status
            except urllib.error.HTTPError as exc:
                entry["http"] = exc.code
            except Exception as exc:
                entry["http"] = None
                entry["error"] = type(exc).__name__
            days = self._cert_days_left(url)
            if days is not None:
                entry["cert_days"] = days
            out[url] = entry
        return out

    def _configured_endpoints(self):
        """Достаёт ENDPOINTS=(...) из monitor.local.conf.

        Конфиг — bash-файл, но исполнять его ради одного массива не станем:
        сторож не должен запускать чужой код, чтобы узнать список доменов.
        """
        try:
            with open(LOCAL_CONF, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return []
        match = re.search(r"^\s*ENDPOINTS=\(([^)]*)\)", text, re.MULTILINE)
        if not match:
            return []
        return re.findall(r'https?://[^\s"\']+', match.group(1))

    def _cert_days_left(self, url):
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
        if not url.startswith("https://"):
            return None
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=NET_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    not_after = tls.getpeercert()["notAfter"]
        except Exception:
            return None
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (expires - datetime.now(timezone.utc)).days

    def remote(self):
        """Серверы владельца, опрашиваемые через их агентов.

        До этого удалённые серверы не мониторил никто: monitor.sh следит только
        за локальной машиной, а monitor_remote.py по устройству пропускал
        владельца. Получалось, что чем сервер важнее, тем меньше о нём известно.
        """
        servers = self._owner_servers()
        if not servers:
            return None

        targets = {name: info for name, info in servers.items()
                   if info.get("server_ip") and info.get("secret_key")}
        if not targets:
            return None

        # Опрашиваем параллельно. Последовательно два молчащих сервера съедали
        # минуту на одних таймаутах, и снимок переставал быть снимком: к концу
        # обхода начало уже устаревало.
        out = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(self._probe_agent, info["server_ip"], info["secret_key"]): name
                for name, info in targets.items()
            }
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    out[name] = future.result()
                except Exception as exc:
                    out[name] = {"ip": targets[name]["server_ip"], "reachable": False,
                                 "error": f"опрос сорвался: {type(exc).__name__}"}
        return out or None

    def _probe_agent(self, ip, key):
        """Опрашивает агента, пробуя оба пути до него.

        Часть серверов у провайдера владельца напрямую недостижима, но
        открывается через тот же мост, которым ходит бот. Пробуем сначала
        напрямую — так быстрее и честнее, — и лишь потом через прокси,
        запоминая, что сработало: это половина ответа на вопрос «почему молчит».
        """
        entry = {"ip": ip}
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        routes = [("напрямую",
                   urllib.request.build_opener(urllib.request.ProxyHandler({})),
                   REMOTE_DIRECT_TIMEOUT)]
        if proxy:
            routes.append(("через прокси",
                           urllib.request.build_opener(urllib.request.ProxyHandler(
                               {"http": proxy, "https": proxy})),
                           REMOTE_PROXY_TIMEOUT))

        problems = []
        for label, opener, timeout in routes:
            headers = agent_auth.build_headers(key, "GET", "/status")
            req = urllib.request.Request(f"http://{ip}:5000/status", headers=headers)
            try:
                with opener.open(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                # Агент ответил, пусть и отказом: путь до него рабочий, искать
                # другой незачем — проблема на самом сервере.
                hint = {403: "агент не принял подпись (старая версия?)",
                        503: "у агента не настроен ключ"}.get(exc.code, "")
                entry.update({"reachable": False, "via": label, "http": exc.code,
                              "error": f"агент отвечает {exc.code}"
                                       + (f" — {hint}" if hint else "")})
                return entry
            except Exception as exc:
                reason = getattr(exc, "reason", None) or type(exc).__name__
                problems.append(f"{label}: {reason}")
                continue

            entry.update({
                "reachable": True,
                "via": label,
                "disk_pct": float(data.get("disk", {}).get("percent", 0)),
                "mem_pct": float(data.get("memory", {}).get("percent", 0)),
                "cpu_pct": float(data.get("cpu", 0)),
            })
            if data.get("gpu"):
                entry["gpu_temp"] = data["gpu"].get("temp")
            return entry

        entry.update({"reachable": False, "error": "; ".join(problems) or "нет связи"})
        return entry

    def _owner_servers(self):
        """Серверы владельца из users.json. Чужие сторожа не касаются."""
        if not OWNER_ID:
            return {}
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                users = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return (users.get(str(OWNER_ID)) or {}).get("servers") or {}

    def security(self):
        """Активные баны — косвенный признак, что машину щупают активнее обычного."""
        out = {}
        crowdsec = _run_priv(["/usr/bin/cscli", "decisions", "list", "-o", "json"])
        if crowdsec is not None:
            try:
                decisions = json.loads(crowdsec) or []
                out["crowdsec_bans"] = len(decisions)
            except json.JSONDecodeError:
                pass
        f2b = _run_priv(["/usr/bin/fail2ban-client", "status"])
        if f2b is not None:
            match = re.search(r"Jail list:\s*(.*)", f2b)
            if match:
                out["fail2ban_jails"] = len([j for j in match.group(1).split(",") if j.strip()])
        return out or None


def collect():
    c = Collector()
    c.detect("disk", c.disk)
    c.detect("memory", c.memory)
    c.detect("cpu", c.cpu)
    c.detect("temperature", c.temperature)
    c.detect("systemd", c.systemd)
    c.detect("docker", c.docker)
    c.detect("smart", c.smart)
    c.detect("endpoints", c.endpoints)
    c.detect("security", c.security)
    c.detect("remote", c.remote)

    snapshot = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "facts": c.facts,
    }
    if c.errors:
        snapshot["errors"] = c.errors
    return snapshot


if __name__ == "__main__":
    json.dump(collect(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
