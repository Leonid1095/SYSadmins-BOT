# bot.py (Версия 7.1: Финальные исправления форматирования)

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
    get_main_menu_keyboard,
    get_start_keyboard,
    get_delete_confirmation_keyboard,
    get_myserver_keyboard,
)

# --- Настройки ---
USERS_FILE = "users.json"
ASK_IP, CONFIRM_DELETE = range(2)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def load_users():
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_users(users_data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users_data, f, indent=4, ensure_ascii=False)

def is_valid_ip(ip: str) -> bool:
    parts = ip.split('.')
    if len(parts) != 4: return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False

def server_registered(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id)
        if user_id not in load_users():
            await update.message.reply_text(r"❗️ У вас нет зарегистрированных серверов\. Используйте /start, чтобы добавить\.", parse_mode='MarkdownV2')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def get_status_text(data: dict) -> str:
    try:
        cpu = escape_markdown(data.get('cpu', 'N/A'))
        mem = data.get('memory', {})
        disk = data.get('disk', {})
        
        mem_text = f"Использовано {escape_markdown(mem.get('used', 'N/A'))} / {escape_markdown(mem.get('total', 'N/A'))} ГБ \\({escape_markdown(mem.get('percent', 'N/A'))}%\\)"
        disk_text = f"Использовано {escape_markdown(disk.get('used', 'N/A'))} / {escape_markdown(disk.get('total', 'N/A'))} ГБ \\({escape_markdown(disk.get('percent', 'N/A'))}%\\)"
        
        return (
            f"*📊 Статус вашего сервера*\n\n"
            f"🔥 *Процессор:* {cpu}%\n"
            f"🧠 *Память:* {mem_text}\n"
            f"💾 *Диск:* {disk_text}"
        )
    except Exception as e:
        logger.error(f"Ошибка формирования текста статуса: {e}")
        return "*❌ Ошибка при обработке данных статуса*"

async def send_or_edit(update: Update, text: str, reply_markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='MarkdownV2')

# --- Обработчики команд и кнопок ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = str(query.from_user.id)
    else:
        user_id = str(update.effective_user.id)

    if user_id in load_users():
        text = r"🏠 *Главное меню*"
        keyboard = get_main_menu_keyboard()
    else:
        text = r"👋 *Привет\!* Чтобы начать, добавьте свой сервер\."
        keyboard = get_start_keyboard()
    
    await send_or_edit(update, text, keyboard)

@server_registered
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer("Получаю статус...")
    
    user_id = str(update.effective_user.id)
    user_data = load_users().get(user_id, {})
    server_ip, secret_key = user_data.get('server_ip'), user_data.get('secret_key')
    
    if not all([server_ip, secret_key]):
        await send_or_edit(update, r"❌ Ошибка: не удалось найти данные вашего сервера\.")
        return

    url = f"http://{server_ip}:5000/status"
    headers = {"X-Secret-Key": secret_key}
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, headers=headers, timeout=10))
        response.raise_for_status()
        status_message = get_status_text(response.json())
        await send_or_edit(update, status_message, get_main_menu_keyboard())
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка подключения к агенту {server_ip} для пользователя {user_id}: {e}")
        error_text = fr"⛔️ *Не удалось подключиться к серверу* `{escape_markdown(server_ip)}`\."
        await send_or_edit(update, error_text, get_main_menu_keyboard())

@server_registered
async def myserver_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    
    user_id = str(update.effective_user.id)
    user_data = load_users()[user_id]
    server_ip = escape_markdown(user_data['server_ip'])
    secret_key = escape_markdown(user_data['secret_key'])

    text = (
        f"⚙️ *Информация о вашем сервере*\n\n"
        f"**IP\-адрес:** `{server_ip}`\n"
        f"**Секретный ключ:** `{secret_key}`"
    )
    await send_or_edit(update, text, get_myserver_keyboard())

@server_registered
async def show_instructions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user_data = load_users()[user_id]
    secret_key = user_data['secret_key']
    
    AGENT_URL = "https://raw.githubusercontent.com/vas-G/server-monitoring-telegram-bot/main/agent.py"

    text = (
        f"📋 *Инструкция по установке агента*\n\n"
        f"1\\. Скачайте агент:\n"
        f"```bash\nwget -O agent.py {AGENT_URL}\n```\n"
        f"2\\. Запустите его с вашим ключом:\n"
        f"```bash\nSECRET_KEY=\"{secret_key}\" python3 agent.py\n```"
    )
    await query.edit_message_text(text, reply_markup=get_myserver_keyboard(), parse_mode='MarkdownV2')

# --- Диалоги для добавления/удаления ---

async def addserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # ИСПРАВЛЕНИЕ ЗДЕСЬ: Экранируем дефис в "IP-адрес"
    await query.edit_message_text(r"📝 Введите IP\-адрес вашего сервера \(например, `192.168.1.100`\):", parse_mode='MarkdownV2')
    return ASK_IP

async def ask_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    server_ip = update.message.text.strip()
    if not is_valid_ip(server_ip):
        await update.message.reply_text(r"❌ Некорректный IP\-адрес\. Попробуйте снова\.", parse_mode='MarkdownV2')
        return ASK_IP
    
    create_server_entry(update.effective_user.id, server_ip)
    await update.message.reply_text(fr"✅ Сервер `{escape_markdown(server_ip)}` успешно добавлен\!", parse_mode='MarkdownV2')
    await start_command(update, context)
    return ConversationHandler.END

async def deleteserver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(r"⚠️ Вы уверены, что хотите удалить свой сервер\?", reply_markup=get_delete_confirmation_keyboard(), parse_mode='MarkdownV2')
    return CONFIRM_DELETE

async def delete_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if query.data == "delete_confirm_yes":
        users = load_users()
        if user_id in users:
            ip = users.pop(user_id)['server_ip']
            save_users(users)
            await query.edit_message_text(fr"✅ Сервер `{escape_markdown(ip)}` удален\.", parse_mode='MarkdownV2')
        else:
            await query.edit_message_text(r"❌ Сервер не найден\.", parse_mode='MarkdownV2')
        await start_command(update, context)
    else:
        await query.edit_message_text("Действие отменено.")
        await start_command(update, context)
        
    return ConversationHandler.END

def create_server_entry(user_id: int, server_ip: str):
    users = load_users()
    users[str(user_id)] = {"server_ip": server_ip, "secret_key": str(uuid.uuid4())}
    save_users(users)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_or_edit(update, r"❌ Действие отменено\.")
    await start_command(update, context)
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update:", exc_info=context.error)

# --- Главная функция ---
def main():
    if not config.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не установлен в config.py")
        return
    
    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    application.add_error_handler(error_handler)
    
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(addserver_start, pattern='^menu_addserver$')],
        states={ASK_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ip_handler)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    delete_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deleteserver_start, pattern='^menu_delete$')],
        states={CONFIRM_DELETE: [CallbackQueryHandler(delete_confirm_handler, pattern=r'^(delete_confirm_yes|delete_confirm_no)$')]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(start_command, pattern='^menu_back$'))
    application.add_handler(CallbackQueryHandler(status_command, pattern='^menu_status$'))
    application.add_handler(CallbackQueryHandler(myserver_command, pattern='^menu_myserver$'))
    application.add_handler(CallbackQueryHandler(show_instructions_callback, pattern='^myserver_show_instructions$'))
    application.add_handler(add_conv)
    application.add_handler(delete_conv)

    logger.info("Центральный бот (v7.1, Финальная версия) запущен...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
