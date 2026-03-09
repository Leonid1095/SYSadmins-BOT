# bot.py (Версия 8.4: Восстановлен show_instructions_callback)

import logging
import json
import uuid
import requests
import re
import asyncio
from functools import wraps
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import config
from keyboards import (
    get_main_menu_keyboard, get_server_list_keyboard,
    get_server_management_keyboard, get_delete_confirm_keyboard,
    get_monitoring_keyboard, get_threshold_keyboard,
)

# --- Настройки ---
USERS_FILE = "users.json"
MONITOR_FILE = "/home/plg/telegram-server-bot/monitor_subscribers.json"
ASK_SERVER_NAME, ASK_IP, CONFIRM_DELETE = range(3)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def load_users():
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_users(users_data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f: json.dump(users_data, f, indent=4, ensure_ascii=False)

def load_monitor_subs():
    try:
        with open(MONITOR_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_monitor_subs(data):
    with open(MONITOR_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

DEFAULT_MONITOR_SETTINGS = {
    "enabled": True,
    "disk_warn": 80,
    "ram_warn": 90,
    "cpu_warn": 200,
}

def is_valid_ip(ip: str) -> bool:
    parts = ip.split('.')
    if len(parts) != 4: return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError: return False

def server_registered(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id)
        if user_id not in load_users():
            await update.message.reply_text(r"❗️ У вас нет зарегистрированных серверов\. Используйте /start, чтобы добавить\.", parse_mode='MarkdownV2')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def get_status_text(data: dict, server_name: str) -> str:
    cpu = escape_markdown(data.get('cpu', 'N/A'))
    mem = data.get('memory', {})
    disk = data.get('disk', {})
    mem_text = f"Использовано {escape_markdown(mem.get('used', 'N/A'))} / {escape_markdown(mem.get('total', 'N/A'))} ГБ \\({escape_markdown(mem.get('percent', 'N/A'))}%\\)"
    disk_text = f"Использовано {escape_markdown(disk.get('used', 'N/A'))} / {escape_markdown(disk.get('total', 'N/A'))} ГБ \\({escape_markdown(disk.get('percent', 'N/A'))}%\\)"
    return (
        f"*📊 Статус сервера «{escape_markdown(server_name)}»*\n\n"
        f"🔥 *Процессор:* {cpu}%\n"
        f"🧠 *Память:* {mem_text}\n"
        f"💾 *Диск:* {disk_text}"
    )

async def send_or_edit(update: Update, text: str, reply_markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')

# --- Обработчики ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    if update.message:
        await update.message.reply_text(r"🏠 *Главное меню*", reply_markup=get_main_menu_keyboard(), parse_mode='MarkdownV2')
    elif query:
        await query.edit_message_text(r"🏠 *Главное меню*", reply_markup=get_main_menu_keyboard(), parse_mode='MarkdownV2')

async def myservers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    users = load_users()
    user_data = users.get(user_id, {"servers": {}})
    text = "🗂️ *Ваши серверы*\n\nВыберите сервер для управления или добавьте новый\."
    await query.edit_message_text(text, reply_markup=get_server_list_keyboard(user_data), parse_mode='MarkdownV2')

async def select_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    users = load_users()
    server_data = users.get(user_id, {}).get("servers", {}).get(server_name)

    if not server_data:
        await query.edit_message_text("❌ Ошибка: Сервер не найден\.", parse_mode='MarkdownV2')
        return

    text = (
        f"⚙️ *Управление сервером «{escape_markdown(server_name)}»*\n\n"
        f"**IP\-адрес:** `{escape_markdown(server_data['server_ip'])}`\n"
        f"**Ключ:** `{escape_markdown(server_data['secret_key'])}`"
    )
    await query.edit_message_text(text, reply_markup=get_server_management_keyboard(server_name), parse_mode='MarkdownV2')

async def set_active_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    
    users = load_users()
    if user_id in users and server_name in users[user_id].get("servers", {}):
        users[user_id]['active_server'] = server_name
        save_users(users)
        await query.edit_message_text(f"✅ Сервер «{escape_markdown(server_name)}» назначен активным\.", parse_mode='MarkdownV2')
        await start_command(update, context)
    else:
        await query.edit_message_text("❌ Ошибка: Не удалось установить активный сервер\.", parse_mode='MarkdownV2')

# --- ВОССТАНОВЛЕННАЯ ФУНКЦИЯ ---
@server_registered
async def show_instructions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает инструкцию по установке агента по кнопке."""
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    user_id = str(query.from_user.id)
    user_data = load_users()[user_id]
    secret_key = user_data["servers"][server_name]['secret_key']
    
    AGENT_URL = f"https://raw.githubusercontent.com/{context.bot_data.get('repo_owner', 'Leonid1095')}/{context.bot_data.get('repo_name', 'telegram-server-bot')}/main/install.sh"
    
    text = (
        f"📋 *Инструкция по установке агента для сервера «{escape_markdown(server_name)}»*\n\n"
        f"1\\. Выполните на сервере \(от `root`\) одну команду:\n"
        f"```bash\nwget -qO- {AGENT_URL} | bash -s -- --key {secret_key}\n```"
    )
    # Отправляем новым сообщением, чтобы не затирать меню управления
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode='MarkdownV2')


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Получаю статус...")
    user_id = str(query.from_user.id)
    users = load_users()
    user_data = users.get(user_id)

    if not user_data or 'active_server' not in user_data:
        await query.edit_message_text("❗️ Активный сервер не выбран\. Пожалуйста, выберите его в меню «Мои серверы»\.", reply_markup=get_main_menu_keyboard(), parse_mode='MarkdownV2')
        return

    active_server_name = user_data['active_server']
    server_info = user_data['servers'][active_server_name]
    server_ip, secret_key = server_info['server_ip'], server_info['secret_key']
    url = f"http://{server_ip}:5000/status"
    headers = {"X-Secret-Key": secret_key}
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, headers=headers, timeout=10))
        response.raise_for_status()
        status_message = get_status_text(response.json(), active_server_name)
        await query.edit_message_text(status_message, reply_markup=get_main_menu_keyboard(), parse_mode='MarkdownV2')
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка подключения к агенту {server_ip} для {user_id}: {e}")
        error_text = fr"⛔️ *Не удалось подключиться к активному серверу* `{escape_markdown(active_server_name)}`\."
        await query.edit_message_text(error_text, reply_markup=get_main_menu_keyboard(), parse_mode='MarkdownV2')

# --- Мониторинг ---

async def monitoring_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    user_sub = subs.get(user_id)
    is_subscribed = user_sub is not None and user_sub.get("enabled", False)
    settings = user_sub if user_sub else DEFAULT_MONITOR_SETTINGS.copy()

    if is_subscribed:
        text = (
            "🔔 *Мониторинг активен*\n\n"
            "Вы получаете алерты при проблемах на сервере\\.\n"
            "Настройте пороги срабатывания ниже\\."
        )
    else:
        text = (
            "🔕 *Мониторинг неактивен*\n\n"
            "Подпишитесь, чтобы получать алерты:\n"
            "• Диск, RAM, CPU\n"
            "• HDD здоровье \\(SMART\\)\n"
            "• Упавшие сервисы\n"
            "• Docker проблемы\n"
            "• SSL сертификаты\n"
            "• Атаки \\(fail2ban\\)"
        )

    await query.edit_message_text(
        text,
        reply_markup=get_monitoring_keyboard(is_subscribed, settings),
        parse_mode='MarkdownV2'
    )

async def monitor_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)

    # Проверить что у пользователя есть хотя бы один сервер
    users = load_users()
    if user_id not in users or not users[user_id].get("servers"):
        await query.answer("Сначала добавьте сервер!")
        await query.edit_message_text(
            "❗️ *Сначала добавьте сервер*\n\n"
            "Перейдите в «Мои серверы» и добавьте сервер с установленным агентом\\.\n"
            "После этого вы сможете подписаться на мониторинг\\.",
            reply_markup=get_monitoring_keyboard(False, DEFAULT_MONITOR_SETTINGS),
            parse_mode='MarkdownV2'
        )
        return

    await query.answer("Подписка оформлена!")
    subs = load_monitor_subs()
    if user_id not in subs:
        subs[user_id] = DEFAULT_MONITOR_SETTINGS.copy()
    subs[user_id]["enabled"] = True
    subs[user_id]["username"] = query.from_user.username or query.from_user.first_name
    save_monitor_subs(subs)

    await query.edit_message_text(
        "✅ *Вы подписаны на алерты\\!*\n\n"
        "Бот будет проверять ваш активный сервер каждые 5 минут\n"
        "и присылать уведомления при проблемах\\.\n\n"
        "⚠️ На сервере должен быть установлен агент\\.\n"
        "Настройте пороги срабатывания ниже\\.",
        reply_markup=get_monitoring_keyboard(True, subs[user_id]),
        parse_mode='MarkdownV2'
    )

async def monitor_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Подписка отменена")
    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    if user_id in subs:
        subs[user_id]["enabled"] = False
        save_monitor_subs(subs)

    await query.edit_message_text(
        "🔕 *Вы отписались от алертов*\n\n"
        "Вы больше не будете получать уведомления\\.\n"
        "Подпишитесь снова в любой момент\\.",
        reply_markup=get_monitoring_keyboard(False, DEFAULT_MONITOR_SETTINGS),
        parse_mode='MarkdownV2'
    )

async def monitor_set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    param = query.data.replace("monitor_set_", "") + "_warn"
    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    current = subs.get(user_id, DEFAULT_MONITOR_SETTINGS).get(param, 80)

    labels = {"disk_warn": "💾 Порог диска", "ram_warn": "🧠 Порог RAM", "cpu_warn": "🔥 Порог CPU Load"}
    text = f"*{escape_markdown(labels.get(param, param))}*\n\nВыберите значение:"

    await query.edit_message_text(
        text,
        reply_markup=get_threshold_keyboard(param, current),
        parse_mode='MarkdownV2'
    )

async def monitor_set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # monitor_val_disk_warn_80
    parts = query.data.split("_")
    value = int(parts[-1])
    param = parts[-3] + "_" + parts[-2]  # disk_warn, ram_warn, cpu_warn

    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    if user_id not in subs:
        subs[user_id] = DEFAULT_MONITOR_SETTINGS.copy()
    subs[user_id][param] = value
    save_monitor_subs(subs)

    await query.answer(f"Установлено: {value}%")

    # Вернуться в меню мониторинга
    await query.edit_message_text(
        "🔔 *Мониторинг активен*\n\n"
        f"✅ Порог обновлён: {value}%\n"
        "Настройте другие пороги или вернитесь в меню\\.",
        reply_markup=get_monitoring_keyboard(True, subs[user_id]),
        parse_mode='MarkdownV2'
    )

async def monitor_status_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Собираю данные...")
    user_id = str(query.from_user.id)
    subs = load_monitor_subs()
    settings = subs.get(user_id, DEFAULT_MONITOR_SETTINGS)

    users = load_users()
    user_data = users.get(user_id)

    if not user_data or 'active_server' not in user_data:
        await query.edit_message_text(
            "❗️ *Активный сервер не выбран*\n\nВыберите сервер в «Мои серверы»\\.",
            reply_markup=get_monitoring_keyboard(True, settings),
            parse_mode='MarkdownV2'
        )
        return

    active_name = user_data['active_server']
    server_info = user_data['servers'].get(active_name, {})
    server_ip = server_info.get('server_ip', '')
    secret_key = server_info.get('secret_key', '')

    try:
        loop = asyncio.get_event_loop()
        url = f"http://{server_ip}:5000/status"
        headers = {"X-Secret-Key": secret_key}
        response = await loop.run_in_executor(
            None, lambda: requests.get(url, headers=headers, timeout=10)
        )
        response.raise_for_status()
        data = response.json()

        cpu = escape_markdown(str(data.get('cpu', 'N/A')))
        mem = data.get('memory', {})
        disk = data.get('disk', {})

        text = (
            f"📊 *Статус сервера «{escape_markdown(active_name)}»*\n"
            f"IP: `{escape_markdown(server_ip)}`\n\n"
            f"🔥 *CPU:* {cpu}%\n"
            f"🧠 *RAM:* {escape_markdown(mem.get('used', '?'))}/{escape_markdown(mem.get('total', '?'))} GB "
            f"\\({escape_markdown(str(mem.get('percent', '?')))}%\\)\n"
            f"💾 *Диск:* {escape_markdown(disk.get('used', '?'))}/{escape_markdown(disk.get('total', '?'))} GB "
            f"\\({escape_markdown(str(disk.get('percent', '?')))}%\\)\n\n"
            f"*Ваши пороги:*\n"
            f"  💾 Диск: {settings.get('disk_warn', 80)}%\n"
            f"  🧠 RAM: {settings.get('ram_warn', 90)}%\n"
            f"  🔥 CPU: {settings.get('cpu_warn', 200)}%"
        )
    except requests.exceptions.RequestException:
        text = (
            f"⛔️ *Не удалось подключиться к серверу*\n"
            f"`{escape_markdown(active_name)}` \\(`{escape_markdown(server_ip)}`\\)\n\n"
            f"Убедитесь, что агент установлен и работает\\."
        )

    await query.edit_message_text(
        text,
        reply_markup=get_monitoring_keyboard(True, settings),
        parse_mode='MarkdownV2'
    )

# --- Диалоги ---

async def addserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(r"📝 Введите **имя** для вашего нового сервера \(например, `web-server-de`\)\. Имя должно быть уникальным, без пробелов\.", parse_mode='MarkdownV2')
    return ASK_SERVER_NAME

async def ask_server_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['server_name'] = update.message.text.strip()
    await update.message.reply_text(r"Теперь введите **IP\-адрес** этого сервера\.", parse_mode='MarkdownV2')
    return ASK_IP

async def ask_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    server_ip = update.message.text.strip()
    server_name = context.user_data.get('server_name')

    if not is_valid_ip(server_ip):
        await update.message.reply_text(r"❌ Некорректный IP\-адрес\. Попробуйте снова\.", parse_mode='MarkdownV2')
        return ASK_IP

    user_id = str(update.effective_user.id)
    users = load_users()
    user_servers = users.setdefault(user_id, {"servers": {}})["servers"]
    
    user_servers[server_name] = {"server_ip": server_ip, "secret_key": str(uuid.uuid4())}
    users[user_id]['active_server'] = server_name
    save_users(users)
    
    await update.message.reply_text(fr"✅ Сервер `{escape_markdown(server_name)}` успешно добавлен и назначен активным\!", parse_mode='MarkdownV2')
    await start_command(update, context)
    return ConversationHandler.END

async def deleteserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_name = query.data.split('_', 2)[-1]
    await query.edit_message_text(
        fr"⚠️ Вы уверены, что хотите удалить сервер `{escape_markdown(server_name)}`\?",
        reply_markup=get_delete_confirm_keyboard(server_name),
        parse_mode='MarkdownV2'
    )

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
        else:
            save_users(users)
        save_users(users)
        await query.edit_message_text(
            fr"✅ Сервер `{escape_markdown(server_name)}` удалён\.",
            parse_mode='MarkdownV2'
        )
        # Show main menu after deletion
        await context.bot.send_message(
            chat_id=user_id,
            text=r"🏠 *Главное меню*",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='MarkdownV2'
        )
    else:
        await query.edit_message_text("❌ Сервер не найден\.", parse_mode='MarkdownV2')

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_or_edit(update, r"❌ Действие отменено\.")
    await start_command(update, context)
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update:", exc_info=context.error)

async def post_init(application: Application):
    application.bot_data['repo_owner'] = 'Leonid1095'
    application.bot_data['repo_name'] = 'telegram-server-bot'
    logger.info("Данные о репозитории загружены.")

def main():
    if not config.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не установлен в config.py")
        return
    
    application = Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_error_handler(error_handler)
    
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
    application.add_handler(CallbackQueryHandler(status_command, pattern='^menu_status$'))
    application.add_handler(CallbackQueryHandler(myservers_menu, pattern='^menu_myservers$'))
    # Мониторинг
    application.add_handler(CallbackQueryHandler(monitoring_menu, pattern='^menu_monitoring$'))
    application.add_handler(CallbackQueryHandler(monitor_subscribe, pattern='^monitor_sub$'))
    application.add_handler(CallbackQueryHandler(monitor_unsubscribe, pattern='^monitor_unsub$'))
    application.add_handler(CallbackQueryHandler(monitor_set_threshold, pattern=r'^monitor_set_(disk|ram|cpu)$'))
    application.add_handler(CallbackQueryHandler(monitor_set_value, pattern=r'^monitor_val_'))
    application.add_handler(CallbackQueryHandler(monitor_status_now, pattern='^monitor_status_now$'))

    application.add_handler(CallbackQueryHandler(select_server_callback, pattern=r'^select_server_'))
    application.add_handler(CallbackQueryHandler(set_active_server_callback, pattern=r'^set_active_'))
    # ИСПРАВЛЕНИЕ: Добавляем обработчик для новой кнопки
    application.add_handler(CallbackQueryHandler(show_instructions_callback, pattern=r'^show_instructions_'))
    application.add_handler(CallbackQueryHandler(deleteserver_start, pattern=r'^delete_server_'))
    application.add_handler(CallbackQueryHandler(confirm_delete_callback, pattern=r'^confirm_delete_'))

    application.add_handler(add_conv)

    logger.info("Центральный бот (v8.4, Финальная версия) запущен...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
