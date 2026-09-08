"""Клавиатуры бота.

Правило навигации: с любого экрана видно, куда вернуться, и ни один экран не
оканчивается тупиком. Раньше часть сообщений (удаление сервера, «сервер не
найден», ошибка подключения) приходила вообще без кнопок — владелец оставался
в чате с текстом и без способа продолжить, кроме /start вручную.

`callback_data` ограничена Telegram 64 байтами. Имена серверов приходят от
владельца, поэтому их длина проверяется при добавлении (см. bot.py), а не здесь.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Кнопки-возвраты собраны здесь, чтобы подпись была одинаковой на всех экранах:
# разнобой («Назад», «В меню», «Главное меню») заставляет читать кнопку каждый раз.
HOME = InlineKeyboardButton("🏠 Главное меню", callback_data="menu_back")
TO_INFRA = InlineKeyboardButton("🔙 К инфраструктуре", callback_data="menu_infra")


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 Этот сервер", callback_data="menu_host")],
        [InlineKeyboardButton("🌐 Инфраструктура", callback_data="menu_infra")],
        [InlineKeyboardButton("🗂 Удалённые серверы", callback_data="menu_myservers")],
        [InlineKeyboardButton("🔔 Алерты и пороги", callback_data="menu_monitoring")],
    ])


def get_host_keyboard() -> InlineKeyboardMarkup:
    """Карточка центрального сервера."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu_host")],
        [InlineKeyboardButton("🌐 Инфраструктура", callback_data="menu_infra")],
        [HOME],
    ])


def get_infra_keyboard() -> InlineKeyboardMarkup:
    """Разделы инфраструктуры. По два в ряд — иначе список выше не помещается
    на экране телефона вместе с кнопками."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐳 Контейнеры", callback_data="infra_containers"),
            InlineKeyboardButton("⚙️ Службы", callback_data="infra_services"),
        ],
        [
            InlineKeyboardButton("🔗 Сайты", callback_data="infra_sites"),
            InlineKeyboardButton("🔒 Сертификаты", callback_data="infra_certs"),
        ],
        [InlineKeyboardButton("🛡 Безопасность", callback_data="infra_security")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu_infra")],
        [HOME],
    ])


def get_infra_section_keyboard(section: str) -> InlineKeyboardMarkup:
    """Экран одного раздела: обновить, назад, домой."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"infra_{section}")],
        [TO_INFRA],
        [HOME],
    ])


def get_monitoring_keyboard(is_subscribed: bool, settings: dict) -> InlineKeyboardMarkup:
    """Настройки алертов."""
    sub_text = "🔕 Отключить алерты" if is_subscribed else "🔔 Включить алерты"
    sub_data = "monitor_unsub" if is_subscribed else "monitor_sub"

    keyboard = [[InlineKeyboardButton(sub_text, callback_data=sub_data)]]

    if is_subscribed:
        keyboard += [
            [InlineKeyboardButton(
                f"💾 Диск: предупредить на {settings.get('disk_warn', 80)}%",
                callback_data="monitor_set_disk")],
            [InlineKeyboardButton(
                f"🧠 Память: предупредить на {settings.get('ram_warn', 90)}%",
                callback_data="monitor_set_ram")],
            [InlineKeyboardButton(
                f"🔥 Процессор: предупредить на {settings.get('cpu_warn', 90)}%",
                callback_data="monitor_set_cpu")],
            [InlineKeyboardButton(
                f"🎮 Видеокарта: предупредить на {settings.get('gpu_temp_warn', 80)}°C",
                callback_data="monitor_set_gpu_temp")],
        ]

    keyboard.append([HOME])
    return InlineKeyboardMarkup(keyboard)


def get_threshold_keyboard(param: str, current: int) -> InlineKeyboardMarkup:
    """Выбор порога. Текущее значение помечено галочкой, чтобы было видно,
    что именно меняешь."""
    if param == "gpu_temp_warn":
        values, fmt = [70, 75, 80, 85, 90], "{}°C"
    else:
        values, fmt = [70, 75, 80, 85, 90, 95], "{}%"

    keyboard, row = [], []
    for v in values:
        label = f"✅ {fmt.format(v)}" if v == current else fmt.format(v)
        row.append(InlineKeyboardButton(label, callback_data=f"monitor_val_{param}_{v}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 К алертам", callback_data="menu_monitoring")])
    return InlineKeyboardMarkup(keyboard)


def get_server_list_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    """Список удалённых серверов. Активный помечен."""
    keyboard = []
    servers = user_data.get("servers", {})
    active = user_data.get("active_server")

    for name in servers:
        text = f"✅ {name}" if name == active else name
        keyboard.append([InlineKeyboardButton(text, callback_data=f"select_server_{name}")])

    keyboard.append([InlineKeyboardButton("➕ Добавить сервер", callback_data="add_server_start")])
    keyboard.append([HOME])
    return InlineKeyboardMarkup(keyboard)


def get_server_management_keyboard(server_name: str) -> InlineKeyboardMarkup:
    """Управление одним сервером."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Показать состояние", callback_data=f"server_status_{server_name}")],
        [InlineKeyboardButton("🚀 Сделать активным", callback_data=f"set_active_{server_name}")],
        [InlineKeyboardButton("📋 Как установить агента", callback_data=f"show_instructions_{server_name}")],
        [InlineKeyboardButton("🗑 Удалить сервер", callback_data=f"delete_server_{server_name}")],
        [InlineKeyboardButton("🔙 К списку серверов", callback_data="menu_myservers")],
    ])


def get_delete_confirm_keyboard(server_name: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления. Отмена — первой: её нажимают чаще."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Отмена", callback_data=f"select_server_{server_name}"),
        InlineKeyboardButton("🗑 Да, удалить", callback_data=f"confirm_delete_{server_name}"),
    ]])


def get_back_keyboard(target: str = "menu_back", label: str = "🏠 Главное меню") -> InlineKeyboardMarkup:
    """Одна кнопка возврата — для экранов ошибок, которые раньше приходили
    вообще без клавиатуры."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])
