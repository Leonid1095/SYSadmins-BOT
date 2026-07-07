# keyboards.py (Финальная, синхронизированная версия)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Возвращает главное меню для навигации."""
    keyboard = [
        [InlineKeyboardButton("📊 Статус активного сервера", callback_data="menu_status")],
        [InlineKeyboardButton("🗂️ Мои серверы", callback_data="menu_myservers")],
        [InlineKeyboardButton("🔔 Мониторинг и алерты", callback_data="menu_monitoring")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_monitoring_keyboard(is_subscribed: bool, settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек мониторинга."""
    sub_text = "🔕 Отписаться от алертов" if is_subscribed else "🔔 Подписаться на алерты"
    sub_data = "monitor_unsub" if is_subscribed else "monitor_sub"

    keyboard = [
        [InlineKeyboardButton(sub_text, callback_data=sub_data)],
    ]

    if is_subscribed:
        keyboard.append([InlineKeyboardButton(
            f"💾 Порог диска: {settings.get('disk_warn', 80)}%",
            callback_data="monitor_set_disk"
        )])
        keyboard.append([InlineKeyboardButton(
            f"🧠 Порог RAM: {settings.get('ram_warn', 90)}%",
            callback_data="monitor_set_ram"
        )])
        keyboard.append([InlineKeyboardButton(
            f"🔥 Порог CPU: {settings.get('cpu_warn', 90)}%",
            callback_data="monitor_set_cpu"
        )])
        keyboard.append([InlineKeyboardButton(
            f"🎮 Порог GPU темп.: {settings.get('gpu_temp_warn', 80)}°C",
            callback_data="monitor_set_gpu_temp"
        )])
        keyboard.append([InlineKeyboardButton(
            "📊 Текущий статус сервера", callback_data="monitor_status_now"
        )])

    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(keyboard)


def get_threshold_keyboard(param: str, current: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора порога."""
    if param == "cpu_warn":
        values = [70, 75, 80, 85, 90, 95]
        fmt = "{}%"
    elif param == "gpu_temp_warn":
        values = [70, 75, 80, 85, 90]
        fmt = "{}°C"
    else:
        values = [70, 75, 80, 85, 90, 95]
        fmt = "{}%"

    keyboard = []
    row = []
    for v in values:
        label = f"✅ {fmt.format(v)}" if v == current else fmt.format(v)
        row.append(InlineKeyboardButton(label, callback_data=f"monitor_val_{param}_{v}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_monitoring")])
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

def get_delete_confirm_keyboard(server_name: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру подтверждения удаления сервера."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{server_name}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"select_server_{server_name}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
