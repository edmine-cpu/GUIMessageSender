"""
Генератор PDF-инструкции для Teleton.
Запуск: python generate_manual.py
"""

import os
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

# --- Пути ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOCKUPS_DIR = os.path.join(SCRIPT_DIR, "data", "mockups")
OUTPUT_PDF = os.path.join(SCRIPT_DIR, "Teleton_Manual.pdf")
FONT_DIR = "C:/Windows/Fonts"

os.makedirs(MOCKUPS_DIR, exist_ok=True)


# ============================================================
# Часть 1: Генерация мокапов GUI через Pillow
# ============================================================

# Цвета (Dark Theme)
BG = (30, 30, 30)
SIDEBAR_BG = (43, 43, 43)
CARD_BG = (50, 50, 55)
INPUT_BG = (60, 60, 65)
ACCENT = (52, 131, 235)
ACCENT_HOVER = (40, 110, 200)
GREEN = (47, 165, 114)
RED = (231, 76, 60)
ORANGE = (243, 156, 18)
TEXT_WHITE = (230, 230, 230)
TEXT_GRAY = (160, 160, 160)
TEXT_DIM = (120, 120, 120)
BORDER = (70, 70, 75)


def get_font(size=14, bold=False):
    """Загрузка шрифта Segoe UI"""
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    path = os.path.join(FONT_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def draw_rounded_rect(draw, xy, radius, fill, outline=None):
    """Скруглённый прямоугольник"""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline)


def draw_input_field(draw, x, y, w, h, text="", placeholder=""):
    """Поле ввода"""
    draw_rounded_rect(draw, (x, y, x + w, y + h), 6, INPUT_BG, BORDER)
    font = get_font(13)
    if text:
        draw.text((x + 10, y + (h - 16) // 2), text, fill=TEXT_WHITE, font=font)
    elif placeholder:
        draw.text((x + 10, y + (h - 16) // 2), placeholder, fill=TEXT_DIM, font=font)


def draw_button(draw, x, y, w, h, text, color=ACCENT):
    """Кнопка"""
    draw_rounded_rect(draw, (x, y, x + w, y + h), 8, color)
    font = get_font(13, bold=True)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((x + (w - tw) // 2, y + (h - 16) // 2), text, fill=(255, 255, 255), font=font)


def draw_checkbox(draw, x, y, text, checked=False):
    """Чекбокс"""
    size = 18
    draw_rounded_rect(draw, (x, y, x + size, y + size), 4, ACCENT if checked else INPUT_BG, BORDER)
    if checked:
        draw.text((x + 3, y - 1), "✓", fill=(255, 255, 255), font=get_font(14, True))
    draw.text((x + size + 8, y), text, fill=TEXT_WHITE, font=get_font(13))


def draw_sidebar(draw, active_key, w=180, h=600):
    """Боковая панель навигации"""
    draw.rectangle((0, 0, w, h), fill=SIDEBAR_BG)
    # Логотип
    draw.text((30, 22), "TELETON", fill=TEXT_WHITE, font=get_font(20, bold=True))
    # Пункты меню
    items = [
        ("accounts", "Аккаунты"),
        ("tasks", "Задачи"),
        ("parsing", "Парсинг"),
        ("broadcast", "Рассылка"),
        ("stats", "Статистика"),
        ("settings", "Настройки"),
    ]
    y = 70
    for key, label in items:
        if key == active_key:
            draw_rounded_rect(draw, (8, y, w - 8, y + 36), 8, (75, 75, 80))
        draw.text((20, y + 8), label, fill=TEXT_WHITE, font=get_font(14))
        y += 42


def draw_tab_bar(draw, x, y, tabs, active_idx, w_each=150):
    """Панель вкладок"""
    tab_bg = (45, 45, 50)
    draw_rounded_rect(draw, (x, y, x + len(tabs) * w_each + 10, y + 36), 8, tab_bg)
    for i, label in enumerate(tabs):
        tx = x + 5 + i * w_each
        if i == active_idx:
            draw_rounded_rect(draw, (tx, y + 3, tx + w_each - 4, y + 33), 6, ACCENT)
            draw.text((tx + 12, y + 7), label, fill=(255, 255, 255), font=get_font(13, True))
        else:
            draw.text((tx + 12, y + 7), label, fill=TEXT_GRAY, font=get_font(13))


def draw_segmented(draw, x, y, items, active_idx, w_each=140):
    """Сегментированная кнопка"""
    total_w = len(items) * w_each
    draw_rounded_rect(draw, (x, y, x + total_w, y + 32), 6, INPUT_BG, BORDER)
    for i, label in enumerate(items):
        ix = x + i * w_each
        if i == active_idx:
            draw_rounded_rect(draw, (ix + 2, y + 2, ix + w_each - 2, y + 30), 5, ACCENT)
            draw.text((ix + 14, y + 6), label, fill=(255, 255, 255), font=get_font(12, True))
        else:
            draw.text((ix + 14, y + 6), label, fill=TEXT_GRAY, font=get_font(12))


def draw_table(draw, x, y, headers, rows, col_widths):
    """Простая таблица"""
    font_b = get_font(12, True)
    font = get_font(12)
    # Заголовки
    cx = x
    for i, h in enumerate(headers):
        draw.text((cx, y), h, fill=TEXT_GRAY, font=font_b)
        cx += col_widths[i]
    y += 24
    draw.line((x, y, x + sum(col_widths), y), fill=BORDER, width=1)
    y += 6
    # Данные
    for row in rows:
        cx = x
        for i, val in enumerate(row):
            draw.text((cx, y), str(val), fill=TEXT_WHITE, font=font)
            cx += col_widths[i]
        y += 22
    return y


def draw_log_area(draw, x, y, w, h, lines):
    """Область лога"""
    draw_rounded_rect(draw, (x, y, x + w, y + h), 6, (25, 25, 28), BORDER)
    font = get_font(11)
    ly = y + 6
    for line in lines:
        color = GREEN if line.startswith("[+]") else (RED if line.startswith("[-]") else TEXT_GRAY)
        draw.text((x + 8, ly), line, fill=color, font=font)
        ly += 17


# ---- Мокап 1: Аккаунты ----
def mockup_accounts():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "accounts", h=600)
    sx = 200  # Начало контентной области

    draw.text((sx + 20, 15), "Аккаунты", fill=TEXT_WHITE, font=get_font(20, True))

    # Кнопки
    draw_button(draw, sx + 20, 55, 110, 32, "Добавить")
    draw_button(draw, sx + 140, 55, 120, 32, "Авторизация")
    draw_button(draw, sx + 270, 55, 100, 32, "Вкл/Выкл")
    draw_button(draw, sx + 380, 55, 100, 32, "Удалить", RED)

    # Таблица
    headers = ["Телефон", "API ID", "Прокси", "Активен", "Отправлено"]
    rows = [
        ("+79001234567", "12345678", "socks5://...", "Да", "12"),
        ("+79007654321", "87654321", "—", "Да", "5"),
        ("+79009876543", "11223344", "socks5://...", "Нет", "0"),
    ]
    widths = [140, 100, 160, 80, 100]
    end_y = draw_table(draw, sx + 20, 110, headers, rows, widths)

    # Лог
    draw_log_area(draw, sx + 20, end_y + 30, 730, 100, [
        "[+] Аккаунт +79001234567 добавлен",
        "[+] Авторизация +79001234567 завершена!",
        "[~] Код отправлен на +79007654321",
    ])

    img.save(os.path.join(MOCKUPS_DIR, "01_accounts.png"))


# ---- Мокап 2: Обычный парсинг ----
def mockup_parsing_regular():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "parsing", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Парсинг групп", fill=TEXT_WHITE, font=get_font(20, True))
    draw_tab_bar(draw, sx + 20, 50, ["Обычный парсинг", "Смарт-парсинг"], 0)

    # Форма
    y0 = 100
    font = get_font(13)
    draw.text((sx + 20, y0), "Группа:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 - 3, 250, 30, text="@crypto_chat")

    draw.text((sx + 20, y0 + 40), "Аккаунт:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 37, 250, 30, text="+79001234567")

    draw_checkbox(draw, sx + 20, y0 + 85, "Aggressive (поиск по алфавиту)", checked=True)
    draw_checkbox(draw, sx + 20, y0 + 115, "Комментаторы канала", checked=False)

    draw.text((sx + 20, y0 + 150), "Лимит постов:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 147, 100, 30, text="50")

    draw_button(draw, sx + 20, y0 + 195, 160, 34, "Начать парсинг")

    # Статистика
    draw.text((sx + 20, y0 + 250), "Спарсенные группы:", fill=TEXT_WHITE, font=get_font(13, True))
    draw_table(draw, sx + 20, y0 + 275, ["Группа", "Пользователей"],
               [("@crypto_chat", "1247"), ("@traffic_ru", "856"), ("@smm_tools", "432")],
               [300, 150])

    # Лог
    draw_log_area(draw, sx + 20, y0 + 370, 730, 80, [
        "[~] Парсинг @crypto_chat через +79001234567...",
        "[+] Спарсено 1247 пользователей из @crypto_chat",
        "=== Итого: 1247 пользователей сохранено ===",
    ])

    img.save(os.path.join(MOCKUPS_DIR, "02_parsing_regular.png"))


# ---- Мокап 3: Смарт-парсинг (keywords) ----
def mockup_smart_parsing_keywords():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "parsing", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Парсинг групп", fill=TEXT_WHITE, font=get_font(20, True))
    draw_tab_bar(draw, sx + 20, 50, ["Обычный парсинг", "Смарт-парсинг"], 1)

    y0 = 100
    font = get_font(13)
    draw.text((sx + 20, y0), "Группа:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 - 3, 250, 30, text="@traffic_chat")

    draw.text((sx + 20, y0 + 40), "Аккаунт:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 37, 250, 30, text="+79001234567")

    draw.text((sx + 20, y0 + 80), "Режим:", fill=TEXT_WHITE, font=font)
    draw_segmented(draw, sx + 180, y0 + 77, ["Ключевые слова", "ИИ"], 0)

    draw.text((sx + 20, y0 + 120), "Ключевые слова:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 117, 350, 30, text="трафик, твиттер, reddit, автоматизация")

    draw.text((sx + 20, y0 + 160), "Лимит сообщений:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 157, 100, 30, text="500")

    draw_button(draw, sx + 20, y0 + 200, 200, 34, "Начать смарт-парсинг")

    # Таблица результатов
    draw.text((sx + 20, y0 + 255), "Найденные посты:", fill=TEXT_WHITE, font=get_font(13, True))
    draw_table(draw, sx + 20, y0 + 280,
               ["Пользователь", "Текст поста", "Совпадение"],
               [
                   ("@user_alpha", "Ищу трафик с твиттера на крипто...", "трафик, твиттер"),
                   ("@marketer99", "Нужен софт для автоматизации reddit", "автоматизация, reddit"),
                   ("@webmaster1", "Кто сливает трафик с соцсетей?", "трафик"),
               ],
               [130, 300, 200])

    draw_log_area(draw, sx + 20, y0 + 380, 730, 70, [
        "[+] Совпадение: @user_alpha — трафик, твиттер",
        "[+] Совпадение: @marketer99 — автоматизация, reddit",
        "=== Найдено совпадений: 3 ===",
    ])

    img.save(os.path.join(MOCKUPS_DIR, "03_smart_keywords.png"))


# ---- Мокап 4: Смарт-парсинг (AI) ----
def mockup_smart_parsing_ai():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "parsing", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Парсинг групп", fill=TEXT_WHITE, font=get_font(20, True))
    draw_tab_bar(draw, sx + 20, 50, ["Обычный парсинг", "Смарт-парсинг"], 1)

    y0 = 100
    font = get_font(13)
    draw.text((sx + 20, y0), "Группа:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 - 3, 250, 30, text="@traffic_chat")

    draw.text((sx + 20, y0 + 40), "Аккаунт:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 37, 250, 30, text="+79001234567")

    draw.text((sx + 20, y0 + 80), "Режим:", fill=TEXT_WHITE, font=font)
    draw_segmented(draw, sx + 180, y0 + 77, ["Ключевые слова", "ИИ"], 1)

    draw.text((sx + 20, y0 + 120), "Критерий ИИ:", fill=TEXT_WHITE, font=font)
    # Текстовая область
    draw_rounded_rect(draw, (sx + 180, y0 + 117, sx + 580, y0 + 187), 6, INPUT_BG, BORDER)
    draw.text((sx + 190, y0 + 123), "ищет трафик с твиттера или софт", fill=TEXT_WHITE, font=get_font(12))
    draw.text((sx + 190, y0 + 143), "для автоматизации реддита", fill=TEXT_WHITE, font=get_font(12))

    draw.text((sx + 20, y0 + 200), "Лимит сообщений:", fill=TEXT_WHITE, font=font)
    draw_input_field(draw, sx + 180, y0 + 197, 100, 30, text="200")

    draw_button(draw, sx + 20, y0 + 245, 200, 34, "Начать смарт-парсинг")

    draw.text((sx + 20, y0 + 300), "Найденные посты:", fill=TEXT_WHITE, font=get_font(13, True))
    draw_table(draw, sx + 20, y0 + 325,
               ["Пользователь", "Текст поста", "Причина ИИ"],
               [
                   ("@user_alpha", "Ищу трафик с твиттера на крипто", "Прямой запрос трафика"),
                   ("@dev_master", "Пишу бота для reddit, кому надо?", "Предлагает автоматизацию"),
               ],
               [130, 300, 200])

    draw_log_area(draw, sx + 20, y0 + 405, 730, 60, [
        "[+] AI совпадение: @user_alpha — Прямой запрос трафика с Twitter",
        "[+] AI совпадение: @dev_master — Предлагает автоматизацию Reddit",
    ])

    img.save(os.path.join(MOCKUPS_DIR, "04_smart_ai.png"))


# ---- Мокап 5: Рассылка (Упоминания) ----
def mockup_broadcast():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "broadcast", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Рассылка", fill=TEXT_WHITE, font=get_font(20, True))
    draw_tab_bar(draw, sx + 20, 50, ["Упоминания", "Рассылка"], 0)

    y0 = 100
    font = get_font(13)

    fields = [
        ("Целевая группа:", "@target_group"),
        ("Источник:", "@crypto_chat"),
        ("Лимит:", "0 (без лимита)"),
        ("Упоминаний/сообщение:", "5"),
    ]
    for i, (label, val) in enumerate(fields):
        draw.text((sx + 20, y0 + i * 38), label, fill=TEXT_WHITE, font=font)
        draw_input_field(draw, sx + 210, y0 + i * 38 - 3, 220, 30, text=val)

    draw.text((sx + 20, y0 + 155), "Сообщение:", fill=TEXT_WHITE, font=font)
    draw_rounded_rect(draw, (sx + 210, y0 + 152, sx + 530, y0 + 222), 6, INPUT_BG, BORDER)
    draw.text((sx + 220, y0 + 158), "{Привет|Здравствуйте}! Смотрите", fill=TEXT_WHITE, font=get_font(12))
    draw.text((sx + 220, y0 + 178), "наш {новый|свежий} проект!", fill=TEXT_WHITE, font=get_font(12))

    draw_checkbox(draw, sx + 20, y0 + 235, "Dry Run", checked=False)

    draw_button(draw, sx + 20, y0 + 275, 180, 34, "Начать упоминания")

    draw_log_area(draw, sx + 20, y0 + 330, 730, 130, [
        "Пользователей: 856, батчей: 172, аккаунтов: 2",
        "Режим: LIVE",
        "",
        "[+] Батч 1/172 отправлен через +79001234567",
        "[+] Батч 2/172 отправлен через +79001234567",
        "[~] Ротация с +79001234567",
        "[+] Батч 3/172 отправлен через +79007654321",
    ])

    img.save(os.path.join(MOCKUPS_DIR, "05_broadcast.png"))


# ---- Мокап 6: Настройки ----
def mockup_settings():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "settings", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Настройки", fill=TEXT_WHITE, font=get_font(20, True))

    y0 = 60
    font = get_font(13)
    fields = [
        ("Задержка мин (сек):", "15.0"),
        ("Задержка макс (сек):", "30.0"),
        ("Flood Wait порог (сек):", "120"),
        ("Макс сообщений/сессию:", "50"),
        ("Dry Run (true/false):", "false"),
        ("API ID по умолчанию:", "12345678"),
        ("API Hash по умолчанию:", "a1b2c3d4e5f6a7b8c9d0e1f2"),
        ("OpenAI API Key:", "sk-proj-xxxxxxxxxxxxx"),
        ("OpenAI Model:", "gpt-4o-mini"),
    ]

    for i, (label, val) in enumerate(fields):
        yy = y0 + i * 38
        draw.text((sx + 20, yy + 5), label, fill=TEXT_WHITE, font=font)
        draw_input_field(draw, sx + 250, yy, 300, 30, text=val)

    # Read-only
    ro_y = y0 + len(fields) * 38 + 10
    draw.text((sx + 20, ro_y + 5), "Путь к БД:", fill=TEXT_DIM, font=font)
    draw_rounded_rect(draw, (sx + 250, ro_y, sx + 550, ro_y + 30), 6, (40, 40, 43), BORDER)
    draw.text((sx + 260, ro_y + 6), "data/teleton.db", fill=TEXT_DIM, font=get_font(12))

    draw_button(draw, sx + 20, ro_y + 50, 120, 34, "Сохранить", GREEN)
    draw.text((sx + 155, ro_y + 58), "Сохранено!", fill=GREEN, font=get_font(13))

    img.save(os.path.join(MOCKUPS_DIR, "06_settings.png"))


# ---- Мокап 7: Статистика ----
def mockup_stats():
    img = Image.new("RGB", (960, 600), BG)
    draw = ImageDraw.Draw(img)
    draw_sidebar(draw, "stats", h=600)
    sx = 200

    draw.text((sx + 20, 15), "Статистика", fill=TEXT_WHITE, font=get_font(20, True))

    # Форма
    draw.text((sx + 20, 60), "Дней:", fill=TEXT_WHITE, font=get_font(13))
    draw_input_field(draw, sx + 80, 57, 80, 30, text="7")
    draw_button(draw, sx + 175, 57, 100, 30, "Обновить")

    # Карточки
    cards = [
        ("Всего", "247", ACCENT),
        ("Отправлено", "198", GREEN),
        ("Ошибки", "12", RED),
        ("Flood Wait", "31", ORANGE),
        ("Бан", "4", (155, 89, 182)),
        ("Нет доступа", "2", TEXT_GRAY),
    ]

    for i, (label, value, color) in enumerate(cards):
        col = i % 3
        row = i // 3
        cx = sx + 20 + col * 245
        cy = 110 + row * 120
        draw_rounded_rect(draw, (cx, cy, cx + 225, cy + 100), 10, CARD_BG)
        draw.text((cx + 20, cy + 15), label, fill=color, font=get_font(14))
        draw.text((cx + 20, cy + 42), value, fill=color, font=get_font(36, True))

    img.save(os.path.join(MOCKUPS_DIR, "07_stats.png"))


# ---- Мокап 8: Диалог авторизации ----
def mockup_auth_dialog():
    img = Image.new("RGB", (400, 220), (45, 45, 50))
    draw = ImageDraw.Draw(img)
    draw_rounded_rect(draw, (0, 0, 399, 219), 12, (45, 45, 50), BORDER)

    draw.text((20, 15), "Код авторизации — +79001234567", fill=TEXT_WHITE, font=get_font(14, True))
    draw.text((20, 50), "Телефон: +79001234567", fill=TEXT_WHITE, font=get_font(14))
    draw.text((20, 80), "Введите код авторизации:", fill=TEXT_GRAY, font=get_font(13))
    draw_input_field(draw, 20, 105, 360, 34, text="12345")

    draw_button(draw, 20, 160, 170, 34, "Подтвердить", ACCENT)
    draw_button(draw, 210, 160, 170, 34, "Отмена", (100, 100, 105))

    img.save(os.path.join(MOCKUPS_DIR, "08_auth_dialog.png"))


# ---- Мокап 9: CLI help ----
def mockup_cli():
    img = Image.new("RGB", (750, 380), (12, 12, 12))
    draw = ImageDraw.Draw(img)
    font = get_font(13)

    lines = [
        ("$ python main.py --help", (100, 200, 100)),
        ("", None),
        ("usage: teleton [-h]", TEXT_GRAY),
        ("  {smart-parse,parse,mention,broadcast,add-account,add-task,stats,auth}", TEXT_GRAY),
        ("", None),
        ("Telegram рассылка и упоминания через несколько аккаунтов", TEXT_WHITE),
        ("", None),
        ("positional arguments:", ORANGE),
        ("  smart-parse   Смарт-парсинг по содержимому постов", TEXT_WHITE),
        ("  parse         Парсинг участников группы", TEXT_WHITE),
        ("  mention       Массовые упоминания", TEXT_WHITE),
        ("  broadcast     Рассылка из задач в БД", TEXT_WHITE),
        ("  add-account   Добавить аккаунт", TEXT_WHITE),
        ("  add-task      Добавить задачу", TEXT_WHITE),
        ("  stats         Статистика отправок", TEXT_WHITE),
        ("  auth          Авторизация аккаунта", TEXT_WHITE),
        ("", None),
        ("$ python main.py smart-parse --group @chat --mode keywords \\", (100, 200, 100)),
        ('    --keywords "трафик,твиттер,reddit" --limit 500', (100, 200, 100)),
    ]

    y = 10
    for text, color in lines:
        if text and color:
            draw.text((15, y), text, fill=color, font=font)
        y += 19

    img.save(os.path.join(MOCKUPS_DIR, "09_cli.png"))


# ============================================================
# Часть 2: Генерация PDF
# ============================================================

class TeletonPDF(FPDF):
    def __init__(self):
        super().__init__()
        # Шрифты с поддержкой кириллицы
        self.add_font("Segoe", "", os.path.join(FONT_DIR, "segoeui.ttf"))
        self.add_font("Segoe", "B", os.path.join(FONT_DIR, "segoeuib.ttf"))
        self.add_font("Segoe", "I", os.path.join(FONT_DIR, "segoeuii.ttf"))
        self.add_font("Segoe", "BI", os.path.join(FONT_DIR, "segoeuiz.ttf"))
        # Моноширинный
        self.add_font("Consolas", "", os.path.join(FONT_DIR, "consola.ttf"))
        self.add_font("Consolas", "B", os.path.join(FONT_DIR, "consolab.ttf"))

    def header(self):
        if self.page_no() > 1:
            self.set_font("Segoe", "I", 9)
            self.set_text_color(130, 130, 130)
            self.cell(0, 8, "Teleton — Руководство пользователя", align="L")
            self.cell(0, 8, f"Стр. {self.page_no()}", align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(200, 200, 200)
            self.line(10, 18, 200, 18)
            self.ln(4)

    def footer(self):
        pass

    def chapter_title(self, num, title):
        self.set_font("Segoe", "B", 18)
        self.set_text_color(52, 131, 235)
        self.cell(0, 12, f"{num}. {title}", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(52, 131, 235)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(6)
        self.set_text_color(30, 30, 30)

    def section_title(self, title):
        self.set_font("Segoe", "B", 14)
        self.set_text_color(60, 60, 60)
        self.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_text_color(30, 30, 30)

    def body_text(self, text):
        self.set_font("Segoe", "", 11)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 6, text)
        self.ln(3)

    def code_block(self, code):
        self.set_font("Consolas", "", 9)
        self.set_fill_color(240, 240, 245)
        self.set_text_color(30, 30, 30)
        # Рамка
        x = self.get_x()
        y = self.get_y()
        lines = code.strip().split("\n")
        block_h = len(lines) * 5 + 6

        if y + block_h > 270:
            self.add_page()
            y = self.get_y()

        self.set_draw_color(200, 200, 210)
        self.rect(x, y, 190, block_h, style="DF")
        self.ln(3)
        for line in lines:
            self.cell(0, 5, "  " + line, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)
        self.set_text_color(40, 40, 40)

    def bullet(self, text, indent=0):
        self.set_font("Segoe", "", 11)
        x = 15 + indent
        self.set_x(x)
        self.cell(5, 6, "•")
        self.multi_cell(190 - indent - 5, 6, text)
        self.ln(1)

    def numbered_step(self, num, text):
        self.set_font("Segoe", "B", 11)
        self.set_text_color(52, 131, 235)
        self.cell(8, 7, f"{num}.")
        self.set_font("Segoe", "", 11)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 7, text)
        self.ln(2)

    def info_box(self, text, color="blue"):
        colors = {
            "blue": ((230, 240, 255), (52, 131, 235)),
            "green": ((230, 255, 240), (47, 165, 114)),
            "orange": ((255, 245, 230), (243, 156, 18)),
            "red": ((255, 235, 235), (200, 60, 50)),
        }
        bg, border_c = colors.get(color, colors["blue"])
        self.set_fill_color(*bg)
        self.set_draw_color(*border_c)

        y = self.get_y()
        lines = text.split("\n")
        h = len(lines) * 6 + 8
        if y + h > 270:
            self.add_page()
        self.rect(10, self.get_y(), 190, h, style="DF")
        self.ln(4)
        self.set_font("Segoe", "", 10)
        self.set_text_color(*border_c)
        for line in lines:
            self.cell(0, 6, "  " + line, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)
        self.set_text_color(40, 40, 40)

    def add_mockup(self, filename, caption=""):
        path = os.path.join(MOCKUPS_DIR, filename)
        if not os.path.exists(path):
            return

        # Проверяем, нужен ли перенос страницы
        if self.get_y() > 160:
            self.add_page()

        img_w = 180
        self.image(path, x=15, w=img_w)
        if caption:
            self.set_font("Segoe", "I", 9)
            self.set_text_color(100, 100, 100)
            self.cell(0, 6, caption, align="C", new_x="LMARGIN", new_y="NEXT")
            self.ln(4)
            self.set_text_color(40, 40, 40)


def generate_pdf():
    pdf = TeletonPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ====== Обложка ======
    pdf.add_page()
    pdf.ln(40)
    pdf.set_font("Segoe", "B", 36)
    pdf.set_text_color(52, 131, 235)
    pdf.cell(0, 20, "TELETON", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Segoe", "", 16)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 10, "Руководство пользователя", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_font("Segoe", "", 12)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 8, "Рассылка сообщений и массовые упоминания", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, "в Telegram-группах через несколько аккаунтов", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_draw_color(52, 131, 235)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(10)

    pdf.set_font("Segoe", "", 11)
    pdf.set_text_color(120, 120, 120)
    features = [
        "Парсинг участников групп и каналов",
        "Смарт-парсинг по ключевым словам и ИИ (OpenAI)",
        "Массовые упоминания с ротацией аккаунтов",
        "Рассылка со Spintax-поддержкой",
        "GUI-интерфейс и CLI-команды",
        "SQLite хранилище + автоматическая ротация",
    ]
    for f in features:
        pdf.cell(0, 7, f"•  {f}", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(30)
    pdf.set_font("Segoe", "I", 10)
    pdf.set_text_color(160, 160, 160)
    pdf.cell(0, 6, "Версия 2.0  |  Март 2026", align="C", new_x="LMARGIN", new_y="NEXT")

    # ====== Оглавление ======
    pdf.add_page()
    pdf.set_font("Segoe", "B", 20)
    pdf.set_text_color(52, 131, 235)
    pdf.cell(0, 12, "Оглавление", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    toc = [
        ("1", "Системные требования"),
        ("2", "Установка и первый запуск"),
        ("3", "Добавление аккаунтов"),
        ("4", "Авторизация"),
        ("5", "Обычный парсинг"),
        ("6", "Смарт-парсинг (ключевые слова)"),
        ("7", "Смарт-парсинг (ИИ / OpenAI)"),
        ("8", "Массовые упоминания"),
        ("9", "Рассылка (Broadcast)"),
        ("10", "Статистика"),
        ("11", "Настройки"),
        ("12", "CLI-команды (справочник)"),
        ("13", "Решение проблем"),
    ]

    for num, title in toc:
        pdf.set_font("Segoe", "", 12)
        pdf.set_text_color(40, 40, 40)
        prefix = "    " if len(num) == 1 else "  "
        pdf.cell(0, 8, f"{prefix}{num}.  {title}", new_x="LMARGIN", new_y="NEXT")

    # ====== 1. Системные требования ======
    pdf.add_page()
    pdf.chapter_title("1", "Системные требования")
    pdf.body_text("Для работы Teleton необходимо:")
    pdf.bullet("Python 3.9 или выше")
    pdf.bullet("Операционная система: Windows 10/11, macOS, Linux")
    pdf.bullet("Telegram аккаунт(ы) с привязанным номером телефона")
    pdf.bullet("API ID и API Hash от Telegram (получить на my.telegram.org)")
    pdf.bullet("(Опционально) OpenAI API Key — для режима ИИ-парсинга")
    pdf.bullet("(Опционально) SOCKS5 прокси — для работы через прокси")
    pdf.ln(4)

    pdf.section_title("Зависимости Python")
    pdf.code_block(
        "telethon>=1.36.0\n"
        "python-dotenv>=1.0.0\n"
        "python-socks[asyncio]>=2.4.0\n"
        "customtkinter>=5.2.0\n"
        "openai>=1.0.0"
    )

    # ====== 2. Установка ======
    pdf.add_page()
    pdf.chapter_title("2", "Установка и первый запуск")

    pdf.section_title("Шаг 1: Клонирование / скачивание")
    pdf.body_text("Скачайте проект или клонируйте репозиторий в удобную папку.")

    pdf.section_title("Шаг 2: Установка зависимостей")
    pdf.code_block("pip install -r requirements.txt")

    pdf.section_title("Шаг 3: Создание файла .env")
    pdf.body_text("Скопируйте шаблон и заполните реальными значениями:")
    pdf.code_block(
        "cp .env.example .env\n"
        "\n"
        "# Содержимое .env:\n"
        "DEFAULT_API_ID=12345678\n"
        "DEFAULT_API_HASH=your_api_hash_here\n"
        "DELAY_MIN=15.0\n"
        "DELAY_MAX=30.0\n"
        "FLOOD_WAIT_THRESHOLD=120\n"
        "MAX_MESSAGES_PER_SESSION=50\n"
        "DRY_RUN=false"
    )

    pdf.info_box(
        "Совет: API ID и API Hash можно получить на https://my.telegram.org\n"
        "  1. Войдите с номером телефона\n"
        "  2. Перейдите в API Development Tools\n"
        "  3. Создайте приложение и скопируйте ID и Hash",
        "blue",
    )

    pdf.section_title("Шаг 4: Запуск")
    pdf.body_text("GUI-интерфейс:")
    pdf.code_block("python gui.py\n# или двойной клик по run_gui.bat")
    pdf.body_text("CLI-режим:")
    pdf.code_block("python main.py --help")

    pdf.add_mockup("09_cli.png", "Рис. 1 — CLI-интерфейс: список всех команд")

    # ====== 3. Добавление аккаунтов ======
    pdf.add_page()
    pdf.chapter_title("3", "Добавление аккаунтов")

    pdf.body_text(
        "Перед началом работы необходимо добавить хотя бы один Telegram-аккаунт. "
        "Это можно сделать через GUI или CLI."
    )

    pdf.section_title("Через GUI")
    pdf.numbered_step(1, 'Откройте раздел "Аккаунты" в боковом меню')
    pdf.numbered_step(2, 'Нажмите кнопку "Добавить"')
    pdf.numbered_step(3, "Заполните поля: телефон, API ID, API Hash, прокси (опционально)")
    pdf.numbered_step(4, 'Нажмите "Добавить" в диалоговом окне')

    pdf.add_mockup("01_accounts.png", "Рис. 2 — Раздел управления аккаунтами")

    pdf.section_title("Через CLI")
    pdf.code_block(
        "# С указанием API ID/Hash:\n"
        "python main.py add-account --phone +79001234567 \\\n"
        "    --api-id 12345678 --api-hash your_hash\n"
        "\n"
        "# С прокси:\n"
        "python main.py add-account --phone +79001234567 \\\n"
        "    --proxy socks5://user:pass@host:port"
    )

    pdf.info_box(
        "Если DEFAULT_API_ID и DEFAULT_API_HASH заданы в .env, их можно не указывать\n"
        "при добавлении — будут использованы значения по умолчанию.",
        "green",
    )

    pdf.section_title("Управление аккаунтами")
    pdf.bullet("Вкл/Выкл — временно деактивировать аккаунт (не будет использоваться при рассылке)")
    pdf.bullet("Удалить — полностью удалить аккаунт из базы данных")
    pdf.bullet('В столбце "Отправлено" — количество сообщений за сегодня (сбрасывается автоматически)')

    # ====== 4. Авторизация ======
    pdf.add_page()
    pdf.chapter_title("4", "Авторизация")

    pdf.body_text(
        "После добавления аккаунта его необходимо авторизовать в Telegram. "
        "Авторизация выполняется один раз — сессия сохраняется в папке data/sessions/."
    )

    pdf.section_title("Через GUI")
    pdf.numbered_step(1, 'В разделе "Аккаунты" выберите аккаунт в таблице (кликните на строку)')
    pdf.numbered_step(2, 'Нажмите "Авторизация"')
    pdf.numbered_step(3, "Дождитесь SMS/звонка с кодом от Telegram")
    pdf.numbered_step(4, "Введите полученный код в появившемся окне")
    pdf.numbered_step(5, "Если включена 2FA — введите пароль двухфакторной аутентификации")

    pdf.add_mockup("08_auth_dialog.png", "Рис. 3 — Окно ввода кода авторизации")

    pdf.section_title("Через CLI")
    pdf.code_block("python main.py auth --phone +79001234567")
    pdf.body_text("Следуйте интерактивным подсказкам в терминале для ввода кода и 2FA-пароля.")

    pdf.info_box(
        "Важно: Не делитесь файлами сессий (data/sessions/) с третьими лицами!\n"
        "Файл сессии даёт полный доступ к Telegram-аккаунту.",
        "red",
    )

    # ====== 5. Обычный парсинг ======
    pdf.add_page()
    pdf.chapter_title("5", "Обычный парсинг")

    pdf.body_text(
        "Парсинг собирает участников группы или комментаторов канала "
        "и сохраняет их в базу данных для последующего использования (упоминания, рассылка)."
    )

    pdf.section_title("Режимы парсинга")
    pdf.bullet("Обычный — получает до 200 участников группы (ограничение Telegram API)")
    pdf.bullet('Aggressive — поиск по каждой букве алфавита, позволяет обойти лимит 200 и получить значительно больше участников')
    pdf.bullet("Комментаторы — собирает пользователей из комментариев к постам канала")

    pdf.section_title("Через GUI")
    pdf.numbered_step(1, 'Откройте раздел "Парсинг" → вкладка "Обычный парсинг"')
    pdf.numbered_step(2, "Введите username группы (@group_name)")
    pdf.numbered_step(3, "Выберите аккаунт из списка")
    pdf.numbered_step(4, "Отметьте нужные опции (Aggressive, Комментаторы)")
    pdf.numbered_step(5, 'Нажмите "Начать парсинг"')

    pdf.add_mockup("02_parsing_regular.png", "Рис. 4 — Вкладка обычного парсинга")

    pdf.section_title("Через CLI")
    pdf.code_block(
        "# Обычный парсинг:\n"
        "python main.py parse --group @crypto_chat\n"
        "\n"
        "# Aggressive-парсинг:\n"
        "python main.py parse --group @crypto_chat --aggressive\n"
        "\n"
        "# Парсинг комментаторов:\n"
        "python main.py parse --group @channel_name --commenters --limit-posts 100\n"
        "\n"
        "# С конкретным аккаунтом:\n"
        "python main.py parse --group @crypto_chat --account +79001234567"
    )

    # ====== 6. Смарт-парсинг (keywords) ======
    pdf.add_page()
    pdf.chapter_title("6", "Смарт-парсинг (ключевые слова)")

    pdf.body_text(
        "Смарт-парсинг — новый режим, который анализирует содержимое сообщений в группе "
        "и находит авторов по заданным критериям. В режиме ключевых слов система ищет точные "
        "вхождения указанных слов в тексте сообщений."
    )

    pdf.section_title("Как это работает")
    pdf.numbered_step(1, "Система читает N последних сообщений в указанной группе")
    pdf.numbered_step(2, "Каждое сообщение проверяется на наличие ключевых слов (без учёта регистра)")
    pdf.numbered_step(3, "Авторы совпавших сообщений сохраняются в parsed_users")
    pdf.numbered_step(4, "Тексты постов сохраняются в matched_posts с указанием сработавших слов")

    pdf.section_title("Через GUI")
    pdf.numbered_step(1, 'Откройте раздел "Парсинг" → вкладка "Смарт-парсинг"')
    pdf.numbered_step(2, "Введите группу и выберите аккаунт")
    pdf.numbered_step(3, 'Убедитесь, что режим установлен на "Ключевые слова"')
    pdf.numbered_step(4, 'Введите слова через запятую: "трафик, твиттер, reddit, автоматизация"')
    pdf.numbered_step(5, "Укажите лимит сообщений (по умолчанию 500)")
    pdf.numbered_step(6, 'Нажмите "Начать смарт-парсинг"')

    pdf.add_mockup("03_smart_keywords.png", "Рис. 5 — Смарт-парсинг: режим ключевых слов")

    pdf.section_title("Через CLI")
    pdf.code_block(
        'python main.py smart-parse --group @traffic_chat \\\n'
        '    --mode keywords \\\n'
        '    --keywords "трафик,твиттер,reddit,автоматизация" \\\n'
        '    --limit 500'
    )

    pdf.info_box(
        "Совет: Начните с лимита 100-200 сообщений для тестирования,\n"
        "затем увеличивайте до 500-1000 для полного парсинга.",
        "green",
    )

    # ====== 7. Смарт-парсинг (AI) ======
    pdf.add_page()
    pdf.chapter_title("7", "Смарт-парсинг (ИИ / OpenAI)")

    pdf.body_text(
        "Режим ИИ использует OpenAI GPT для анализа содержимого постов. "
        "Вместо точных ключевых слов вы описываете критерий на естественном языке, "
        "а нейросеть определяет, подходит ли каждый пост."
    )

    pdf.section_title("Преимущества режима ИИ")
    pdf.bullet("Понимает контекст и смысл, а не только точные совпадения")
    pdf.bullet("Работает с перифразами и синонимами")
    pdf.bullet('Можно задать сложный критерий: "ищет трафик с соцсетей или предлагает автоматизацию"')

    pdf.section_title("Настройка OpenAI")
    pdf.numbered_step(1, 'Откройте раздел "Настройки" в боковом меню')
    pdf.numbered_step(2, 'В поле "OpenAI API Key" введите ваш ключ (sk-proj-...)')
    pdf.numbered_step(3, 'В поле "OpenAI Model" укажите модель (по умолчанию gpt-4o-mini)')
    pdf.numbered_step(4, 'Нажмите "Сохранить"')

    pdf.info_box(
        "Получить OpenAI API Key: https://platform.openai.com/api-keys\n"
        "Рекомендуемая модель: gpt-4o-mini (быстрая, дешёвая, достаточно точная)\n"
        "Для максимальной точности: gpt-4o",
        "blue",
    )

    pdf.section_title("Использование через GUI")
    pdf.numbered_step(1, 'Перейдите в "Парсинг" → "Смарт-парсинг"')
    pdf.numbered_step(2, 'Переключите режим на "ИИ"')
    pdf.numbered_step(3, 'В поле "Критерий ИИ" опишите что искать на естественном языке')
    pdf.numbered_step(4, "Установите лимит (рекомендуется 100-200 для экономии токенов)")
    pdf.numbered_step(5, 'Нажмите "Начать смарт-парсинг"')

    pdf.add_mockup("04_smart_ai.png", "Рис. 6 — Смарт-парсинг: режим ИИ")

    pdf.section_title("Через CLI")
    pdf.code_block(
        'python main.py smart-parse --group @traffic_chat \\\n'
        '    --mode ai \\\n'
        '    --criteria "ищет трафик с твиттера или софт для автоматизации реддита" \\\n'
        '    --limit 200'
    )

    pdf.info_box(
        "Стоимость: Посты обрабатываются батчами по 10 штук для экономии токенов.\n"
        "~500 сообщений через gpt-4o-mini обойдутся примерно в $0.01-0.05.",
        "orange",
    )

    # ====== 8. Упоминания ======
    pdf.add_page()
    pdf.chapter_title("8", "Массовые упоминания")

    pdf.body_text(
        "Упоминания отправляют сообщения в целевую группу с inline-тегами пользователей. "
        "Используются спарсенные пользователи из базы данных. "
        "Система автоматически ротирует аккаунты при Flood Wait."
    )

    pdf.section_title("Через GUI")
    pdf.numbered_step(1, 'Откройте раздел "Рассылка" → вкладка "Упоминания"')
    pdf.numbered_step(2, "Укажите целевую группу, источник пользователей, текст сообщения")
    pdf.numbered_step(3, 'Поддерживается Spintax: {Привет|Здравствуйте}')
    pdf.numbered_step(4, 'Включите "Dry Run" для тестовой отправки (без реальной отправки)')
    pdf.numbered_step(5, 'Нажмите "Начать упоминания"')

    pdf.add_mockup("05_broadcast.png", "Рис. 7 — Рассылка: вкладка упоминаний")

    pdf.section_title("Через CLI")
    pdf.code_block(
        'python main.py mention --target @target_group \\\n'
        '    --source @crypto_chat \\\n'
        '    --message "{Привет|Здравствуйте}! Посмотрите наш проект" \\\n'
        '    --mentions-per-message 5 --limit 100'
    )

    pdf.section_title("Параметры")
    pdf.bullet("Целевая группа — куда отправлять сообщения с упоминаниями")
    pdf.bullet("Источник — group_source из parsed_users (откуда взяты пользователи)")
    pdf.bullet("Упоминаний/сообщение — сколько пользователей тегать в одном сообщении (рекомендация: 3-5)")
    pdf.bullet("Лимит — максимальное количество пользователей (0 = без лимита)")

    pdf.info_box(
        "Spintax: текст в фигурных скобках рандомизируется.\n"
        "  {Привет|Здравствуйте|Добрый день} → случайный вариант при каждой отправке.\n"
        "  Вложенный: {Смотрите {наш|этот} {проект|продукт}}",
        "blue",
    )

    # ====== 9. Рассылка ======
    pdf.add_page()
    pdf.chapter_title("9", "Рассылка (Broadcast)")

    pdf.body_text(
        "Рассылка отправляет сообщения из задач в базе данных. "
        "Задачи создаются заранее, и система последовательно выполняет их "
        "через доступные аккаунты."
    )

    pdf.section_title("Создание задач")
    pdf.body_text("Через GUI:")
    pdf.numbered_step(1, 'Откройте раздел "Задачи"')
    pdf.numbered_step(2, 'Нажмите "Добавить"')
    pdf.numbered_step(3, "Укажите целевую группу, тип (broadcast/mention), текст сообщения")

    pdf.body_text("Через CLI:")
    pdf.code_block(
        'python main.py add-task --target @target_group \\\n'
        '    --message "{Привет|Здравствуйте}! Текст рассылки"'
    )

    pdf.section_title("Запуск рассылки")
    pdf.body_text("Через GUI:")
    pdf.numbered_step(1, 'Перейдите в "Рассылка" → вкладка "Рассылка"')
    pdf.numbered_step(2, 'Опционально включите "Dry Run" для тестирования')
    pdf.numbered_step(3, 'Нажмите "Начать рассылку"')

    pdf.body_text("Через CLI:")
    pdf.code_block(
        "# Реальная рассылка:\n"
        "python main.py broadcast\n"
        "\n"
        "# Тестовый режим:\n"
        "python main.py broadcast --dry-run"
    )

    # ====== 10. Статистика ======
    pdf.add_page()
    pdf.chapter_title("10", "Статистика")

    pdf.body_text(
        "Раздел статистики показывает агрегированные данные об отправках за выбранный период."
    )

    pdf.add_mockup("07_stats.png", "Рис. 8 — Раздел статистики")

    pdf.section_title("Метрики")
    pdf.bullet("Всего — общее количество попыток отправки")
    pdf.bullet("Отправлено — успешно доставленные сообщения")
    pdf.bullet("Ошибки — сообщения с неизвестными ошибками")
    pdf.bullet("Flood Wait — временные ограничения от Telegram (аккаунт был ротирован)")
    pdf.bullet("Бан — аккаунт деактивирован из-за PeerFloodError")
    pdf.bullet("Нет доступа — целевая группа недоступна или приватна")

    pdf.section_title("Через CLI")
    pdf.code_block(
        "# Статистика за 7 дней (по умолчанию):\n"
        "python main.py stats\n"
        "\n"
        "# За 30 дней:\n"
        "python main.py stats --days 30"
    )

    # ====== 11. Настройки ======
    pdf.add_page()
    pdf.chapter_title("11", "Настройки")

    pdf.body_text(
        "Все настройки можно изменить через GUI-интерфейс. "
        "Значения сохраняются в файл .env и применяются при следующем запуске."
    )

    pdf.add_mockup("06_settings.png", "Рис. 9 — Раздел настроек")

    pdf.section_title("Описание параметров")
    pdf.bullet("Задержка мин/макс (сек) — случайная пауза между отправками (защита от бана)")
    pdf.bullet("Flood Wait порог — максимальное время ожидания при flood_wait (при превышении — ротация)")
    pdf.bullet("Макс сообщений/сессию — лимит отправок за одну сессию на аккаунт")
    pdf.bullet("Dry Run — если true, сообщения не отправляются (только логирование)")
    pdf.bullet("API ID / Hash — значения по умолчанию для новых аккаунтов")
    pdf.bullet("OpenAI API Key — ключ для режима ИИ-парсинга")
    pdf.bullet("OpenAI Model — модель GPT (gpt-4o-mini, gpt-4o и др.)")

    pdf.info_box(
        "Рекомендуемые задержки для безопасной работы:\n"
        "  DELAY_MIN=15, DELAY_MAX=30 — стандартный режим\n"
        "  DELAY_MIN=30, DELAY_MAX=60 — осторожный режим\n"
        "  MAX_MESSAGES_PER_SESSION=30-50 — безопасный лимит",
        "green",
    )

    # ====== 12. CLI справочник ======
    pdf.add_page()
    pdf.chapter_title("12", "CLI-команды (справочник)")

    commands = [
        ("smart-parse", "Смарт-парсинг по содержимому постов",
         'python main.py smart-parse --group @chat --mode keywords \\\n'
         '    --keywords "трафик,софт" --limit 500'),
        ("parse", "Парсинг участников группы",
         "python main.py parse --group @chat --aggressive"),
        ("mention", "Массовые упоминания",
         'python main.py mention --target @group --source @chat \\\n'
         '    --message "Привет!" --mentions-per-message 5'),
        ("broadcast", "Рассылка из задач",
         "python main.py broadcast --dry-run"),
        ("add-account", "Добавить аккаунт",
         "python main.py add-account --phone +79001234567 \\\n"
         "    --api-id 123456 --api-hash abc123"),
        ("add-task", "Добавить задачу",
         'python main.py add-task --target @group --message "Текст"'),
        ("stats", "Статистика",
         "python main.py stats --days 30"),
        ("auth", "Авторизация аккаунта",
         "python main.py auth --phone +79001234567"),
    ]

    for cmd, desc, example in commands:
        pdf.set_font("Segoe", "B", 12)
        pdf.set_text_color(52, 131, 235)
        pdf.cell(0, 8, cmd, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Segoe", "", 11)
        pdf.set_text_color(40, 40, 40)
        pdf.cell(0, 6, desc, new_x="LMARGIN", new_y="NEXT")
        pdf.code_block(example)
        pdf.ln(1)

    # ====== 13. Решение проблем ======
    pdf.add_page()
    pdf.chapter_title("13", "Решение проблем")

    problems = [
        (
            "FloodWaitError",
            "Telegram ограничил отправку. Система автоматически ротирует на следующий аккаунт. "
            "Рекомендация: увеличьте задержку (DELAY_MIN/MAX) и добавьте больше аккаунтов.",
        ),
        (
            "PeerFloodError / аккаунт деактивирован",
            "Telegram заблокировал аккаунт за спам. Аккаунт автоматически деактивируется в базе. "
            "Подождите 24-48 часов перед повторной активацией.",
        ),
        (
            "ChatWriteForbiddenError / Нет доступа",
            "У аккаунта нет прав писать в целевую группу. Убедитесь, что аккаунт является "
            "участником группы и не забанен.",
        ),
        (
            "Ошибка авторизации",
            "Убедитесь, что номер телефона указан в международном формате (+79001234567). "
            "Если включена 2FA, введите пароль во втором диалоговом окне.",
        ),
        (
            "OpenAI API ошибка",
            "Проверьте правильность API Key в настройках. Убедитесь, что на аккаунте OpenAI "
            "достаточно средств. Попробуйте модель gpt-4o-mini (дешевле и быстрее).",
        ),
        (
            "Парсинг возвращает 0 пользователей",
            "Группа может быть приватной или аккаунт не является участником. "
            "Для больших групп используйте --aggressive режим.",
        ),
        (
            "SessionPasswordNeededError",
            "Аккаунт защищён двухфакторной аутентификацией. Введите 2FA-пароль "
            "в появившемся диалоговом окне (GUI) или в терминале (CLI).",
        ),
    ]

    for title, solution in problems:
        pdf.set_font("Segoe", "B", 12)
        pdf.set_text_color(200, 60, 50)
        pdf.cell(0, 8, f"  {title}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Segoe", "", 11)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 6, f"    {solution}")
        pdf.ln(4)

    # ====== Финальная страница ======
    pdf.add_page()
    pdf.ln(60)
    pdf.set_font("Segoe", "B", 24)
    pdf.set_text_color(52, 131, 235)
    pdf.cell(0, 15, "Готово!", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Segoe", "", 14)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 8, "Teleton настроен и готов к работе.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Segoe", "", 12)
    pdf.set_text_color(120, 120, 120)

    steps_summary = [
        "1. Установите зависимости (pip install -r requirements.txt)",
        "2. Создайте .env с API ID/Hash",
        "3. Добавьте аккаунт и пройдите авторизацию",
        "4. Спарсьте участников нужных групп",
        "5. Запустите упоминания или рассылку",
    ]
    for step in steps_summary:
        pdf.cell(0, 8, step, align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(20)
    pdf.set_draw_color(52, 131, 235)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(10)
    pdf.set_font("Segoe", "I", 10)
    pdf.set_text_color(160, 160, 160)
    pdf.cell(0, 6, "Teleton v2.0 — Март 2026", align="C", new_x="LMARGIN", new_y="NEXT")

    # Сохранение
    pdf.output(OUTPUT_PDF)
    return OUTPUT_PDF


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("Генерация мокапов интерфейса...")
    mockup_accounts()
    print("  [+] 01_accounts.png")
    mockup_parsing_regular()
    print("  [+] 02_parsing_regular.png")
    mockup_smart_parsing_keywords()
    print("  [+] 03_smart_keywords.png")
    mockup_smart_parsing_ai()
    print("  [+] 04_smart_ai.png")
    mockup_broadcast()
    print("  [+] 05_broadcast.png")
    mockup_settings()
    print("  [+] 06_settings.png")
    mockup_stats()
    print("  [+] 07_stats.png")
    mockup_auth_dialog()
    print("  [+] 08_auth_dialog.png")
    mockup_cli()
    print("  [+] 09_cli.png")

    print("\nГенерация PDF...")
    path = generate_pdf()
    print(f"\n=== PDF создан: {path} ===")
