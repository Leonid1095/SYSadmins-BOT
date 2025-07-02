# keyboards.py (Версия для v6.0)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Возвращает главное меню для пользователя с зарегистрированным сервером."""
    keyboard = [
        [InlineKeyboardButton("📊 Статус сервера", callback_data="menu_status")],
        [InlineKeyboardButton("⚙️ Мой сервер и ключ", callback_data="menu_myserver")],
        [InlineKeyboardButton("🗑️ Удалить сервер", callback_data="menu_delete")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_start_keyboard() -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для нового пользователя с одной кнопкой 'Добавить сервер'."""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить сервер", callback_data="menu_addserver")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_delete_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для подтверждения удаления сервера."""
    keyboard = [
        [
            InlineKeyboardButton("Да, я уверен", callback_data="delete_confirm_yes"),
            InlineKeyboardButton("Нет, оставить", callback_data="delete_confirm_no"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_myserver_keyboard() -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для меню 'Мой сервер'."""
    keyboard = [
        [InlineKeyboardButton("📋 Показать инструкцию по установке", callback_data="myserver_show_instructions")],
        [InlineKeyboardButton("🔙 Назад в меню", callback_data="menu_back")],
    ]
    return InlineKeyboardMarkup(keyboard)
