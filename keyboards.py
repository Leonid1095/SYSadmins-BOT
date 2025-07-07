# keyboards.py (Финальная, синхронизированная версия)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Возвращает главное меню для навигации."""
    keyboard = [
        [InlineKeyboardButton("📊 Статус активного сервера", callback_data="menu_status")],
        [InlineKeyboardButton("🗂️ Мои серверы", callback_data="menu_myservers")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_server_list_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    """Динамически создает клавиатуру со списком серверов."""
    keyboard = []
    servers = user_data.get("servers", {})
    active_server_name = user_data.get("active_server")

    for server_name in servers:
        button_text = f"✅ {server_name}" if server_name == active_server_name else server_name
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_server_{server_name}")])
    
    keyboard.append([InlineKeyboardButton("➕ Добавить новый сервер", callback_data="add_server_start")])
    return InlineKeyboardMarkup(keyboard)

def get_server_management_keyboard(server_name: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для управления выбранным сервером."""
    keyboard = [
        [InlineKeyboardButton("🚀 Сделать активным", callback_data=f"set_active_{server_name}")],
        [InlineKeyboardButton("📋 Показать инструкцию", callback_data=f"show_instructions_{server_name}")],
        [InlineKeyboardButton("🗑️ Удалить сервер", callback_data=f"delete_server_{server_name}")],
        [InlineKeyboardButton("🔙 К списку серверов", callback_data="menu_myservers")]
    ]
    return InlineKeyboardMarkup(keyboard)
