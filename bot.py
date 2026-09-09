"""PLGames Admin — Telegram-бот администрирования серверов.

Отвечает только владельцу (OWNER_ID); все остальные update'ы отбрасываются в
группе -1 до всех обработчиков.

Три источника данных, намеренно разные:
  * снимок сторожа (infra.py) — состояние центрального сервера и инфраструктуры
    на нём; собирать это самому боту нечем и не нужно;
  * агенты на удалённых серверах — по подписанным запросам (agent_auth.py);
  * вердикты сторожа — кнопки починки, исполняемые root-хелпером через каталог.

Сообщения пишутся человеческим языком: цифра без объяснения («swap 80%») в час
ночи не помогает решить, вставать или спать дальше. Разметка — HTML: MarkdownV2
требует экранировать полтора десятка символов, и один пропущенный роняет всё
сообщение целиком.
"""

import logging
import json
import os
import sys
import uuid
import requests
import re
import asyncio
import html
import ipaddress
import tempfile
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import agent_auth
import infra  # чтение снимка сторожа: состояние машины и инфраструктуры

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchdog"))
import incidents  # noqa: E402  — мост между кнопками сторожа и root-исполнителем
import converse   # noqa: E402  — вопросы аналитику по инциденту
from keyboards import (
    get_main_menu_keyboard, get_server_list_keyboard,
    get_server_management_keyboard, get_delete_confirm_keyboard,
    get_monitoring_keyboard, get_threshold_keyboard,
    get_host_keyboard, get_infra_keyboard, get_infra_section_keyboard,
    get_back_keyboard,
)

# --- Настройки ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Изменяемые данные живут вне репозитория. Причина не в аккуратности: боту нужно
# право записи в каталог с users.json (атомарная запись создаёт временный файл
# рядом), а дай мы это право на каталог репозитория — скомпрометированный бот
# смог бы переписать собственный код. По умолчанию остаётся каталог проекта,
# чтобы запуск из исходников работал без настройки.
DATA_DIR = os.environ.get("BOT_DATA_DIR", BASE_DIR)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MONITOR_FILE = os.path.join(DATA_DIR, "monitor_subscribers.json")
ASK_SERVER_NAME, ASK_IP, CONFIRM_DELETE = range(3)

# Ограничения на имя сервера. Имя попадает в callback_data (лимит Telegram 64 байта),
# поэтому длину ограничиваем с запасом на самый длинный префикс (show_instructions_).
MAX_SERVER_NAME_LEN = 24
SERVER_NAME_RE = re.compile(r'^[\w .\-]+$', re.UNICODE)
LONGEST_CB_PREFIX = "show_instructions_"

# Публичный адрес этого сервера. Подставляется в команду установки агента, чтобы
# порт 5000 на наблюдаемой машине сразу открывался только нам.
#
# Задаётся руками, а не определяется автоматически: наружу машина ходит через
# мост, и любой сервис вида ifconfig.me вернёт адрес выходного узла, а не наш.
# Проверено — возвращал именно чужой.
CENTRAL_IP = os.environ.get("CENTRAL_IP", "").strip() or "<IP этого сервера>"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
# httpx на INFO печатает полный URL запроса, а токен бота — часть этого URL.
# С уровнем WARNING в журнал попадают только сбои, без секрета в тексте лога.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def esc(value) -> str:
    """Экранирование для HTML-сообщений.

    Бот перешёл с MarkdownV2 на HTML целиком. Причина практическая: MarkdownV2
    требует экранировать полтора десятка символов, включая точку и дефис, и один
    пропущенный символ роняет ВСЁ сообщение (Telegram отвечает 400, владелец не
    видит ничего). Отсюда и брались строки вида `r"...\\."` в каждом тексте.
    В HTML экранировать нужно три символа, и сторож (watchdog/notify.py) уже
    пишет на нём — теперь обе половины бота говорят одинаково.
    """
    # quote=False намеренно: кавычка внутри текста ничего не ломает, а
    # html.escape по умолчанию превращает её в &quot; — и владелец видит
    # мнемонику вместо кавычки в цитате из лога.
    return html.escape(str(value), quote=False)


async def show(update: Update, text: str, reply_markup=None):
    """Показывает экран: правит текущее сообщение или шлёт новое.

    Кнопка «Обновить» часто приносит ровно тот же текст — Telegram считает это
    ошибкой (`Message is not modified`). Для владельца это не ошибка, а «всё
    по-прежнему», поэтому гасим её и просто подтверждаем нажатие.
    """
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup,
                                          parse_mode='HTML',
                                          disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return
    await update.effective_message.reply_text(text, reply_markup=reply_markup,
                                              parse_mode='HTML',
                                              disable_web_page_preview=True)

def _atomic_write_json(path: str, data) -> None:
    """Атомарная запись JSON: во временный файл + fsync + rename.

    Гарантирует, что читатель (в т.ч. cron monitor_remote.py) никогда не увидит
    частично записанный/битый файл, а сбой в момент записи не приведёт к потере данных.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def load_users():
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_users(users_data):
    _atomic_write_json(USERS_FILE, users_data)

def load_monitor_subs():
    try:
        with open(MONITOR_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_monitor_subs(data):
    _atomic_write_json(MONITOR_FILE, data)

DEFAULT_MONITOR_SETTINGS = {
    "enabled": True,
    "disk_warn": 80,
    "ram_warn": 90,
    "cpu_warn": 90,   # проценты (0..100), как отдаёт агент psutil.cpu_percent
    "gpu_temp_warn": 80,
}

def is_valid_ip(ip: str) -> bool:
    """Проверяет, что это публичный IPv4-адрес.

    Отклоняем частные/loopback/link-local/multicast/зарезервированные диапазоны,
    чтобы бот и удалённый монитор нельзя было заставить обращаться во внутреннюю
    сеть хоста (SSRF), например к 127.0.0.1 или 169.254.169.254 (метаданные облака).
    """
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if addr.version != 4:
        return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        return False
    return True

def validate_server_name(name: str, existing: dict) -> str | None:
    """Возвращает текст ошибки или None, если имя корректно.

    existing — словарь уже добавленных серверов пользователя (для проверки уникальности).
    """
    if not name:
        return "Имя не может быть пустым."
    if len(name) > MAX_SERVER_NAME_LEN:
        return f"Слишком длинное имя (макс. {MAX_SERVER_NAME_LEN} символов)."
    if not SERVER_NAME_RE.match(name):
        return "Допустимы буквы, цифры, пробел, точка, дефис и подчёркивание."
    # Гарантируем, что имя влезет в callback_data Telegram (лимит 64 байта)
    if len((LONGEST_CB_PREFIX + name).encode('utf-8')) > 64:
        return "Имя слишком длинное для кнопок Telegram, сократите его."
    if name in existing:
        return "Сервер с таким именем уже есть — выберите другое имя."
    return None

def server_registered(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id)
        if user_id not in load_users():
            # Работает и для callback (update.message is None), и для обычных сообщений
            if update.callback_query:
                await update.callback_query.answer(
                    "Сначала добавьте удалённый сервер", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "❗️ Удалённых серверов пока нет.\n\n"
                    "Добавьте первый: «Удалённые серверы» → «Добавить сервер».",
                    reply_markup=get_back_keyboard("menu_myservers", "🗂 Удалённые серверы"),
                    parse_mode='HTML')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# Пороги для удалённых серверов — те же, по которым сторож решает, что это
# событие (watchdog/delta.py, REMOTE_RISING). Держим согласованными, иначе бот
# скажет «нормально» там, где сторож уже прислал алерт.
REMOTE_BANDS = {"disk": (80, 90), "memory": (90, 95), "cpu": (85, 95)}


def _verdict(value, warn, crit, wording):
    """Значок и человеческая приписка к цифре."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return infra.MARK["unknown"], ""
    band = "crit" if value >= crit else "warn" if value >= warn else "ok"
    return infra.MARK[band], wording[band]


def get_status_text(data: dict, server_name: str) -> str:
    """Состояние удалённого сервера словами, а не только цифрами."""
    lines = [f"📊 <b>Сервер «{esc(server_name)}»</b>", ""]

    disk = data.get('disk', {})
    mark, note = _verdict(disk.get('percent'), *REMOTE_BANDS["disk"], {
        "ok": "запас есть", "warn": "стоит посмотреть, чем занято",
        "crit": "место кончается"})
    lines.append(f"{mark} <b>Диск</b> — занято {esc(disk.get('percent', '?'))}% "
                 f"({esc(disk.get('used', '?'))} из {esc(disk.get('total', '?'))} ГБ)"
                 + (f". {note.capitalize()}." if note else ""))

    mem = data.get('memory', {})
    mark, note = _verdict(mem.get('percent'), *REMOTE_BANDS["memory"], {
        "ok": "хватает", "warn": "в обрез",
        "crit": "на исходе, процессы могут падать"})
    lines.append(f"{mark} <b>Память</b> — занято {esc(mem.get('percent', '?'))}% "
                 f"({esc(mem.get('used', '?'))} из {esc(mem.get('total', '?'))} ГБ)"
                 + (f". {note.capitalize()}." if note else ""))

    mark, note = _verdict(data.get('cpu'), *REMOTE_BANDS["cpu"], {
        "ok": "не загружен", "warn": "нагрузка выше обычной",
        "crit": "процессор не справляется"})
    cpu_temp = data.get('cpu_temp')
    temp_text = f", {esc(cpu_temp)}°C" if cpu_temp is not None else ""
    lines.append(f"{mark} <b>Процессор</b> — {esc(data.get('cpu', '?'))}%{temp_text}"
                 + (f". {note.capitalize()}." if note else ""))

    gpu = data.get('gpu')
    if gpu:
        mark, note = _verdict(gpu.get('temp'), 80, 90, {
            "ok": "температура в норме", "warn": "горячевато",
            "crit": "перегрев, проверьте охлаждение"})
        lines.append(f"{mark} <b>Видеокарта</b> — {esc(gpu.get('name', '?'))}, "
                     f"нагрузка {esc(gpu.get('load', '?'))}%, "
                     f"{esc(gpu.get('temp', '?'))}°C"
                     + (f". {note.capitalize()}." if note else ""))

    return "\n".join(lines)

# --- Обработчики ---

WELCOME = (
    "🏠 <b>PLGames Admin</b>\n\n"
    "Пульт администратора. Отсюда видно состояние центрального сервера, "
    "всей инфраструктуры на нём и удалённых серверов.\n\n"
    "Выберите раздел:"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    await show(update, WELCOME, get_main_menu_keyboard())


# --- Центральный сервер и инфраструктура ------------------------------------
#
# Данные берутся из снимка сторожа, а не собираются заново: у бота нет ни
# доступа к docker, ни sudo, и выдавать их процессу, смотрящему в интернет,
# ради одной кнопки не стоит. Подробности — в infra.py.

async def host_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Состояние самой машины, на которой всё живёт."""
    query = update.callback_query
    await query.answer()
    try:
        snapshot = infra.load_snapshot()
    except infra.SnapshotUnavailable as exc:
        await show(update, f"⚠️ <b>Данных пока нет</b>\n\n{exc}", get_host_keyboard())
        return

    text = (f"🖥 <b>Центральный сервер</b>\n"
            f"<i>данные {infra.age_text(snapshot)}</i>\n\n"
            f"{infra.render_host(snapshot)}")

    remote = infra.render_remote(snapshot)
    if remote:
        text += ("\n\n🗂 <b>Удалённые серверы</b>\n" + remote)

    await show(update, text, get_host_keyboard())


async def infra_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сводка по инфраструктуре: что вообще крутится и что из этого сломано."""
    query = update.callback_query
    await query.answer()
    try:
        snapshot = infra.load_snapshot()
    except infra.SnapshotUnavailable as exc:
        await show(update, f"⚠️ <b>Данных пока нет</b>\n\n{exc}", get_infra_keyboard())
        return

    text = (f"🌐 <b>Инфраструктура</b>\n"
            f"<i>данные {infra.age_text(snapshot)}</i>\n\n"
            f"{infra.render_overview(snapshot)}\n\n"
            f"Нажмите раздел, чтобы посмотреть подробности.")
    await show(update, text, get_infra_keyboard())


# Раздел → (заголовок, функция отрисовки). Список закрытый: имя раздела приходит
# из callback_data, и подставлять по нему произвольный атрибут модуля не станем.
INFRA_SECTIONS = {
    "containers": ("🐳 Контейнеры", infra.render_containers),
    "services": ("⚙️ Службы", infra.render_services),
    "sites": ("🔗 Сайты", infra.render_sites),
    "certs": ("🔒 Сертификаты", infra.render_certs),
    "security": ("🛡 Безопасность", infra.render_security),
}


async def infra_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    section = query.data.removeprefix("infra_")
    entry = INFRA_SECTIONS.get(section)
    if entry is None:
        await show(update, "⚠️ Неизвестный раздел.", get_infra_keyboard())
        return
    title, render = entry

    try:
        snapshot = infra.load_snapshot()
    except infra.SnapshotUnavailable as exc:
        await show(update, f"⚠️ <b>Данных пока нет</b>\n\n{exc}",
                   get_infra_section_keyboard(section))
        return

    text = (f"<b>{title}</b>\n<i>данные {infra.age_text(snapshot)}</i>\n\n"
            f"{render(snapshot)}")
    await show(update, text, get_infra_section_keyboard(section))


# --- Удалённые серверы ------------------------------------------------------

async def myservers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user_data = load_users().get(user_id, {"servers": {}})

    if not user_data.get("servers"):
        text = ("🗂 <b>Удалённые серверы</b>\n\n"
                "Пока ни одного. Сюда добавляются <b>другие</b> машины — на них "
                "ставится небольшой агент, и бот показывает их состояние так же, "
                "как состояние центрального сервера.\n\n"
                "Сам центральный сервер добавлять не нужно: он в разделе "
                "«🖥 Этот сервер».")
    else:
        text = ("🗂 <b>Удалённые серверы</b>\n\n"
                "Галочкой отмечен активный — именно его показывает кнопка "
                "«Показать состояние» в разделе алертов.\n\n"
                "Выберите сервер или добавьте новый:")
    await show(update, text, get_server_list_keyboard(user_data))


async def select_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    server_data = load_users().get(user_id, {}).get("servers", {}).get(server_name)

    if not server_data:
        await show(update, "❌ Такого сервера больше нет — возможно, он удалён.",
                   get_back_keyboard("menu_myservers", "🔙 К списку серверов"))
        return

    text = (
        f"⚙️ <b>Сервер «{esc(server_name)}»</b>\n\n"
        f"Адрес: <code>{esc(server_data['server_ip'])}</code>\n"
        # Ключ агента в чат не выводим: сообщение осело бы в истории Telegram
        # навсегда. Его показывает только инструкция по установке, по запросу.
        f"Ключ агента: <code>{esc(server_data['secret_key'][:4])}…</code> "
        f"<i>(скрыт — целиком он есть только в инструкции по установке)</i>"
    )
    await show(update, text, get_server_management_keyboard(server_name))


async def set_active_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)

    users = load_users()
    if user_id in users and server_name in users[user_id].get("servers", {}):
        users[user_id]['active_server'] = server_name
        save_users(users)
        await query.answer(f"Активный сервер: {server_name}")
        await show(update,
                   f"🚀 <b>«{esc(server_name)}» теперь активный</b>\n\n"
                   "Кнопка «Показать состояние» в разделе алертов будет "
                   "показывать именно его.",
                   get_back_keyboard("menu_myservers", "🔙 К списку серверов"))
    else:
        await query.answer()
        await show(update, "❌ Не получилось: такого сервера больше нет.",
                   get_back_keyboard("menu_myservers", "🔙 К списку серверов"))


async def server_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Состояние конкретного сервера по кнопке из его карточки."""
    query = update.callback_query
    await query.answer("Опрашиваю сервер…")
    server_name = query.data.removeprefix("server_status_")
    user_id = str(query.from_user.id)
    server = load_users().get(user_id, {}).get("servers", {}).get(server_name)

    if not server:
        await show(update, "❌ Такого сервера больше нет.",
                   get_back_keyboard("menu_myservers", "🔙 К списку серверов"))
        return

    text = await fetch_server_status(server_name, server, user_id)
    await show(update, text, get_server_management_keyboard(server_name))


async def fetch_server_status(server_name: str, server: dict, user_id: str) -> str:
    """Опрашивает агента и объясняет отказ, если он случился.

    Раньше на любую сетевую ошибку приходило одно «Не удалось подключиться» —
    по нему невозможно понять, агент не поставлен, порт закрыт или ключ разошёлся.
    """
    url = f"http://{server['server_ip']}:5000/status"
    # Секрет не отправляем — только подпись этого запроса (см. agent_auth).
    headers = agent_auth.build_headers(server['secret_key'], "GET", "/status")
    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        response.raise_for_status()
        return get_status_text(response.json(), server_name)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        hint = {
            403: ("Агент не принял подпись. Обычно это старая версия агента — "
                  "она ждёт ключ в заголовке. Переустановите агента: кнопка "
                  "«Как установить агента»."),
            503: ("У агента не настроен ключ: он запущен, но не видит SECRET_KEY. "
                  "Проверьте на сервере <code>/etc/bot-agent.env</code> и "
                  "<code>systemctl restart bot-agent</code>."),
        }.get(code, "Агент ответил ошибкой — смотрите его журнал на сервере.")
        logger.warning("Агент %s ответил %s", server['server_ip'], code)
        return (f"⛔️ <b>«{esc(server_name)}» ответил ошибкой {code}</b>\n\n{hint}")
    except requests.exceptions.RequestException as exc:
        logger.error("Нет связи с агентом %s для %s: %s",
                     server['server_ip'], user_id, exc)
        return (f"⛔️ <b>«{esc(server_name)}» не отвечает</b>\n\n"
                f"Адрес: <code>{esc(server['server_ip'])}</code>, порт 5000.\n\n"
                "Что обычно означает:\n"
                "• агент не установлен или не запущен;\n"
                "• порт 5000 закрыт фаерволом для этого сервера;\n"
                "• сервер недоступен по сети.")

# --- ВОССТАНОВЛЕННАЯ ФУНКЦИЯ ---
@server_registered
async def show_instructions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает инструкцию по установке агента по кнопке."""
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    server = load_users().get(user_id, {}).get("servers", {}).get(server_name)
    if not server:
        await show(update, "❌ Такого сервера больше нет.",
                   get_back_keyboard("menu_myservers", "🔙 К списку серверов"))
        return
    secret_key = server['secret_key']
    
    AGENT_URL = f"https://raw.githubusercontent.com/{context.bot_data.get('repo_owner', 'Leonid1095')}/{context.bot_data.get('repo_name', 'SYSadmins-BOT')}/main/install.sh"
    
    # Ключ уходит переменной окружения, а не аргументом. Аргументы процесса
    # лежат в /proc/<pid>/cmdline и читаются любым пользователем той машины:
    # пока идёт установка, ключ агента видно в обычном `ps`. Окружение процесса
    # (/proc/<pid>/environ) доступно только владельцу и root.
    text = (
        f"📋 <b>Установка агента для «{esc(server_name)}»</b>\n\n"
        f"Агент — маленькая программа, которая отдаёт боту загрузку процессора, "
        f"памяти и диска. Ставится одной командой.\n\n"
        f"<b>1.</b> Зайдите на сервер <code>{esc(server['server_ip'])}</code> "
        f"под <code>root</code>.\n"
        f"<b>2.</b> Выполните:\n\n"
        f"<pre>SECRET_KEY={esc(secret_key)} \\\n"
        f"  bash &lt;(wget -qO- {esc(AGENT_URL)}) \\\n"
        f"  --allow-from {esc(CENTRAL_IP)}</pre>\n"
        f"<b>3.</b> Вернитесь сюда и нажмите «Показать состояние».\n\n"
        f"🔒 <code>--allow-from</code> сразу открывает порт 5000 только этому "
        f"серверу и закрывает всем остальным. Раньше установщик лишь советовал "
        f"это сделать — и совет не выполнялся: сервер оказывался за фаерволом, "
        f"который не пускал никого, и не наблюдался месяцами.\n"
        f"🔑 Ключ выше — пароль от метрик этого сервера. Сообщение с ним остаётся "
        f"в истории чата: удалите его, когда агент заработает."
    )
    # Отправляем новым сообщением, чтобы не затирать карточку сервера
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML',
                                   disable_web_page_preview=True)


# Опрос агента жил в трёх почти одинаковых копиях: status_command,
# monitor_status_now и карточка сервера. Именно поэтому одна из них незаметно
# осталась на старой схеме авторизации — слала сам секрет заголовком по
# открытому HTTP и получала в ответ 403. Теперь копия одна:
# fetch_server_status() выше, и её зовут все кнопки.

# --- Мониторинг ---

async def monitoring_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user_sub = load_monitor_subs().get(user_id)
    is_subscribed = user_sub is not None and user_sub.get("enabled", False)
    settings = user_sub if user_sub else DEFAULT_MONITOR_SETTINGS.copy()

    if is_subscribed:
        text = (
            "🔔 <b>Алерты включены</b>\n\n"
            "Сообщение придёт, когда что-то <b>изменится</b> к худшему: кончается "
            "место, падает служба или контейнер, сервер перестал отвечать, "
            "истекает сертификат.\n\n"
            "Пока всё спокойно — бот молчит. Это намеренно: уведомление про то, "
            "что диск третьи сутки занят на 81%, быстро научило бы вас их "
            "игнорировать.\n\n"
            "Кнопками ниже настройте, при каких значениях предупреждать:"
        )
    else:
        text = (
            "🔕 <b>Алерты выключены</b>\n\n"
            "Сейчас бот ничего не присылает — состояние можно посмотреть только "
            "вручную.\n\n"
            "Если включить, будут приходить сообщения о том, что:\n"
            "• кончается место на диске или память;\n"
            "• упала служба или контейнер;\n"
            "• сайт перестал отвечать;\n"
            "• истекает сертификат;\n"
            "• диск не проходит самопроверку;\n"
            "• выросло число заблокированных адресов.\n\n"
            "К серьёзным сообщениям бот приложит кнопки с вариантами починки."
        )

    await show(update, text, get_monitoring_keyboard(is_subscribed, settings))


async def monitor_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)

    await query.answer("Алерты включены")
    subs = load_monitor_subs()
    if user_id not in subs:
        subs[user_id] = DEFAULT_MONITOR_SETTINGS.copy()
    subs[user_id]["enabled"] = True
    subs[user_id]["username"] = query.from_user.username or query.from_user.first_name
    save_monitor_subs(subs)

    # Требование «сначала добавьте сервер» убрано: центральный сервер наблюдается
    # всегда, добавлять его никуда не нужно, и отказ подписаться на алерты о нём
    # из-за отсутствия удалённых серверов был лишним препятствием.
    await show(update,
               "✅ <b>Алерты включены</b>\n\n"
               "Проверка идёт каждые 5 минут. Придёт сообщение — значит "
               "действительно что-то изменилось.\n\n"
               "Ниже можно настроить, при каких значениях предупреждать. "
               "Значения по умолчанию подобраны так, чтобы предупреждение "
               "приходило заранее, а не когда уже поздно.",
               get_monitoring_keyboard(True, subs[user_id]))


async def monitor_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Алерты выключены")
    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    if user_id in subs:
        subs[user_id]["enabled"] = False
        save_monitor_subs(subs)

    await show(update,
               "🔕 <b>Алерты выключены</b>\n\n"
               "Бот больше не будет присылать уведомления о поломках. "
               "Состояние по-прежнему можно смотреть вручную в разделах "
               "«Этот сервер» и «Инфраструктура».\n\n"
               "Включить обратно можно в любой момент.",
               get_monitoring_keyboard(False, DEFAULT_MONITOR_SETTINGS))


# Что означает каждый порог — своими словами. Без этого «Порог CPU» ничего не
# объясняет: непонятно ни что мерят, ни что будет при превышении.
THRESHOLD_HELP = {
    "disk_warn": ("💾 Место на диске",
                  "Предупредить, когда диск заполнится до этого значения. "
                  "Ниже 80% ставить не стоит — начнёте получать сообщения "
                  "о нормальном состоянии."),
    "ram_warn": ("🧠 Оперативная память",
                 "Предупредить, когда занято столько памяти. При нехватке "
                 "система начинает выгружать процессы на диск, и всё "
                 "заметно замедляется."),
    "cpu_warn": ("🔥 Загрузка процессора",
                 "Предупредить, когда процессор занят настолько. Короткие "
                 "всплески — норма; сообщение придёт, если нагрузка держится."),
    "gpu_temp_warn": ("🎮 Температура видеокарты",
                      "Предупредить при таком нагреве. Выше 85°C карта "
                      "начинает сбрасывать частоты, чтобы не сгореть."),
}


async def monitor_set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    param = query.data.replace("monitor_set_", "") + "_warn"
    user_id = str(query.from_user.id)
    current = load_monitor_subs().get(user_id, DEFAULT_MONITOR_SETTINGS).get(param, 80)

    title, explain = THRESHOLD_HELP.get(param, (param, ""))
    unit = "°C" if param == "gpu_temp_warn" else "%"
    text = (f"<b>{title}</b>\n\n{explain}\n\n"
            f"Сейчас: <b>{current}{unit}</b>. Выберите новое значение:")

    await show(update, text, get_threshold_keyboard(param, current))

# Пороги, которые вообще можно менять кнопкой, и допустимый диапазон значения.
# Раньше имя параметра и число брались из нажатия как есть: `monitor_val_x_5`
# завёл бы в файл подписок посторонний ключ, а `monitor_val_x_abc` ронял
# обработчик на int(). Кнопки рисуем мы, но проверять надо то, что пришло.
ADJUSTABLE_THRESHOLDS = {
    "disk_warn": (50, 99),
    "ram_warn": (50, 99),
    "cpu_warn": (50, 100),
    "gpu_temp_warn": (50, 100),
}


async def monitor_set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # monitor_val_disk_warn_80 или monitor_val_gpu_temp_warn_80
    data = query.data  # "monitor_val_<param>_<value>"
    param, _, raw_value = data.removeprefix("monitor_val_").rpartition("_")

    bounds = ADJUSTABLE_THRESHOLDS.get(param)
    if bounds is None or not raw_value.isdigit():
        await query.answer("Не понял, какой порог менять.", show_alert=True)
        return
    value = int(raw_value)
    if not bounds[0] <= value <= bounds[1]:
        await query.answer(f"Допустимо {bounds[0]}–{bounds[1]}.", show_alert=True)
        return

    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    if user_id not in subs:
        subs[user_id] = DEFAULT_MONITOR_SETTINGS.copy()
    subs[user_id][param] = value
    save_monitor_subs(subs)

    unit = "°C" if param == "gpu_temp_warn" else "%"
    await query.answer(f"Теперь предупрежу на {value}{unit}")

    title = THRESHOLD_HELP.get(param, (param, ""))[0]
    await show(update,
               f"✅ <b>Порог изменён</b>\n\n"
               f"{title} — предупрежу при <b>{value}{unit}</b>.\n\n"
               "Можно настроить остальные пороги или вернуться в главное меню.",
               get_monitoring_keyboard(True, subs[user_id]))

# --- Диалоги ---

async def addserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await show(update,
               "➕ <b>Новый сервер — шаг 1 из 2</b>\n\n"
               "Придумайте название, по которому вы его узнаете. Например: "
               "<code>DE сервер</code> или <code>прокси-NL</code>.\n\n"
               "Можно буквы, цифры, пробел, точку, дефис и подчёркивание, "
               "до 24 символов.\n\n"
               "Отменить — команда /cancel")
    return ASK_SERVER_NAME


async def ask_server_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    user_id = str(update.effective_user.id)
    existing = load_users().get(user_id, {}).get("servers", {})
    error = validate_server_name(name, existing)
    if error:
        await update.message.reply_text(
            f"❌ {esc(error)}\n\nВведите другое название или /cancel",
            parse_mode='HTML')
        return ASK_SERVER_NAME
    context.user_data['server_name'] = name
    await update.message.reply_text(
        f"➕ <b>Новый сервер «{esc(name)}» — шаг 2 из 2</b>\n\n"
        "Теперь пришлите его IP-адрес — тот, по которому сервер доступен "
        "из интернета. Например: <code>203.0.113.10</code>\n\n"
        "Отменить — /cancel",
        parse_mode='HTML')
    return ASK_IP


async def ask_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    server_ip = update.message.text.strip()
    server_name = context.user_data.get('server_name')

    if not is_valid_ip(server_ip):
        # Причину называем: «некорректный адрес» не отличает опечатку от
        # намеренно запрещённого диапазона, и владелец сидит гадает.
        await update.message.reply_text(
            "❌ Это не подходит как адрес удалённого сервера.\n\n"
            "Нужен публичный IPv4 — вида <code>203.0.113.10</code>.\n"
            "Внутренние адреса (127.0.0.1, 192.168.*, 10.*) сюда не годятся: "
            "этот раздел про <b>другие</b> машины, а сам центральный сервер "
            "уже наблюдается и живёт в разделе «🖥 Этот сервер».\n\n"
            "Попробуйте снова или /cancel",
            parse_mode='HTML')
        return ASK_IP

    user_id = str(update.effective_user.id)
    users = load_users()
    user_servers = users.setdefault(user_id, {"servers": {}})["servers"]
    
    user_servers[server_name] = {"server_ip": server_ip, "secret_key": str(uuid.uuid4())}
    users[user_id]['active_server'] = server_name
    save_users(users)
    
    await update.message.reply_text(
        f"✅ <b>Сервер «{esc(server_name)}» добавлен</b>\n\n"
        f"Адрес: <code>{esc(server_ip)}</code>. Он назначен активным.\n\n"
        "<b>Осталось поставить на него агента</b> — без этого бот не увидит "
        "его состояние. Откройте «Удалённые серверы» → выберите этот сервер → "
        "«Как установить агента»: там готовая команда.",
        reply_markup=get_back_keyboard("menu_myservers", "🗂 К списку серверов"),
        parse_mode='HTML')
    return ConversationHandler.END

async def deleteserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    await show(update,
               f"🗑 <b>Удалить «{esc(server_name)}»?</b>\n\n"
               "Из бота пропадёт запись о сервере и его ключ. Сам сервер и "
               "установленный на нём агент останутся работать — если соберётесь "
               "добавить его обратно, агента придётся переустановить с новым "
               "ключом.",
               get_delete_confirm_keyboard(server_name))


async def confirm_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    users = load_users()

    if user_id in users and server_name in users[user_id].get("servers", {}):
        del users[user_id]["servers"][server_name]
        if users[user_id].get("active_server") == server_name:
            remaining = list(users[user_id]["servers"].keys())
            users[user_id]["active_server"] = remaining[0] if remaining else ""
        if not users[user_id]["servers"]:
            del users[user_id]
        save_users(users)
        # Одним сообщением вместо двух: раньше следом прилетало отдельное
        # «Главное меню», и подтверждение уезжало вверх.
        await show(update,
                   f"✅ <b>«{esc(server_name)}» удалён</b>\n\n"
                   "Запись и ключ убраны из бота.",
                   get_back_keyboard("menu_myservers", "🗂 К списку серверов"))
    else:
        await show(update, "❌ Такого сервера уже нет — возможно, он удалён раньше.",
                   get_back_keyboard("menu_myservers", "🗂 К списку серверов"))

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('server_name', None)
    await show(update, "↩️ Добавление отменено — ничего не сохранено.",
               get_main_menu_keyboard())
    return ConversationHandler.END

# --- Сторож: доступ и кнопки ------------------------------------------------

async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропускает дальше только владельца.

    Стоит в группе -1, то есть срабатывает раньше всех остальных обработчиков.
    Посторонним не отвечаем вовсе: любой ответ подтвердил бы, что бот живой и
    чем-то управляет. Раньше бот пускал кого угодно регистрировать свои серверы —
    для админского бота с кнопками действий это недопустимо.
    """
    user = update.effective_user
    if user is not None and str(user.id) == str(config.OWNER_ID):
        return
    logger.warning("Отброшен update от постороннего: id=%s",
                   user.id if user else "неизвестен")
    raise ApplicationHandlerStop


def _remedy_keyboard(incident_id: str, index: int) -> InlineKeyboardMarkup:
    """Подтверждение перед реальным действием: кнопку легко задеть случайно."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, выполнить", callback_data=f"wdok:{incident_id}:{index}"),
        InlineKeyboardButton("↩️ Отмена", callback_data=f"wdno:{incident_id}:{index}"),
    ]])


def _parse_callback(data: str):
    parts = data.split(":", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        raise incidents.IncidentError("Не удалось разобрать нажатие.")
    return parts[1], int(parts[2])


async def watchdog_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Владелец выбрал вариант. Действие берём из вердикта, а не из нажатия."""
    query = update.callback_query
    await query.answer()
    try:
        incident_id, index = _parse_callback(query.data)
        result, chosen = incidents.option(incident_id, index)
    except incidents.IncidentError as exc:
        await query.edit_message_text(f"⚠️ {esc(exc)}", parse_mode='HTML')
        return

    if chosen["action"] == "nothing":
        incidents.record_choice(incident_id, {"choice": "nothing", "by": query.from_user.id})
        await query.edit_message_text(
            f"{query.message.text_html}\n\n☑️ <i>Принято к сведению, действий не предпринято.</i>",
            parse_mode='HTML')
        return

    # Экранируем всё, что пришло из вердикта: его писала модель, а она читала
    # логи, в которые пишет посторонний. Неэкранированный текст здесь либо ломал
    # разметку (Telegram отвечает 400, кнопка «молчит»), либо позволял вложить
    # в админский алерт чужую ссылку. В watchdog_execute это уже делалось.
    target = (f" → <code>{esc(chosen['target'])}</code>"
              if chosen.get("target") else "")
    await query.edit_message_text(
        f"{query.message.text_html}\n\n❓ <b>{esc(chosen['label'])}</b>{target}\n"
        f"<i>{esc(chosen['why'])}</i>\n\nВыполнить?",
        parse_mode='HTML', reply_markup=_remedy_keyboard(incident_id, index))


async def watchdog_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждено. Зовём исполнителя — он проверит заявку заново."""
    query = update.callback_query
    await query.answer("Выполняю…")
    try:
        incident_id, index = _parse_callback(query.data)
        result, chosen = incidents.option(incident_id, index)
    except incidents.IncidentError as exc:
        await query.edit_message_text(f"⚠️ {exc}", parse_mode=None)
        return

    outcome = await asyncio.to_thread(
        incidents.run_remedy, chosen["action"], chosen.get("target"))
    incidents.record_choice(incident_id, {
        "choice": chosen["action"], "target": chosen.get("target"),
        "ok": outcome.get("ok"), "by": query.from_user.id,
    })

    mark = "✅" if outcome.get("ok") else "⛔️"
    body = esc(str(outcome.get("output", ""))[:900])
    await query.edit_message_text(
        f"{query.message.text_html}\n\n{mark} <b>{esc(chosen['label'])}</b>\n"
        f"<pre>{body}</pre>", parse_mode='HTML')


async def watchdog_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отменено")
    try:
        incident_id, _ = _parse_callback(query.data)
        incidents.record_choice(incident_id, {"choice": "cancelled", "by": query.from_user.id})
    except incidents.IncidentError:
        pass
    await query.edit_message_text(
        f"{query.message.text_html}\n\n↩️ <i>Отменено, ничего не выполнено.</i>",
        parse_mode='HTML')


# --- Разговор со сторожем ---------------------------------------------------
#
# Раньше на сообщение об инциденте нельзя было ответить: бот обрабатывал только
# нажатия и текст внутри диалога добавления сервера, поэтому любой вопрос
# проваливался в пустоту. Теперь ответ на сообщение — это вопрос аналитику.
#
# Новых полномочий у модели при этом ноль: тот же каталог инцидента, те же
# Read/Grep/Glob (watchdog/sandbox.py). Разговор не пульт — изменить что-либо
# по-прежнему можно только кнопкой, через каталог и root-хелпера.

# Идентификатор инцидента бот берёт из текста сообщения, на которое ответили:
# notify.py печатает его последней строкой, и он переживает правки сообщения
# кнопками. Отдельного хранилища для этого не нужно.
INCIDENT_ID_RE = re.compile(r"\b\d{8}T\d{6}Z(?:-\d{1,3})?\b")

# Сколько ждём ответа службы. Модель думает 30-60 секунд; запас нужен, но и
# бесконечно держать владельца в неизвестности нельзя — по истечении честно
# говорим, что ответа нет, и куда смотреть.
ASK_TIMEOUT = int(os.environ.get("BOT_ASK_TIMEOUT", "150"))
ASK_POLL_SECONDS = 1.5


class IncidentReplyFilter(filters.MessageFilter):
    """Ответ именно на сообщение об инциденте.

    Фильтр намеренно узкий: пока владелец добавляет сервер, он тоже отвечает на
    сообщения бота, и перехватывать их здесь нельзя — шаги диалога сломались бы.
    """

    def filter(self, message) -> bool:
        replied = getattr(message, "reply_to_message", None)
        if replied is None:
            return False
        return bool(INCIDENT_ID_RE.search(replied.text or ""))


async def ask_watchdog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Владелец спросил про инцидент — отвечает аналитик."""
    message = update.effective_message
    incident_id = INCIDENT_ID_RE.search(message.reply_to_message.text or "").group(0)

    # Ответ модели идёт десятки секунд. Индикатор «печатает» живёт пять, поэтому
    # ставим видимую заглушку и правим её ответом: владелец сразу видит, что
    # вопрос принят, а не гадает, дошёл ли он.
    thinking = await message.reply_text("🤔 Смотрю материалы происшествия…")

    try:
        incident_dir = incidents.directory(incident_id)
        # Модель зовёт служба под plg — у бота нет credentials подписки, и
        # выдавать их ему нельзя (см. converse.py). Мы кладём вопрос и ждём.
        token = await asyncio.to_thread(converse.submit, incident_dir, message.text)
    except (incidents.IncidentError, converse.AskError) as exc:
        await thinking.edit_text(f"⚠️ {esc(exc)}", parse_mode='HTML')
        return

    answer = None
    deadline = asyncio.get_running_loop().time() + ASK_TIMEOUT
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(ASK_POLL_SECONDS)
            answer = await asyncio.to_thread(converse.collect, token)
            if answer is not None:
                break
    except converse.AskError as exc:
        await thinking.edit_text(f"⚠️ {esc(exc)}", parse_mode='HTML')
        return

    if answer is None:
        logger.warning("Ответ по инциденту %s не пришёл за %s c", incident_id, ASK_TIMEOUT)
        await thinking.edit_text(
            "⏳ Аналитик не ответил за отведённое время.\n\n"
            "Обычно это значит, что служба ответов не запущена. Проверьте:\n"
            "<code>systemctl status watchdog-ask.path</code>",
            parse_mode='HTML')
        return

    await thinking.edit_text(
        f"{esc(answer)}\n\n<i>Можно спросить ещё — ответьте на это сообщение.</i>",
        parse_mode='HTML', disable_web_page_preview=True)


async def unhandled_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст, который бот не ждал.

    Раньше такие сообщения молча исчезали, и со стороны это выглядело как
    «боту нельзя написать». Молчание в админском инструменте — худший ответ:
    непонятно, дошло ли сообщение и жив ли бот вообще.
    """
    await update.effective_message.reply_text(
        "Я понимаю два вида сообщений:\n\n"
        "• <b>вопрос про происшествие</b> — ответьте на сообщение о нём, "
        "и я разберу подробнее;\n"
        "• <b>кнопки</b> — всё остальное делается ими.\n\n"
        "Свободные команды я не выполняю: это админский бот, "
        "и такой возможности в нём нет намеренно.",
        reply_markup=get_main_menu_keyboard(), parse_mode='HTML')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка при обработке update", exc_info=context.error)

async def post_init(application: Application):
    application.bot_data['repo_owner'] = 'Leonid1095'
    application.bot_data['repo_name'] = 'SYSadmins-BOT'
    logger.info("Данные о репозитории загружены.")

def main():
    if not config.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не установлен в config.py")
        return
    
    if not config.OWNER_ID:
        logger.error("OWNER_ID не задан — бот не знает, кого пускать, и не стартует")
        return

    application = Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_error_handler(error_handler)

    # Группа -1: отсечка посторонних до всех остальных обработчиков.
    application.add_handler(TypeHandler(Update, owner_only), group=-1)
    
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(addserver_start, pattern='^add_server_start$')],
        states={
            ASK_SERVER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_server_name_handler)],
            ASK_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ip_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(start_command, pattern='^menu_back$'))

    # Центральный сервер и его инфраструктура — из снимка сторожа.
    application.add_handler(CallbackQueryHandler(host_menu, pattern='^menu_host$'))
    application.add_handler(CallbackQueryHandler(infra_menu, pattern='^menu_infra$'))
    application.add_handler(CallbackQueryHandler(
        infra_section, pattern=r'^infra_(containers|services|sites|certs|security)$'))

    # Алерты и пороги.
    application.add_handler(CallbackQueryHandler(monitoring_menu, pattern='^menu_monitoring$'))
    application.add_handler(CallbackQueryHandler(monitor_subscribe, pattern='^monitor_sub$'))
    application.add_handler(CallbackQueryHandler(monitor_unsubscribe, pattern='^monitor_unsub$'))
    application.add_handler(CallbackQueryHandler(monitor_set_threshold, pattern=r'^monitor_set_(disk|ram|cpu|gpu_temp)$'))
    application.add_handler(CallbackQueryHandler(monitor_set_value, pattern=r'^monitor_val_'))

    # Удалённые серверы.
    application.add_handler(CallbackQueryHandler(myservers_menu, pattern='^menu_myservers$'))
    application.add_handler(CallbackQueryHandler(select_server_callback, pattern=r'^select_server_'))
    application.add_handler(CallbackQueryHandler(server_status_callback, pattern=r'^server_status_'))
    application.add_handler(CallbackQueryHandler(set_active_server_callback, pattern=r'^set_active_'))
    application.add_handler(CallbackQueryHandler(show_instructions_callback, pattern=r'^show_instructions_'))
    application.add_handler(CallbackQueryHandler(deleteserver_start, pattern=r'^delete_server_'))
    application.add_handler(CallbackQueryHandler(confirm_delete_callback, pattern=r'^confirm_delete_'))

    # Сторож: выбор варианта, подтверждение, отмена.
    application.add_handler(CallbackQueryHandler(watchdog_option, pattern=r'^wd:'))
    application.add_handler(CallbackQueryHandler(watchdog_execute, pattern=r'^wdok:'))
    application.add_handler(CallbackQueryHandler(watchdog_cancel, pattern=r'^wdno:'))

    # Вопрос по инциденту — ответом на сообщение о нём. Стоит ДО диалога
    # добавления сервера: фильтр узкий (нужен идентификатор инцидента в тексте,
    # на который отвечают), поэтому шаги диалога он не перехватывает.
    application.add_handler(MessageHandler(
        IncidentReplyFilter() & filters.TEXT & ~filters.COMMAND, ask_watchdog))

    application.add_handler(add_conv)

    # Последним: всё, что не подошло никуда выше. Молча терять сообщения
    # владельца админский бот не должен.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           unhandled_text))

    logger.info("PLGames Admin: бот запущен, владелец %s", config.OWNER_ID)
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
