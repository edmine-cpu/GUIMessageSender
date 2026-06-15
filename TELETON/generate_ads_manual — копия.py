"""
generate_ads_manual.py — генерация PDF-инструкций для модуля "Объявления".

Создаёт два файла:
  Teleton_Ads_Manual.pdf       — полная инструкция по планировщику объявлений
  Teleton_Ads_Subscriptions.pdf — инструкция по автоподпискам
"""

import os
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOCKUPS_DIR = os.path.join(SCRIPT_DIR, "data", "mockups_ads")
os.makedirs(MOCKUPS_DIR, exist_ok=True)

FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# ── Цвета (Dark Theme) ────────────────────────────────────────────────────────
BG         = (30, 30, 30)
SIDEBAR_BG = (43, 43, 43)
CARD_BG    = (50, 50, 55)
INPUT_BG   = (60, 60, 65)
ACCENT     = (52, 131, 235)
GREEN      = (47, 165, 114)
RED        = (200, 60, 50)
ORANGE     = (220, 140, 30)
YELLOW     = (210, 180, 0)
TEXT_WHITE = (230, 230, 230)
TEXT_GRAY  = (160, 160, 160)
TEXT_DIM   = (110, 110, 115)
BORDER     = (70, 70, 75)
SEL_BG     = (40, 80, 140)


def fnt(size=13, bold=False, mono=False):
    path = FONT_MONO if mono else (FONT_BOLD if bold else FONT_REG)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def rrect(draw, xy, r, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=width)


def btn(draw, x, y, w, h, text, color=ACCENT, text_color=(255,255,255)):
    rrect(draw, (x, y, x+w, y+h), 7, color)
    f = fnt(12, bold=True)
    bb = draw.textbbox((0,0), text, font=f)
    tw = bb[2]-bb[0]
    draw.text((x+(w-tw)//2, y+(h-14)//2), text, fill=text_color, font=f)


def inp(draw, x, y, w, h, text="", placeholder="", mono=False):
    rrect(draw, (x, y, x+w, y+h), 5, INPUT_BG, BORDER)
    f = fnt(12, mono=mono)
    if text:
        draw.text((x+8, y+(h-14)//2), text, fill=TEXT_WHITE, font=f)
    elif placeholder:
        draw.text((x+8, y+(h-14)//2), placeholder, fill=TEXT_DIM, font=f)


def label(draw, x, y, text, size=13, bold=False, color=TEXT_WHITE):
    draw.text((x, y), text, fill=color, font=fnt(size, bold))


def sidebar(draw, active, h=620):
    draw.rectangle((0, 0, 185, h), fill=SIDEBAR_BG)
    label(draw, 28, 18, "TELETON", size=18, bold=True)
    items = [
        ("accounts",  "Аккаунты"),
        ("tasks",     "Задачи"),
        ("parsing",   "Парсинг"),
        ("audiences", "Аудитории"),
        ("broadcast", "Рассылка"),
        ("channels",  "Каналы"),
        ("autoreply", "Автоответчик"),
        ("account",   "Аккаунт"),
        ("ads",       "Объявления"),
        ("stats",     "Статистика"),
        ("settings",  "Настройки"),
    ]
    y = 58
    for key, lbl in items:
        if key == active:
            rrect(draw, (8, y, 177, y+34), 7, (60, 100, 170))
        draw.text((20, y+7), lbl, fill=TEXT_WHITE, font=fnt(13))
        y += 40


def tabview(draw, x, y, tabs, active_idx):
    w_each = 120
    total = len(tabs) * w_each
    rrect(draw, (x, y, x+total+8, y+34), 8, (45, 45, 52))
    for i, t in enumerate(tabs):
        tx = x + 4 + i * w_each
        if i == active_idx:
            rrect(draw, (tx, y+3, tx+w_each, y+31), 6, ACCENT)
            label(draw, tx+10, y+8, t, size=12, bold=True)
        else:
            label(draw, tx+10, y+8, t, size=12, color=TEXT_GRAY)


def table_header(draw, x, y, headers, widths):
    cx = x
    for i, h in enumerate(headers):
        label(draw, cx, y, h, size=11, bold=True, color=TEXT_GRAY)
        cx += widths[i]
    draw.line((x, y+18, x+sum(widths), y+18), fill=BORDER, width=1)


def table_row(draw, x, y, vals, widths, selected=False, status_col=None):
    if selected:
        rrect(draw, (x-4, y-2, x+sum(widths)+4, y+18), 4, SEL_BG)
    cx = x
    for i, v in enumerate(vals):
        col = TEXT_WHITE
        if i == status_col:
            if v == "active" or v == "активна":
                col = GREEN
            elif v in ("paused", "пауза"):
                col = ORANGE
            elif v == "banned":
                col = RED
        label(draw, cx, y, str(v), size=11, color=col)
        cx += widths[i]


def log_box(draw, x, y, w, h, lines):
    rrect(draw, (x, y, x+w, y+h), 5, (22, 22, 26), BORDER)
    ly = y + 6
    for line in lines:
        c = GREEN if line.startswith("[+]") else (RED if line.startswith("[-]") else TEXT_GRAY)
        draw.text((x+8, ly), line, fill=c, font=fnt(11, mono=True))
        ly += 16


def callout(draw, x, y, w, text, color=ACCENT):
    lines = text.split("\n")
    h = len(lines) * 18 + 12
    rrect(draw, (x, y, x+w, y+h), 6, (*color, 30), color, width=2)
    for i, l in enumerate(lines):
        draw.text((x+10, y+6+i*18), l, fill=color, font=fnt(12))
    return y + h + 6


def arrow(draw, x1, y1, x2, y2, color=ACCENT):
    draw.line((x1, y1, x2, y2), fill=color, width=2)
    # Стрелка
    import math
    angle = math.atan2(y2-y1, x2-x1)
    size = 8
    for da in (0.5, -0.5):
        ax = x2 - size * math.cos(angle - da)
        ay = y2 - size * math.sin(angle - da)
        draw.line((x2, y2, int(ax), int(ay)), fill=color, width=2)


def save(img, name):
    path = os.path.join(MOCKUPS_DIR, name)
    img.save(path)
    return path


# ═════════════════════════════════════════════════════════════════════════════
# МОКАПЫ
# ═════════════════════════════════════════════════════════════════════════════

def mk_main_screen():
    """Главный экран — вкладка Объявления > Группы"""
    img = Image.new("RGB", (980, 620), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=620)
    sx = 195

    label(d, sx+10, 12, "Объявления", size=18, bold=True)
    tabview(d, sx+10, 42, ["Группы", "Объявления", "Планировщик", "История"], 0)

    # Toolbar
    y = 88
    btn(d, sx+10, y, 105, 30, "Добавить")
    btn(d, sx+122, y, 125, 30, "Редактировать", (70, 70, 80))
    btn(d, sx+254, y, 100, 30, "Подписки", (70, 70, 80))
    btn(d, sx+361, y, 125, 30, "Пауза/Активно", (70, 70, 80))
    btn(d, sx+493, y, 85, 30, "Удалить", RED)
    btn(d, sx+730, y, 100, 30, "↻ Обновить", (55, 55, 65))

    # Таблица групп
    y = 132
    headers = ["Ссылка", "Название", "Интервал", "Часы", "Статус", "Подписан", "Retry до"]
    widths =  [160, 170, 85, 70, 80, 80, 90]
    table_header(d, sx+10, y, headers, widths)

    rows = [
        ("@msk_objavleniya", "МСК Объявления", "60 мин", "08-22", "активна", "member", "—"),
        ("@spb_prodazhi",    "СПБ Продажи",    "90 мин", "09-21", "активна", "member", "—"),
        ("@auto_market",     "Авто Маркет",    "120 мин","00-23", "пауза",   "unknown","—"),
        ("@electronics_msk", "Электроника МСК","60 мин", "08-22", "активна", "member", "—"),
        ("@realty_board",    "Недвижимость",   "180 мин","09-20", "активна", "unknown","23.04 21:53"),
    ]
    for i, row in enumerate(rows):
        table_row(d, sx+10, y+26+i*24, row, widths,
                  selected=(i==0), status_col=4)

    # Лог
    log_box(d, sx+10, 490, 760, 95, [
        "[+] Добавлена группа: @msk_objavleniya (2 подписки)",
        "[+] Опубликовано: 'Продам iPhone 15' → @msk_objavleniya (msg_id=18423)",
        "[~] @realty_board → retry до 23.04.2026 21:53 (SlowModeWait)",
    ])

    return save(img, "01_groups_main.png")


def mk_group_dialog():
    """Диалог добавления группы с секцией подписок"""
    img = Image.new("RGB", (980, 700), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=700)

    # Затемнение фона
    overlay = Image.new("RGBA", (980, 700), (0, 0, 0, 120))
    img.paste(Image.new("RGB", (980, 700), (15, 15, 15)), mask=overlay.split()[3])

    # Диалог
    dx, dy, dw, dh = 230, 30, 500, 640
    rrect(d, (dx, dy, dx+dw, dy+dh), 10, CARD_BG, BORDER, width=1)

    label(d, dx+20, dy+15, "Добавить группу", size=15, bold=True)
    d.line((dx, dy+42, dx+dw, dy+42), fill=BORDER, width=1)

    fields = [
        ("Ссылка / @username:", "@msk_objavleniya"),
        ("Название:", "МСК Объявления"),
        ("Категория / теги:", "продажа, электроника, авто"),
        ("Мин. интервал (мин):", "60"),
        ("Час начала (0-23):", "8"),
        ("Час конца (0-23):", "22"),
    ]
    fy = dy + 54
    for lbl_text, val in fields:
        label(d, dx+15, fy+5, lbl_text, size=12, color=TEXT_GRAY)
        inp(d, dx+210, fy, 270, 28, text=val)
        fy += 38

    # Заметки
    label(d, dx+15, fy+5, "Заметки (правила):", size=12, color=TEXT_GRAY)
    rrect(d, (dx+210, fy, dx+480, fy+50), 5, INPUT_BG, BORDER)
    label(d, dx+220, fy+8, "Только авто и электроника,", size=11, color=TEXT_DIM)
    label(d, dx+220, fy+24, "без услуг", size=11, color=TEXT_DIM)
    fy += 62

    # Статус
    label(d, dx+15, fy+7, "Статус:", size=12, color=TEXT_GRAY)
    rrect(d, (dx+210, fy, dx+350, fy+28), 5, INPUT_BG, BORDER)
    rrect(d, (dx+212, fy+2, dx+278, fy+26), 4, ACCENT)
    label(d, dx+232, fy+6, "активна", size=12, bold=True)
    label(d, dx+292, fy+6, "пауза", size=12, color=TEXT_GRAY)
    fy += 42

    # ── Секция подписок ──────────────────────────────────────────────────────
    d.line((dx+15, fy, dx+dw-15, fy), fill=BORDER, width=1)
    fy += 10

    label(d, dx+15, fy, "Обязательные подписки", size=13, bold=True, color=TEXT_WHITE)
    fy += 20
    label(d, dx+15, fy, "Каналы/группы, в которых нужно состоять перед публикацией",
          size=11, color=TEXT_GRAY)
    fy += 22

    # Список подписок
    sub_bg = (38, 38, 44)
    subs = ["@telegram_ads_official", "@msk_help_channel"]
    for sub in subs:
        rrect(d, (dx+15, fy, dx+dw-15, fy+28), 5, sub_bg, BORDER)
        label(d, dx+25, fy+7, f"📌 {sub}", size=12)
        btn(d, dx+dw-55, fy+4, 30, 20, "✕", RED)
        fy += 34

    # Строка добавления
    inp(d, dx+15, fy, 330, 30, placeholder="@channel или t.me/...")
    btn(d, dx+355, fy, 110, 30, "+ Добавить")
    fy += 44

    # Кнопки
    btn(d, dx+dw-225, dy+dh-50, 100, 36, "Отмена", (80, 80, 90))
    btn(d, dx+dw-115, dy+dh-50, 100, 36, "OK")

    # Стрелка-пояснение
    arrow(d, dx+dw+10, fy-60, dx+dw+80, fy-60)
    callout(d, dx+dw+80, fy-80, 210,
            "Добавляйте каналы\nи группы — бот вступит\nавтоматически перед\nпубликацией", ACCENT)

    return save(img, "02_group_dialog.png")


def mk_ads_tab():
    """Вкладка Объявления"""
    img = Image.new("RGB", (980, 620), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=620)
    sx = 195

    label(d, sx+10, 12, "Объявления", size=18, bold=True)
    tabview(d, sx+10, 42, ["Группы", "Объявления", "Планировщик", "История"], 1)

    y = 88
    btn(d, sx+10, y, 105, 30, "Добавить")
    btn(d, sx+122, y, 125, 30, "Редактировать", (70,70,80))
    btn(d, sx+254, y, 100, 30, "Вкл/Выкл",     (70,70,80))
    btn(d, sx+361, y, 85,  30, "Удалить", RED)
    btn(d, sx+730, y, 100, 30, "↻ Обновить",    (55,55,65))

    y = 132
    headers = ["Название", "Аккаунт", "Категория", "Активно", "Групп", "Медиа"]
    widths  = [200, 140, 150, 80, 65, 60]
    table_header(d, sx+10, y, headers, widths)

    rows = [
        ("Продам iPhone 15 Pro",  "+79991234567", "электроника", "Да", "3", "✓"),
        ("Сдам квартиру ЦАО",     "+79991234567", "недвижимость", "Да", "2", "—"),
        ("Ford Focus 2019",        "+79997654321", "авто",         "Да", "4", "✓"),
        ("MacBook Pro M3",         "+79991234567", "электроника", "Нет","1", "✓"),
        ("Грузоперевозки по МСК",  "+79997654321", "услуги",       "Да", "2", "—"),
    ]
    for i, row in enumerate(rows):
        table_row(d, sx+10, y+26+i*24, row, widths, selected=(i==0))

    log_box(d, sx+10, 490, 760, 95, [
        "[+] Добавлено объявление: Продам iPhone 15 Pro",
        "[~] Ford Focus 2019 → активно",
        "[+] Опубликовано: 'MacBook Pro M3' → @electronics_msk (msg_id=9821)",
    ])

    return save(img, "03_ads_tab.png")


def mk_ad_dialog():
    """Диалог создания объявления с AI"""
    img = Image.new("RGB", (980, 720), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=720)

    overlay = Image.new("RGBA", (980, 720), (0,0,0,120))
    img.paste(Image.new("RGB", (980,720), (15,15,15)), mask=overlay.split()[3])

    dx, dy, dw, dh = 220, 20, 530, 680
    rrect(d, (dx, dy, dx+dw, dy+dh), 10, CARD_BG, BORDER)
    label(d, dx+20, dy+14, "Добавить объявление", size=15, bold=True)
    d.line((dx, dy+42, dx+dw, dy+42), fill=BORDER)

    fy = dy + 55

    label(d, dx+15, fy+5, "Название (внутр.):", size=12, color=TEXT_GRAY)
    inp(d, dx+200, fy, 310, 28, "Продам iPhone 15 Pro")
    fy += 38

    label(d, dx+15, fy+5, "Аккаунт:", size=12, color=TEXT_GRAY)
    rrect(d, (dx+200, fy, dx+510, fy+28), 5, INPUT_BG, BORDER)
    label(d, dx+210, fy+7, "+79991234567  ▾", size=12)
    fy += 38

    label(d, dx+15, fy+5, "Текст объявления:", size=12, color=TEXT_GRAY)
    rrect(d, (dx+200, fy, dx+510, fy+110), 5, INPUT_BG, BORDER)
    lines = [
        "📱 iPhone 15 Pro 256GB, цвет Titan Black",
        "Состояние: 9/10, полный комплект.",
        "Цена: 90 000 руб. Торг уместен.",
        "📍 Москва, м. Курская",
        "Самовывоз или доставка СДЭК.",
        "Пишите в ЛС 👇",
    ]
    for i, l in enumerate(lines):
        label(d, dx+208, fy+6+i*16, l, size=11)
    fy += 118

    # AI кнопка
    btn(d, dx+200, fy, 200, 28, "✨ Сгенерировать AI", (80, 50, 160))
    label(d, dx+412, fy+7, "✅ Текст сгенерирован", size=11, color=GREEN)
    fy += 38

    label(d, dx+15, fy+5, "Медиа (путь):", size=12, color=TEXT_GRAY)
    inp(d, dx+200, fy, 270, 28, "/photos/iphone15.jpg")
    btn(d, dx+478, fy, 32, 28, "📁", (65,65,75))
    fy += 38

    label(d, dx+15, fy+5, "Категория:", size=12, color=TEXT_GRAY)
    inp(d, dx+200, fy, 310, 28, "электроника, смартфон")
    fy += 38

    # Активно
    rrect(d, (dx+200, fy, dx+218, fy+18), 3, ACCENT)
    label(d, dx+202, fy+2, "✓", size=11, bold=True)
    label(d, dx+224, fy+2, "Активно (публиковать)", size=12)
    fy += 34

    # Группы
    label(d, dx+15, fy+5, "Группы:", size=12, color=TEXT_GRAY)
    group_items = [
        ("@msk_objavleniya", True),
        ("@electronics_msk", True),
        ("@spb_prodazhi",    False),
        ("@auto_market",     False),
    ]
    for glink, checked in group_items:
        rrect(d, (dx+200, fy, dx+218, fy+16), 3, ACCENT if checked else INPUT_BG, BORDER)
        if checked:
            label(d, dx+202, fy+1, "✓", size=11, bold=True)
        label(d, dx+224, fy+1, glink, size=12)
        fy += 22

    btn(d, dx+dw-225, dy+dh-46, 100, 36, "Отмена", (80,80,90))
    btn(d, dx+dw-115, dy+dh-46, 100, 36, "OK")

    return save(img, "04_ad_dialog.png")


def mk_scheduler_tab():
    """Вкладка Планировщик"""
    img = Image.new("RGB", (980, 620), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=620)
    sx = 195

    label(d, sx+10, 12, "Объявления", size=18, bold=True)
    tabview(d, sx+10, 42, ["Группы", "Объявления", "Планировщик", "История"], 2)

    # Статус
    y = 90
    label(d, sx+10, y, "▶ Работает", size=16, bold=True, color=GREEN)
    btn(d, sx+180, y-5, 140, 34, "▶ Запустить", (40,130,60))
    btn(d, sx+330, y-5, 140, 34, "⏹ Остановить", RED)

    # Настройки
    y = 145
    label(d, sx+10, y, "Настройки планировщика", size=14, bold=True)
    y += 28

    settings = [
        ("Интервал между публикациями (сек):", "300", 0),
        ("Лимит публикаций в сутки:", "30", 0),
        ("Интервал между вступлениями (сек):", "900", 1),
        ("Лимит вступлений в сутки:", "5", 1),
    ]
    for i, (lbl_text, val, row) in enumerate(settings):
        rx = sx+10 + (row * 380)
        ry = y + (i // 2) * 42
        label(d, rx, ry, lbl_text, size=12, color=TEXT_GRAY)
        inp(d, rx, ry+18, 160, 26, val)

    y += 95

    # AI секция
    label(d, sx+10, y, "AI настройки", size=14, bold=True)
    y += 28
    label(d, sx+10, y+6, "Провайдер:", size=12, color=TEXT_GRAY)
    rrect(d, (sx+130, y, sx+330, y+28), 5, INPUT_BG, BORDER)
    rrect(d, (sx+132, y+2, sx+228, y+26), 4, ACCENT)
    label(d, sx+155, y+6, "openai", size=12, bold=True)
    label(d, sx+245, y+6, "groq", size=12, color=TEXT_GRAY)
    y += 38

    label(d, sx+10, y+6, "OpenAI API Key:", size=12, color=TEXT_GRAY)
    inp(d, sx+130, y, 380, 28, "sk-••••••••••••••••••••••••••••••••")
    y += 38
    label(d, sx+10, y+6, "Groq API Key:", size=12, color=TEXT_GRAY)
    inp(d, sx+130, y, 380, 28, placeholder="gsk_••••••••••••••••")
    y += 48

    btn(d, sx+10, y, 180, 32, "💾 Сохранить настройки")
    y += 50

    log_box(d, sx+10, y, 760, 110, [
        "[+] Планировщик запущен",
        "[~] Публикую объявление 'iPhone 15 Pro' → @msk_objavleniya...",
        "[~] Не состоим в @telegram_ads_official, пробуем вступить...",
        "[+] Вступил в @telegram_ads_official",
        "[+] Опубликовано: 'iPhone 15 Pro' → @msk_objavleniya (msg_id=18424)",
        "[~] Публикую объявление 'iPhone 15 Pro' → @electronics_msk...",
        "[+] Опубликовано: 'iPhone 15 Pro' → @electronics_msk (msg_id=9822)",
    ])

    return save(img, "05_scheduler_tab.png")


def mk_history_tab():
    """Вкладка История"""
    img = Image.new("RGB", (980, 620), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=620)
    sx = 195

    label(d, sx+10, 12, "Объявления", size=18, bold=True)
    tabview(d, sx+10, 42, ["Группы", "Объявления", "Планировщик", "История"], 3)

    y = 88
    label(d, sx+10, y+6, "Статус:", size=12, color=TEXT_GRAY)
    rrect(d, (sx+70, y, sx+210, y+28), 5, INPUT_BG, BORDER)
    label(d, sx+80, y+7, "Все  ▾", size=12)
    btn(d, sx+660, y, 100, 30, "↻ Обновить", (55,55,65))

    y = 132
    headers = ["Время", "Объявление", "Группа", "Аккаунт", "Статус", "Ошибка"]
    widths  = [75, 185, 155, 115, 90, 140]
    table_header(d, sx+10, y, headers, widths)

    rows_data = [
        ("12:34", "iPhone 15 Pro",     "@msk_objavleniya", "+79991234567", "ok",         ""),
        ("12:39", "iPhone 15 Pro",     "@electronics_msk", "+79991234567", "ok",         ""),
        ("13:10", "Сдам квартиру ЦАО", "@realty_board",    "+79991234567", "slow_mode",  "SlowModeWait 1800s"),
        ("13:15", "Ford Focus 2019",   "@auto_market",     "+79997654321", "ok",         ""),
        ("13:45", "Ford Focus 2019",   "@msk_objavleniya", "+79997654321", "ok",         ""),
        ("14:20", "MacBook Pro M3",    "@electronics_msk", "+79991234567", "flood_wait", "FloodWait 3600s"),
        ("15:00", "Грузоперевозки",    "@spb_prodazhi",    "+79997654321", "ok",         ""),
    ]
    status_colors = {
        "ok": GREEN, "slow_mode": ORANGE, "flood_wait": RED,
        "forbidden": RED, "banned": RED, "error": RED,
    }
    for i, row in enumerate(rows_data):
        table_row(d, sx+10, y+26+i*24, row, widths, selected=(i==0))
        # Перекрасим статус
        sc = status_colors.get(row[4], TEXT_WHITE)
        stat_x = sx+10 + sum(widths[:4])
        label(d, stat_x, y+26+i*24, row[4], size=11, color=sc)

    # Сводка
    rrect(d, (sx+10, 510, sx+760, 570), 7, CARD_BG, BORDER)
    label(d, sx+25, 522, "Итого сегодня:", size=12, bold=True)
    label(d, sx+25, 542, "✅ Успешно: 5", size=11, color=GREEN)
    label(d, sx+170, 542, "⚠️ Отложено: 1 (SlowMode)", size=11, color=ORANGE)
    label(d, sx+400, 542, "❌ Ошибок: 1 (FloodWait)", size=11, color=RED)

    return save(img, "06_history_tab.png")


def mk_subs_flow():
    """Схема работы автовступления"""
    img = Image.new("RGB", (980, 680), BG)
    d = ImageDraw.Draw(img)

    label(d, 30, 20, "Схема: автоматическое вступление в обязательные подписки", size=15, bold=True)

    # Блок 1
    def flow_block(x, y, w, h, title, lines, color=ACCENT):
        rrect(d, (x, y, x+w, y+h), 8, (*color, 0), color, width=2)
        label(d, x+12, y+10, title, size=13, bold=True, color=color)
        for i, l in enumerate(lines):
            label(d, x+12, y+32+i*18, l, size=11, color=TEXT_GRAY)

    flow_block(30, 70, 200, 90, "1. Тик планировщика",
               ["Каждые N секунд", "ищет пару", "(объявление, группа)"])

    arrow(d, 233, 115, 280, 115)

    flow_block(283, 70, 200, 90, "2. Проверка подписок",
               ["GetParticipantRequest", "для каждого канала", "из required_subs"])

    # Развилка
    label(d, 500, 110, "?", size=20, bold=True, color=YELLOW)
    arrow(d, 486, 115, 500, 115)

    # Ветка: уже подписан
    arrow(d, 520, 90, 560, 60)
    rrect(d, (560, 40, 760, 80), 6, (*GREEN, 0), GREEN, width=2)
    label(d, 575, 52, "✅ Уже состоит", size=12, bold=True, color=GREEN)
    arrow(d, 760, 60, 810, 115)

    # Ветка: не подписан
    arrow(d, 520, 130, 560, 155)
    rrect(d, (560, 140, 760, 180), 6, (*ORANGE, 0), ORANGE, width=2)
    label(d, 575, 152, "⚠️ Не состоит", size=12, bold=True, color=ORANGE)
    arrow(d, 660, 180, 660, 210)

    flow_block(560, 210, 200, 100, "3. Вступление",
               ["JoinChannelRequest", "Лимит: 5 мин между", "вступлениями,", "макс 15/сутки"],
               ORANGE)

    # Успех/неуспех вступления
    arrow(d, 660, 313, 660, 345)

    # Развилка 2
    label(d, 652, 340, "?", size=16, bold=True, color=YELLOW)

    arrow(d, 620, 358, 560, 380)
    rrect(d, (380, 370, 555, 410), 6, (*RED, 0), RED, width=2)
    label(d, 395, 382, "❌ Не удалось", size=12, bold=True, color=RED)
    arrow(d, 380, 390, 280, 430)
    rrect(d, (80, 420, 275, 460), 6, (*RED, 0), RED, width=2)
    label(d, 95, 432, "Публикация отложена", size=12, color=RED)

    arrow(d, 700, 358, 760, 380)
    rrect(d, (760, 370, 935, 410), 6, (*GREEN, 0), GREEN, width=2)
    label(d, 775, 382, "✅ Вступили!", size=12, bold=True, color=GREEN)
    arrow(d, 847, 410, 847, 440)

    # Публикация
    flow_block(810, 70, 150, 90, "4. Публикация",
               ["send_message()", "или", "send_file()"])
    arrow(d, 810, 115, 763, 115)

    # Итоговый блок
    rrect(d, (700, 440, 935, 490), 8, (*ACCENT, 0), ACCENT, width=2)
    label(d, 715, 452, "📤 Публикация выполнена!", size=13, bold=True, color=ACCENT)
    label(d, 715, 472, "Лог записан в publications_log", size=11, color=TEXT_GRAY)

    # Hard limits блок
    rrect(d, (30, 520, 530, 640), 8, CARD_BG, BORDER)
    label(d, 50, 533, "🔒 Hard Limits (нельзя изменить через UI)", size=13, bold=True, color=YELLOW)
    limits = [
        "• Минимум 5 минут между любыми вступлениями",
        "• Максимум 15 вступлений в сутки",
        "• Минимум 30 сек между любыми публикациями",
        "• Максимум 50 публикаций в сутки с одного аккаунта",
        "• Минимум 30 минут между публикациями в одну группу",
    ]
    for i, l in enumerate(limits):
        label(d, 50, 558+i*16, l, size=11, color=TEXT_GRAY)

    return save(img, "07_subs_flow.png")


def mk_subs_dialog_detail():
    """Детальный вид диалога группы с подписками + пояснения"""
    img = Image.new("RGB", (980, 680), BG)
    d = ImageDraw.Draw(img)
    sidebar(d, "ads", h=680)

    overlay = Image.new("RGBA", (980, 680), (0,0,0,100))
    img.paste(Image.new("RGB", (980,680), (15,15,15)), mask=overlay.split()[3])

    dx, dy, dw = 200, 20, 430
    rrect(d, (dx, dy, dx+dw, dy+620), 10, CARD_BG, BORDER)
    label(d, dx+15, dy+14, "Редактировать группу: @msk_objavleniya", size=13, bold=True)
    d.line((dx, dy+40, dx+dw, dy+40), fill=BORDER)

    # Поля (кратко)
    fields_brief = [
        ("Ссылка:", "@msk_objavleniya"),
        ("Название:", "МСК Объявления"),
        ("Интервал:", "60 мин"),
        ("Часы:", "08 — 22"),
        ("Статус:", "активна"),
    ]
    fy = dy + 52
    for lbl_t, val in fields_brief:
        label(d, dx+15, fy, lbl_t, size=11, color=TEXT_GRAY)
        label(d, dx+130, fy, val, size=11)
        fy += 22

    d.line((dx+15, fy+5, dx+dw-15, fy+5), fill=BORDER)
    fy += 18

    # Секция подписок
    label(d, dx+15, fy, "Обязательные подписки", size=13, bold=True, color=TEXT_WHITE)
    fy += 18
    label(d, dx+15, fy, "Каналы/группы, в которых нужно состоять", size=11, color=TEXT_GRAY)
    label(d, dx+15, fy+14, "перед публикацией:", size=11, color=TEXT_GRAY)
    fy += 34

    subs = [
        ("@telegram_ads_official", True),
        ("@msk_main_channel",      True),
        ("@sellers_community",     False),
    ]
    for link, joined in subs:
        rrect(d, (dx+15, fy, dx+dw-15, fy+30), 5, (38,38,44), BORDER)
        icon = "✅" if joined else "⬜"
        label(d, dx+25, fy+7, f"{icon} {link}", size=12)
        btn(d, dx+dw-50, fy+5, 28, 20, "✕", RED)
        fy += 36

    # Добавить
    inp(d, dx+15, fy, 280, 30, placeholder="@channel или t.me/...")
    btn(d, dx+305, fy, 110, 30, "+ Добавить")
    fy += 50

    btn(d, dx+dw-215, dy+590, 95, 34, "Отмена", (80,80,90))
    btn(d, dx+dw-110, dy+590, 95, 34, "OK")

    # Пояснения справа
    ex = dx + dw + 25
    ey = dy + 165

    callout(d, ex, ey, 250,
            "✅ — бот уже состоит\n   в этом канале/группе\n\n"
            "⬜ — ещё не вступил,\n   вступит перед след.\n   публикацией",
            GREEN)
    ey += 130

    callout(d, ex, ey, 250,
            "Можно указывать:\n• @username канала\n• @username группы\n"
            "• t.me/joinchat/...\n  (приватные каналы)",
            ACCENT)
    ey += 120

    callout(d, ex, ey, 250,
            "После нажатия OK\nподписки сохранятся\n"
            "и планировщик начнёт\nих проверять перед\nкаждой публикацией\nв эту группу",
            ORANGE)

    return save(img, "08_subs_dialog_detail.png")


# ═════════════════════════════════════════════════════════════════════════════
# PDF
# ═════════════════════════════════════════════════════════════════════════════

class PDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("DejaVu",      "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        self.add_font("DejaVu", "B", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        self.add_font("DejaVuMono",  "", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

    def header(self):
        pass

    def h1(self, text):
        self.set_font("DejaVu", "B", 18)
        self.set_text_color(52, 131, 235)
        self.ln(4)
        self.cell(0, 10, text, ln=True)
        self.set_text_color(0)
        self.ln(2)

    def h2(self, text):
        self.set_font("DejaVu", "B", 14)
        self.set_text_color(52, 131, 235)
        self.ln(3)
        self.cell(0, 8, text, ln=True)
        self.set_text_color(0)
        self.ln(1)

    def h3(self, text):
        self.set_font("DejaVu", "B", 12)
        self.set_text_color(80, 80, 80)
        self.cell(0, 7, text, ln=True)
        self.set_text_color(0)

    def body(self, text):
        self.set_font("DejaVu", "", 11)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 6, text)
        self.ln(2)

    def bullet(self, items):
        self.set_font("DejaVu", "", 11)
        self.set_text_color(40, 40, 40)
        for item in items:
            x = self.get_x()
            self.set_x(x + 5)
            self.multi_cell(0, 6, f"• {item}")
            self.set_x(x)
        self.ln(2)

    def note(self, text, color=(52, 131, 235)):
        self.set_fill_color(*color)
        self.set_text_color(255, 255, 255)
        self.set_font("DejaVu", "B", 11)
        self.multi_cell(0, 7, f"  {text}", fill=True)
        self.set_text_color(0)
        self.ln(2)

    def warning(self, text):
        self.note(text, color=(180, 100, 0))

    def mockup(self, path, caption="", w=180):
        if os.path.exists(path):
            self.image(path, x=self.get_x(), y=self.get_y(), w=w)
            self.ln(w * 0.62 + 3)
        if caption:
            self.set_font("DejaVu", "", 9)
            self.set_text_color(120, 120, 120)
            self.cell(0, 5, caption, ln=True, align="C")
            self.set_text_color(0)
        self.ln(3)

    def divider(self):
        self.set_draw_color(200, 200, 200)
        self.line(self.get_x(), self.get_y(), self.get_x()+170, self.get_y())
        self.ln(4)

    def step(self, n, text):
        self.set_font("DejaVu", "B", 12)
        self.set_fill_color(52, 131, 235)
        self.set_text_color(255, 255, 255)
        self.cell(8, 7, str(n), fill=True, align="C")
        self.set_text_color(40, 40, 40)
        self.set_font("DejaVu", "B", 11)
        self.cell(0, 7, f"  {text}", ln=True)
        self.set_text_color(0)
        self.ln(1)

    def code(self, text):
        self.set_font("DejaVuMono", "", 10)
        self.set_fill_color(240, 240, 245)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 6, text, fill=True)
        self.set_text_color(0)
        self.ln(2)


# ─── Основная инструкция ─────────────────────────────────────────────────────

def build_main_manual(paths):
    pdf = PDF()
    pdf.set_margins(15, 15, 15)

    # Обложка
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 28)
    pdf.set_text_color(52, 131, 235)
    pdf.ln(30)
    pdf.cell(0, 14, "TELETON", ln=True, align="C")
    pdf.set_font("DejaVu", "B", 18)
    pdf.cell(0, 10, "Модуль «Объявления»", ln=True, align="C")
    pdf.set_font("DejaVu", "", 13)
    pdf.set_text_color(100, 100, 100)
    pdf.ln(4)
    pdf.cell(0, 8, "Полная инструкция по использованию планировщика объявлений", ln=True, align="C")
    pdf.ln(6)
    pdf.cell(0, 6, "Версия 1.0  •  Апрель 2026", ln=True, align="C")
    pdf.set_text_color(0)
    pdf.ln(20)
    pdf.set_font("DejaVu", "", 11)
    pdf.set_text_color(60, 60, 60)
    intro = (
        "Модуль «Объявления» позволяет автоматически публиковать объявления "
        "в группы Telegram по расписанию. Планировщик работает в фоновом режиме, "
        "соблюдает лимиты антибан-защиты, автоматически вступает в обязательные "
        "каналы и поддерживает генерацию текста через AI (OpenAI GPT-4o / Groq Llama)."
    )
    pdf.multi_cell(0, 7, intro)

    # ── Раздел 1: Обзор ──────────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("1. Обзор функционала")

    pdf.h2("Что умеет планировщик")
    pdf.bullet([
        "Публикация объявлений в несколько групп Telegram по расписанию",
        "Настройка интервала и разрешённых часов для каждой группы отдельно",
        "Автоматическая проверка и выполнение обязательных подписок перед публикацией",
        "Генерация текста объявления через OpenAI GPT-4o или Groq Llama 3.3",
        "Адаптация текста под конкретную группу (тон, длина, стиль)",
        "Публикация с медиафайлом (фото/видео) или без",
        "Обработка всех ошибок Telegram: FloodWait, SlowMode, запрет до даты",
        "Полный журнал публикаций с фильтрами по статусу",
        "Hard limits для защиты аккаунтов от бана",
    ])

    pdf.h2("Структура интерфейса")
    pdf.body(
        "Модуль доступен через пункт «Объявления» в боковом меню Teleton. "
        "Внутри четыре вкладки:"
    )
    pdf.bullet([
        "Группы — управление группами-назначениями (куда публиковать)",
        "Объявления — управление текстами объявлений",
        "Планировщик — запуск/остановка, настройка параметров",
        "История — журнал всех публикаций",
    ])

    pdf.mockup(paths.get("main"), "Главный экран — вкладка «Группы»", w=175)

    # ── Раздел 2: Группы ─────────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("2. Вкладка «Группы»")
    pdf.body(
        "Группы — это Telegram-чаты (доски объявлений), в которые планировщик "
        "будет публиковать объявления. Для каждой группы настраиваются правила "
        "публикации и список обязательных подписок."
    )

    pdf.h2("Добавление группы")
    pdf.step(1, "Нажмите кнопку «Добавить» в toolbar")
    pdf.step(2, "Заполните поля в диалоге:")
    pdf.bullet([
        "Ссылка / @username — @username группы или t.me/... ссылка (обязательно)",
        "Название — внутреннее название для удобства",
        "Категория / теги — через запятую, для будущей фильтрации",
        "Мин. интервал (мин) — минимум между публикациями в эту группу (от 30 мин по hard limit)",
        "Час начала / конца — разрешённое окно для публикаций (например, 8-22)",
        "Заметки — правила группы, напоминание для себя",
        "Статус — «активна» (публикуем) или «пауза» (пропускаем)",
    ])
    pdf.step(3, "В секции «Обязательные подписки» добавьте каналы если требуется")
    pdf.step(4, "Нажмите OK — группа сохранена")

    pdf.mockup(paths.get("group_dialog"), "Диалог добавления группы с секцией подписок", w=175)

    pdf.h2("Кнопки toolbar")
    pdf.bullet([
        "Добавить — открыть диалог создания группы",
        "Редактировать — изменить выбранную группу (выделите строку в таблице)",
        "Подписки — открыть отдельный диалог управления подписками для выбранной группы",
        "Пауза/Активно — переключить статус группы",
        "Удалить — удалить группу (и все её подписки, связи с объявлениями)",
        "↻ Обновить — перечитать данные из БД",
    ])

    pdf.h2("Таблица групп — столбцы")
    pdf.bullet([
        "Ссылка — @username группы",
        "Название — внутреннее название",
        "Интервал (мин) — минимум между публикациями",
        "Часы — разрешённое окно (например, 08-22)",
        "Статус — активна / пауза / banned / unavailable",
        "Подписан — member (состоим) / unknown (не проверялось) / not_member",
        "Retry до — если есть временный запрет от Telegram, до какого времени",
    ])

    # ── Раздел 3: Объявления ─────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("3. Вкладка «Объявления»")
    pdf.body(
        "Объявление — это текст (+ опционально медиафайл), который публикуется "
        "в одну или несколько групп. Каждое объявление привязывается к конкретному "
        "аккаунту Telegram и набору групп."
    )

    pdf.h2("Создание объявления")
    pdf.step(1, "Нажмите «Добавить»")
    pdf.step(2, "Заполните форму:")
    pdf.bullet([
        "Название — для внутреннего использования (можно оставить пустым, возьмётся из текста)",
        "Аккаунт — с какого аккаунта публиковать (выпадающий список активных аккаунтов)",
        "Текст объявления — основной текст поста",
        "Медиа — путь к фото/видео файлу (кнопка 📁 открывает проводник)",
        "Категория / теги — для фильтрации",
        "Активно — если снято, объявление не публикуется",
        "Группы — отметьте галочками в каких группах публиковать",
    ])
    pdf.step(3, "Опционально: нажмите «✨ Сгенерировать AI» для автоматического создания текста")
    pdf.step(4, "Нажмите OK")

    pdf.mockup(paths.get("ad_dialog"), "Диалог создания объявления с AI-генерацией", w=175)

    pdf.h2("AI-генерация текста")
    pdf.body(
        "Кнопка «✨ Сгенерировать AI» открывает диалог генерации. "
        "Опишите что продаёте или предлагаете — AI создаст готовый текст объявления."
    )
    pdf.bullet([
        "Описание — ключевые факты: что, цена, город, состояние, контакты",
        "Тон — дружелюбный, деловой, срочный (опционально)",
        "Длина — короткое / подробное (опционально)",
        "Провайдер выбирается в настройках Планировщика (OpenAI или Groq)",
    ])
    pdf.note("Пример описания для AI: «iPhone 15 Pro 256GB, чёрный, 9/10, 90000 руб, Москва, торг, СДЭК»")

    pdf.mockup(paths.get("ads_tab"), "Вкладка «Объявления» со списком", w=175)

    # ── Раздел 4: Планировщик ────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("4. Вкладка «Планировщик»")
    pdf.body(
        "Планировщик — фоновый процесс который автоматически находит подходящие "
        "пары (объявление, группа) и публикует с соблюдением всех настроек и лимитов."
    )

    pdf.h2("Быстрый старт")
    pdf.step(1, "Перейдите в вкладку «Планировщик»")
    pdf.step(2, "Убедитесь что в Teleton добавлен хотя бы один активный аккаунт")
    pdf.step(3, "Укажите API-ключ OpenAI или Groq если планируете использовать AI")
    pdf.step(4, "Нажмите «▶ Запустить»")
    pdf.body("Планировщик начнёт работу в фоновом режиме. Все действия отображаются в логе.")

    pdf.h2("Параметры планировщика")
    pdf.bullet([
        "Интервал между публикациями (сек) — пауза между любыми публикациями. Минимум 30 сек.",
        "Лимит публикаций в сутки — максимум публикаций за 24 часа. Максимум 50.",
        "Интервал между вступлениями (сек) — пауза между автовступлениями в каналы. Минимум 5 мин.",
        "Лимит вступлений в сутки — максимум автовступлений за 24 часа. Максимум 15.",
    ])

    pdf.warning("Hard limits нельзя обойти через UI. Они защищают аккаунты от бана.")

    pdf.h2("Как работает один цикл планировщика")
    pdf.bullet([
        "Каждые N секунд (по умолчанию 60) запускается один «тик»",
        "Тик находит следующую доступную пару (объявление, группа)",
        "Проверяет обязательные подписки и при необходимости вступает",
        "Публикует текст (или текст + медиа) в группу",
        "Записывает результат в журнал",
        "Обновляет retry_after группы если Telegram вернул ошибку с временем ожидания",
        "Один тик = одна публикация (не спамит несколько сразу)",
    ])

    pdf.mockup(paths.get("scheduler"), "Вкладка «Планировщик» во время работы", w=175)

    # ── Раздел 5: История ────────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("5. Вкладка «История»")
    pdf.body(
        "История показывает все попытки публикаций — успешные и неудачные. "
        "Используйте фильтр по статусу для диагностики проблем."
    )

    pdf.h2("Статусы публикаций")
    pdf.bullet([
        "ok — публикация прошла успешно",
        "slow_mode — в группе включён slow mode, публикация отложена",
        "flood_wait — превышен личный лимит аккаунта, публикация отложена",
        "forbidden — нет прав на запись (временный запрет или нет прав администратора)",
        "banned — аккаунт заблокирован в канале",
        "error — другая ошибка (группа удалена, недоступна, сетевая ошибка)",
    ])

    pdf.mockup(paths.get("history"), "Вкладка «История» с журналом публикаций", w=175)

    pdf.h2("Диагностика по статусам")
    pdf.h3("slow_mode / flood_wait")
    pdf.body(
        "Нормальная ситуация — планировщик сам ждёт нужное время и публикует позже. "
        "Столбец «Retry до» в таблице групп показывает когда снова можно публиковать."
    )
    pdf.h3("forbidden")
    pdf.body(
        "Проверьте: аккаунт состоит в группе, есть права на публикацию. "
        "Если группа выдаёт «запрет до [дата]» — планировщик автоматически распарсит дату "
        "и установит правильный retry_after."
    )
    pdf.h3("banned")
    pdf.body(
        "Аккаунт заблокирован в канале. Группа будет помечена статусом «banned» "
        "и исключена из ротации на 30 дней. Можно сбросить вручную через «Редактировать» "
        "и установить статус «активна»."
    )

    # ── Раздел 6: Советы ─────────────────────────────────────────────────────
    pdf.add_page()
    pdf.h1("6. Советы и лучшие практики")

    pdf.h2("Настройка интервалов")
    pdf.bullet([
        "Не ставьте интервал меньше 60 минут для одной группы — большинство досок объявлений "
        "имеют slowmode или неформальное правило «не чаще раза в час»",
        "Общий интервал между публикациями 5-10 минут — безопасный режим для 1 аккаунта",
        "При использовании нескольких аккаунтов — создайте отдельные объявления для каждого",
        "Указывайте разрешённые часы (например 8-22) — не публикуйте ночью",
    ])

    pdf.h2("AI-генерация")
    pdf.bullet([
        "Описывайте всё конкретно: цена, город, состояние, контакты",
        "GPT-4o-mini дешевле и быстрее для объявлений, GPT-4o лучше для сложных текстов",
        "Groq (Llama 3.3 70B) — бесплатный при умеренных нагрузках",
        "Используйте тон «срочный» для объявлений с дедлайном",
    ])

    pdf.h2("Медиафайлы")
    pdf.bullet([
        "Поддерживаются: JPG, PNG, GIF, WEBP, MP4",
        "Оптимальный размер фото: до 5 МБ",
        "Путь должен быть абсолютным: C:\\Photos\\iphone.jpg (Windows)",
        "Если файл не найден — публикация пройдёт без медиа",
    ])

    pdf.h2("Защита от бана")
    pdf.bullet([
        "Hard limits вшиты в код и не могут быть отключены через UI",
        "Не добавляйте один аккаунт в несколько активных объявлений одновременно",
        "Следите за статусом «Подписан» в таблице групп",
        "При статусе «banned» — смените аккаунт в объявлении или подождите разбана",
    ])

    output = os.path.join(SCRIPT_DIR, "Teleton_Ads_Manual.pdf")
    pdf.output(output)
    print(f"[+] Основная инструкция: {output}")
    return output


# ─── Инструкция по автоподпискам ─────────────────────────────────────────────

def build_subs_manual(paths):
    pdf = PDF()
    pdf.set_margins(15, 15, 15)

    # Обложка
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 22)
    pdf.set_text_color(52, 131, 235)
    pdf.ln(20)
    pdf.cell(0, 12, "TELETON", ln=True, align="C")
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 9, "Автоматическое вступление", ln=True, align="C")
    pdf.cell(0, 9, "в обязательные подписки", ln=True, align="C")
    pdf.set_font("DejaVu", "", 12)
    pdf.set_text_color(100)
    pdf.ln(4)
    pdf.cell(0, 7, "Подробная инструкция", ln=True, align="C")
    pdf.set_text_color(0)
    pdf.ln(15)

    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(0, 7,
        "Многие группы объявлений требуют быть подписанным на связанные каналы "
        "или состоять в определённых сообществах. Teleton автоматически проверяет "
        "эти условия перед каждой публикацией и вступает в нужные каналы/группы "
        "без вашего участия, соблюдая лимиты безопасности."
    )

    # Раздел 1: Что это
    pdf.add_page()
    pdf.h1("1. Что такое обязательные подписки")
    pdf.body(
        "Обязательные подписки — список каналов и групп Telegram, в которых "
        "аккаунт должен состоять для публикации в конкретную группу объявлений. "
        "Вы задаёте этот список один раз в карточке группы, и планировщик "
        "автоматически следит за выполнением условий."
    )

    pdf.h2("Типичный пример")
    pdf.body(
        "Допустим, группа @msk_objavleniya требует быть подписанным на канал "
        "@msk_news и состоять в чате @msk_sellers. Без этого публикация невозможна. "
        "Вы добавляете оба в список подписок группы — и Teleton сам вступит "
        "в оба канала/чата перед первой публикацией."
    )

    pdf.h2("Что поддерживается")
    pdf.bullet([
        "Публичные каналы — @username",
        "Публичные группы — @username",
        "Приватные каналы/группы — t.me/joinchat/... (пригласительная ссылка)",
        "Ссылки формата t.me/+HASH",
    ])

    # Раздел 2: Настройка
    pdf.add_page()
    pdf.h1("2. Как настроить обязательные подписки")

    pdf.h2("Шаг 1: Открыть карточку группы")
    pdf.step(1, "Перейдите в раздел «Объявления» → вкладка «Группы»")
    pdf.step(2, "Нажмите «Добавить» для новой группы или выберите существующую и «Редактировать»")
    pdf.body("Диалог группы содержит секцию «Обязательные подписки» внизу карточки.")

    pdf.mockup(paths.get("subs_dialog"), "Карточка группы с секцией «Обязательные подписки»", w=175)

    pdf.h2("Шаг 2: Добавить каналы/группы")
    pdf.step(1, "В поле ввода внизу секции введите @username или t.me/... ссылку")
    pdf.step(2, "Нажмите «+ Добавить»")
    pdf.step(3, "Канал появится в списке с иконкой ⬜ (ещё не проверялось)")
    pdf.step(4, "Повторите для всех необходимых каналов")
    pdf.step(5, "Нажмите OK — подписки сохранены")

    pdf.note("Можно добавить любое количество каналов. Планировщик обработает их все.")

    pdf.h2("Шаг 3: Запустить планировщик")
    pdf.body(
        "После сохранения группы с подписками — перейдите на вкладку «Планировщик» "
        "и нажмите «▶ Запустить». При первой публикации в эту группу планировщик "
        "автоматически проверит и выполнит все подписки."
    )

    # Раздел 3: Как это работает
    pdf.add_page()
    pdf.h1("3. Как работает автовступление")
    pdf.body(
        "Каждый раз когда планировщик собирается публиковать в группу, "
        "он выполняет следующую последовательность:"
    )

    pdf.mockup(paths.get("subs_flow"), "Схема работы автовступления", w=175)

    pdf.h2("Подробно по шагам")
    pdf.step(1, "Планировщик находит пару (объявление, группа) для публикации")
    pdf.body(
        "Соблюдаются все условия: группа активна, прошёл нужный интервал, "
        "текущий час в разрешённом окне, нет активного retry_after."
    )

    pdf.step(2, "Проверка каждой подписки через GetParticipantRequest")
    pdf.body(
        "Для каждого канала/группы из списка required_subs выполняется запрос "
        "GetParticipantRequest — это стандартный API Telegram для проверки членства. "
        "Работает одинаково для каналов и групп."
    )

    pdf.step(3, "Если не состоит — вступление через JoinChannelRequest")
    pdf.body("Выполняется с соблюдением hard limits:")
    pdf.bullet([
        "Минимум 5 минут между любыми вступлениями",
        "Максимум 15 вступлений в сутки",
        "При FloodWait — вступление откладывается, публикация тоже откладывается",
    ])

    pdf.step(4, "Если все подписки выполнены — публикация")
    pdf.body(
        "Только после успешной проверки всех подписок планировщик публикует "
        "объявление. Если хотя бы одна подписка не выполнена — публикация в эту "
        "группу откладывается до следующего тика."
    )

    pdf.step(5, "Обновление статуса в БД")
    pdf.body(
        "После проверки статус каждой подписки обновляется в базе данных: "
        "✅ member или ⬜ not_member. Этот статус виден в карточке группы."
    )

    # Раздел 4: Hard limits
    pdf.add_page()
    pdf.h1("4. Лимиты безопасности (Hard Limits)")
    pdf.body(
        "Лимиты вшиты в код и не могут быть изменены через интерфейс. "
        "Они защищают аккаунты от блокировки со стороны Telegram."
    )

    pdf.h2("Лимиты вступлений")
    limits_table = [
        ("Минимальный интервал между вступлениями", "5 минут (300 сек)"),
        ("Максимум вступлений в сутки",              "15"),
    ]
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(110, 8, "Параметр", border=1)
    pdf.cell(60, 8, "Значение", border=1, ln=True)
    pdf.set_font("DejaVu", "", 11)
    for param, val in limits_table:
        pdf.cell(110, 7, param, border=1)
        pdf.cell(60, 7, val, border=1, ln=True)
    pdf.ln(4)

    pdf.h2("Лимиты публикаций")
    pub_table = [
        ("Минимальный интервал между публикациями", "30 секунд"),
        ("Максимум публикаций в сутки (1 аккаунт)", "50"),
        ("Минимальный интервал в одну группу",       "30 минут"),
    ]
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(110, 8, "Параметр", border=1)
    pdf.cell(60, 8, "Значение", border=1, ln=True)
    pdf.set_font("DejaVu", "", 11)
    for param, val in pub_table:
        pdf.cell(110, 7, param, border=1)
        pdf.cell(60, 7, val, border=1, ln=True)
    pdf.ln(4)

    pdf.warning("Попытка поставить значения ниже hard limits через настройки будет автоматически скорректирована до минимально допустимых.")

    # Раздел 5: Диагностика
    pdf.add_page()
    pdf.h1("5. Диагностика и частые ситуации")

    pdf.h2("Статус «⬜ unknown» в карточке группы")
    pdf.body(
        "Планировщик ещё не запускался или ещё не дошёл до этой группы. "
        "После первого тика статус обновится на ✅ member или обозначит что нужно вступить."
    )

    pdf.h2("«Не выполнены подписки для @group, откладываем» в логе")
    pdf.body("Одна или несколько подписок не удалась. Причины:")
    pdf.bullet([
        "Исчерпан дневной лимит вступлений (15/сутки) — ждите до следующего дня",
        "Слишком рано после последнего вступления (5 мин интервал) — ждите",
        "Канал приватный и ссылка устарела — обновите ссылку в списке подписок",
        "FloodWait — Telegram временно ограничил аккаунт, планировщик подождёт автоматически",
    ])

    pdf.h2("Приватные каналы по инвайт-ссылке")
    pdf.body(
        "Для вступления в приватный канал через инвайт-ссылку — добавьте полную ссылку:"
    )
    pdf.code("t.me/joinchat/AAAAAAAAAAAAAAAA\nили\nt.me/+HASH_СТРОКА")
    pdf.body(
        "Teleton автоматически определит тип ссылки и использует нужный метод вступления."
    )

    pdf.h2("Проверить подписки вручную")
    pdf.body(
        "Для ручной проверки — выделите группу в таблице и нажмите «Подписки». "
        "Диалог покажет текущий статус каждой подписки (✅/⬜) на основе последней проверки. "
        "Фактическая проверка через Telegram API происходит только при работе планировщика."
    )

    pdf.h2("Удаление подписки")
    pdf.bullet([
        "Откройте карточку группы через «Редактировать»",
        "В секции «Обязательные подписки» нажмите ✕ рядом с нужным каналом",
        "Нажмите OK — подписка удалена из требований",
        "Планировщик больше не будет проверять этот канал перед публикацией в группу",
    ])

    pdf.h2("Логи автовступления")
    pdf.body("В логе вкладки «Планировщик» вы увидите:")
    pdf.code(
        "[~] Не состоим в @channel_name, пробуем вступить...\n"
        "[+] Вступил в @channel_name\n"
        "[!] FloodWait 120s при вступлении в @channel_name\n"
        "[!] Лимит вступлений в сутки исчерпан (15), пропускаем @channel_name\n"
        "[!] Не выполнены подписки для @group, откладываем"
    )

    output = os.path.join(SCRIPT_DIR, "Teleton_Ads_Subscriptions.pdf")
    pdf.output(output)
    print(f"[+] Инструкция по подпискам: {output}")
    return output


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("[~] Генерирую мокапы...")
    paths = {
        "main":        mk_main_screen(),
        "group_dialog":mk_group_dialog(),
        "ads_tab":     mk_ads_tab(),
        "ad_dialog":   mk_ad_dialog(),
        "scheduler":   mk_scheduler_tab(),
        "history":     mk_history_tab(),
        "subs_flow":   mk_subs_flow(),
        "subs_dialog": mk_subs_dialog_detail(),
    }
    print("[~] Генерирую PDF...")
    build_main_manual(paths)
    build_subs_manual(paths)
    print("[+] Готово!")
