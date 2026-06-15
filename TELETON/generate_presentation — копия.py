"""
Генератор PDF-презентации Teleton.
Запуск: python generate_presentation.py
"""

import os
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SLIDES_DIR = os.path.join(SCRIPT_DIR, "data", "slides")
OUTPUT_PDF = os.path.join(SCRIPT_DIR, "Teleton_Presentation.pdf")
FONT_DIR = "C:/Windows/Fonts"

os.makedirs(SLIDES_DIR, exist_ok=True)

# --- Цвета ---
BG_DARK = (18, 18, 24)
BG_CARD = (28, 28, 38)
BG_CARD2 = (35, 35, 48)
ACCENT = (52, 131, 235)
ACCENT2 = (100, 160, 255)
GREEN = (47, 200, 130)
ORANGE = (255, 170, 50)
RED = (240, 75, 65)
PURPLE = (155, 100, 235)
CYAN = (50, 210, 220)
WHITE = (240, 240, 245)
GRAY = (140, 140, 160)
DIM = (80, 80, 100)

W, H = 1920, 1080


def font(size=32, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def font_mono(size=28):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, "consola.ttf"), size)
    except Exception:
        return ImageFont.load_default()


def new_slide():
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)
    return img, draw


def draw_gradient_bar(draw, y, h, color_left, color_right):
    """Горизонтальный градиент"""
    for x in range(W):
        t = x / W
        r = int(color_left[0] * (1 - t) + color_right[0] * t)
        g = int(color_left[1] * (1 - t) + color_right[1] * t)
        b = int(color_left[2] * (1 - t) + color_right[2] * t)
        draw.line([(x, y), (x, y + h)], fill=(r, g, b))


def draw_pill(draw, xy, text, color, text_color=WHITE, font_size=22):
    """Скруглённый badge/pill"""
    x, y = xy
    f = font(font_size, bold=True)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pw, ph = tw + 30, th + 16
    draw.rounded_rectangle((x, y, x + pw, y + ph), radius=ph // 2, fill=color)
    draw.text((x + 15, y + 6), text, fill=text_color, font=f)
    return pw


def draw_icon_circle(draw, cx, cy, r, color, icon_char=""):
    """Круглая иконка с символом"""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    if icon_char:
        f = font(int(r * 1.1), bold=True)
        bbox = draw.textbbox((0, 0), icon_char, font=f)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - 4), icon_char, fill=WHITE, font=f)


def draw_feature_card(draw, x, y, w, h, icon, icon_color, title, desc):
    """Карточка фичи"""
    draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=BG_CARD)
    # Иконка
    draw_icon_circle(draw, x + 50, y + 50, 30, icon_color, icon)
    # Заголовок
    draw.text((x + 95, y + 30), title, fill=WHITE, font=font(28, True))
    # Описание
    lines = _wrap_text(desc, w - 40, font(22))
    ly = y + 72
    for line in lines:
        draw.text((x + 20, ly), line, fill=GRAY, font=font(22))
        ly += 30


def draw_step_card(draw, x, y, w, h, num, title, desc, color=ACCENT):
    """Карточка шага"""
    draw.rounded_rectangle((x, y, x + w, y + h), radius=14, fill=BG_CARD)
    # Номер
    draw.rounded_rectangle((x + 20, y + 20, x + 60, y + 60), radius=10, fill=color)
    f = font(24, True)
    bbox = draw.textbbox((0, 0), str(num), font=f)
    tw = bbox[2] - bbox[0]
    draw.text((x + 40 - tw // 2, y + 25), str(num), fill=WHITE, font=f)
    # Текст
    draw.text((x + 75, y + 22), title, fill=WHITE, font=font(24, True))
    draw.text((x + 75, y + 56), desc, fill=GRAY, font=font(20))


def draw_stat_block(draw, x, y, value, label, color):
    """Блок статистики"""
    draw.text((x, y), value, fill=color, font=font(60, True))
    draw.text((x, y + 70), label, fill=GRAY, font=font(22))


def _wrap_text(text, max_w, f):
    """Разбивка текста на строки по ширине"""
    words = text.split()
    lines = []
    current = ""
    dummy = Image.new("RGB", (1, 1))
    dd = ImageDraw.Draw(dummy)
    for word in words:
        test = f"{current} {word}".strip()
        bbox = dd.textbbox((0, 0), test, font=f)
        if bbox[2] - bbox[0] <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_bottom_bar(draw, page_num, total):
    """Полоска внизу"""
    draw.rectangle((0, H - 50, W, H), fill=(14, 14, 18))
    # Прогресс
    bar_w = 300
    bar_x = W // 2 - bar_w // 2
    draw.rounded_rectangle((bar_x, H - 32, bar_x + bar_w, H - 22), radius=5, fill=DIM)
    fill_w = int(bar_w * page_num / total)
    if fill_w > 0:
        draw.rounded_rectangle((bar_x, H - 32, bar_x + fill_w, H - 22), radius=5, fill=ACCENT)
    # Номер
    draw.text((W - 80, H - 42), f"{page_num}/{total}", fill=DIM, font=font(18))


# ============================================================
# Слайды
# ============================================================

TOTAL_SLIDES = 10


def slide_01_cover():
    """Обложка"""
    img, draw = new_slide()

    # Градиентная полоса сверху
    draw_gradient_bar(draw, 0, 6, ACCENT, PURPLE)

    # Декоративные круги
    for cx, cy, r, alpha in [(150, 200, 180, 25), (1800, 150, 120, 20),
                               (1700, 900, 200, 15), (100, 850, 100, 20)]:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*ACCENT, alpha))
        img.paste(Image.alpha_composite(Image.new("RGBA", (W, H), (*BG_DARK, 255)), overlay).convert("RGB"))
        draw = ImageDraw.Draw(img)

    # Логотип
    draw.text((W // 2 - 190, 240), "TELETON", fill=WHITE, font=font(96, True))

    # Подзаголовок
    draw_gradient_bar(draw, 370, 3, ACCENT, PURPLE)
    draw.text((W // 2 - 380, 410), "Умная рассылка и парсинг для Telegram",
              fill=GRAY, font=font(36))

    # Пиллы
    pills = [("Мульти-аккаунт", ACCENT), ("Смарт-парсинг", GREEN),
             ("ИИ-анализ", PURPLE), ("GUI + CLI", ORANGE)]
    px = W // 2 - 380
    for text, color in pills:
        pw = draw_pill(draw, (px, 500), text, color, font_size=24)
        px += pw + 20

    # Версия
    draw.text((W // 2 - 80, 600), "Версия 2.0", fill=DIM, font=font(24))

    draw_bottom_bar(draw, 1, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "01.png"))


def slide_02_problem():
    """Проблема → Решение"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, RED, ORANGE)

    draw.text((100, 60), "Зачем нужен Teleton?", fill=WHITE, font=font(48, True))

    # Левая колонка — проблемы
    draw.rounded_rectangle((80, 150, 900, 620), radius=20, fill=(40, 25, 25))
    draw.text((120, 170), "Проблемы ручной работы", fill=RED, font=font(30, True))

    problems = [
        "Telegram лимитирует парсинг до 200 участников",
        "FloodWait блокирует отправку через 1 аккаунт",
        "Невозможно найти целевую аудиторию по постам",
        "Ручная рассылка занимает часы работы",
        "Нет аналитики — непонятно что работает",
    ]
    py = 230
    for p in problems:
        draw.text((140, py), "✕", fill=RED, font=font(24, True))
        draw.text((175, py), p, fill=(200, 180, 180), font=font(24))
        py += 55

    # Правая колонка — решения
    draw.rounded_rectangle((1020, 150, 1840, 620), radius=20, fill=(20, 35, 25))
    draw.text((1060, 170), "Решение: Teleton", fill=GREEN, font=font(30, True))

    solutions = [
        "Aggressive-парсинг обходит лимит (1000+ юзеров)",
        "Авто-ротация аккаунтов при FloodWait",
        "Смарт-парсинг по словам и ИИ",
        "Полная автоматизация с GUI и CLI",
        "Статистика отправок в реальном времени",
    ]
    sy = 230
    for s in solutions:
        draw.text((1040, sy), "✓", fill=GREEN, font=font(24, True))
        draw.text((1075, sy), s, fill=(180, 220, 190), font=font(24))
        sy += 55

    # Стрелка
    draw.text((940, 370), "→", fill=ACCENT, font=font(60, True))

    # Цифры внизу
    stats = [("10x", "быстрее ручной\nрассылки", ACCENT),
             ("1000+", "участников\nза один парсинг", GREEN),
             ("24/7", "автоматическая\nработа", ORANGE)]
    sx = 250
    for val, label, color in stats:
        draw.text((sx, 680), val, fill=color, font=font(56, True))
        for i, line in enumerate(label.split("\n")):
            draw.text((sx, 750 + i * 28), line, fill=GRAY, font=font(22))
        sx += 520

    draw_bottom_bar(draw, 2, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "02.png"))


def slide_03_features_overview():
    """Обзор ключевых фич"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, ACCENT, CYAN)

    draw.text((100, 50), "Ключевые возможности", fill=WHITE, font=font(48, True))

    features = [
        ("🔍", ACCENT, "Парсинг участников",
         "Обычный, aggressive (по алфавиту) и парсинг комментаторов каналов"),
        ("🧠", PURPLE, "Смарт-парсинг по ИИ",
         "OpenAI GPT анализирует посты и находит целевую аудиторию по смыслу"),
        ("🔑", GREEN, "Поиск по ключевым словам",
         "Мгновенный поиск авторов постов с нужными словами — бесплатно"),
        ("📢", ORANGE, "Массовые упоминания",
         "Inline-теги пользователей в группах с авто-ротацией аккаунтов"),
        ("📨", CYAN, "Рассылка со Spintax",
         "Рандомизация текста {вариант1|вариант2} для уникальности"),
        ("👥", RED, "Мульти-аккаунт",
         "Работа через несколько аккаунтов с умной ротацией при FloodWait"),
    ]

    col1_x, col2_x = 80, 980
    y_start = 160
    card_w, card_h = 840, 130

    for i, (icon, color, title, desc) in enumerate(features):
        col = i % 2
        row = i // 2
        x = col1_x if col == 0 else col2_x
        y = y_start + row * (card_h + 25)

        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=14, fill=BG_CARD)
        # Цветная полоска слева
        draw.rounded_rectangle((x, y, x + 6, y + card_h), radius=3, fill=color)
        # Иконка
        draw.text((x + 25, y + 20), icon, fill=color, font=font(44))
        # Текст
        draw.text((x + 90, y + 18), title, fill=WHITE, font=font(26, True))
        lines = _wrap_text(desc, card_w - 110, font(21))
        for j, line in enumerate(lines):
            draw.text((x + 90, y + 55 + j * 28), line, fill=GRAY, font=font(21))

    # Бейдж
    draw.rounded_rectangle((80, 680, 350, 720), radius=20, fill=BG_CARD2)
    draw.text((100, 688), "GUI + CLI интерфейс", fill=ACCENT2, font=font(22))

    draw.rounded_rectangle((380, 680, 620, 720), radius=20, fill=BG_CARD2)
    draw.text((400, 688), "SQLite хранилище", fill=GREEN, font=font(22))

    draw.rounded_rectangle((650, 680, 920, 720), radius=20, fill=BG_CARD2)
    draw.text((670, 688), "Прокси (SOCKS5)", fill=ORANGE, font=font(22))

    draw_bottom_bar(draw, 3, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "03.png"))


def slide_04_smart_parse():
    """Смарт-парсинг (главная фишка)"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, PURPLE, ACCENT)

    draw.text((100, 50), "Смарт-парсинг — главная фишка", fill=WHITE, font=font(48, True))
    draw.text((100, 115), "Находите целевую аудиторию по содержимому постов, а не просто списки участников",
              fill=GRAY, font=font(24))

    # Два режима
    # Левый — Keywords
    bx, by = 80, 180
    bw, bh = 860, 500
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=18, fill=BG_CARD)
    draw_pill(draw, (bx + 20, by + 20), "БЕСПЛАТНО", GREEN, font_size=18)
    draw.text((bx + 30, by + 65), "Режим: Ключевые слова", fill=WHITE, font=font(32, True))
    draw.text((bx + 30, by + 115), "Точный поиск по вхождению слов в текст поста.",
              fill=GRAY, font=font(22))
    draw.text((bx + 30, by + 145), "Быстро, без внешних API, без затрат.", fill=GRAY, font=font(22))

    # Пример
    draw.rounded_rectangle((bx + 30, by + 195, bx + bw - 30, by + 310), radius=10, fill=(22, 22, 30))
    draw.text((bx + 50, by + 210), "Ключевые слова:", fill=ACCENT2, font=font(18))
    draw.text((bx + 50, by + 238), '"трафик, твиттер, reddit, автоматизация"',
              fill=WHITE, font=font_mono(20))
    draw.text((bx + 50, by + 275), "→ Совпадение: @user — трафик, твиттер",
              fill=GREEN, font=font_mono(18))

    # Плюсы
    kw_pros = ["Мгновенная проверка", "Нулевая стоимость", "Точное совпадение", "Любой объём"]
    py = by + 335
    for p in kw_pros:
        draw.text((bx + 50, py), "✓", fill=GREEN, font=font(20, True))
        draw.text((bx + 80, py), p, fill=WHITE, font=font(21))
        py += 36

    # Правый — AI
    bx2 = 1000
    draw.rounded_rectangle((bx2, by, bx2 + bw, by + bh), radius=18, fill=BG_CARD)
    draw_pill(draw, (bx2 + 20, by + 20), "OPENAI GPT", PURPLE, font_size=18)
    draw.text((bx2 + 30, by + 65), "Режим: ИИ-анализ", fill=WHITE, font=font(32, True))
    draw.text((bx2 + 30, by + 115), "Нейросеть понимает смысл и контекст.",
              fill=GRAY, font=font(22))
    draw.text((bx2 + 30, by + 145), "Находит то, что ключевые слова не поймают.", fill=GRAY, font=font(22))

    # Пример
    draw.rounded_rectangle((bx2 + 30, by + 195, bx2 + bw - 30, by + 310), radius=10, fill=(22, 22, 30))
    draw.text((bx2 + 50, by + 210), "Критерий:", fill=ACCENT2, font=font(18))
    draw.text((bx2 + 50, by + 238), '"ищет трафик с соцсетей или предлагает"',
              fill=WHITE, font=font_mono(20))
    draw.text((bx2 + 50, by + 275), "→ AI: Пользователь запрашивает источники трафика",
              fill=PURPLE, font=font_mono(18))

    ai_pros = ["Понимает синонимы", "Анализирует контекст", "Батч-обработка (экономия)", "~$0.01 / 500 постов"]
    py = by + 335
    for p in ai_pros:
        draw.text((bx2 + 50, py), "✓", fill=PURPLE, font=font(20, True))
        draw.text((bx2 + 80, py), p, fill=WHITE, font=font(21))
        py += 36

    # VS
    draw.rounded_rectangle((920, by + 320, 980, by + 370), radius=25, fill=ORANGE)
    draw.text((932, by + 328), "VS", fill=WHITE, font=font(22, True))

    draw_bottom_bar(draw, 4, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "04.png"))


def slide_05_multi_account():
    """Мульти-аккаунт и ротация"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, ORANGE, RED)

    draw.text((100, 50), "Мульти-аккаунт и умная ротация", fill=WHITE, font=font(48, True))
    draw.text((100, 115), "Автоматическое переключение между аккаунтами при ограничениях Telegram",
              fill=GRAY, font=font(24))

    # Схема ротации
    # Аккаунты слева
    accounts = [
        ("+7 900 123-45-67", "Активен", GREEN),
        ("+7 900 765-43-21", "FloodWait", ORANGE),
        ("+7 900 987-65-43", "Ожидает", GRAY),
    ]
    ax = 120
    for i, (phone, status, color) in enumerate(accounts):
        ay = 220 + i * 130
        draw.rounded_rectangle((ax, ay, ax + 400, ay + 100), radius=14, fill=BG_CARD)
        draw_icon_circle(draw, ax + 45, ay + 50, 25, color, str(i + 1))
        draw.text((ax + 85, ay + 15), phone, fill=WHITE, font=font(24, True))
        draw.text((ax + 85, ay + 50), status, fill=color, font=font(20))
        # Стрелка
        if i < 2:
            draw.text((ax + 180, ay + 100), "↓", fill=DIM, font=font(24))

    # Стрелка к целевой группе
    draw.text((560, 340), "→→→", fill=ACCENT, font=font(48, True))

    # Целевая группа
    draw.rounded_rectangle((750, 250, 1200, 430), radius=18, fill=BG_CARD)
    draw.text((800, 275), "Целевая группа", fill=WHITE, font=font(28, True))
    draw.text((800, 320), "@target_group", fill=ACCENT2, font=font(24))
    draw.text((800, 365), "Сообщения доставляются", fill=GRAY, font=font(20))
    draw.text((800, 393), "без перерывов", fill=GREEN, font=font(20, True))

    # Процесс справа
    steps = [
        ("1", "Аккаунт #1 отправляет батчи", "50 сообщений → FloodWait"),
        ("2", "Авто-ротация на аккаунт #2", "Мгновенное переключение"),
        ("3", "Аккаунт #2 продолжает", "Без потери прогресса"),
        ("4", "Деактивация при бане", "PeerFloodError → пауза 24ч"),
    ]
    sy = 210
    for num, title, desc in steps:
        draw.rounded_rectangle((1320, sy, 1840, sy + 85), radius=12, fill=BG_CARD)
        # Номер
        draw.rounded_rectangle((1340, sy + 15, 1378, sy + 55), radius=8, fill=ACCENT)
        draw.text((1350, sy + 20), num, fill=WHITE, font=font(22, True))
        draw.text((1395, sy + 12), title, fill=WHITE, font=font(22, True))
        draw.text((1395, sy + 45), desc, fill=GRAY, font=font(18))
        sy += 105

    # Защита от бана
    draw.rounded_rectangle((100, 640, 1840, 760), radius=16, fill=(30, 25, 20))
    draw.text((140, 660), "Встроенная защита от бана", fill=ORANGE, font=font(30, True))
    protections = [
        "Случайные задержки 15-60 сек",
        "Лимит сообщений на сессию",
        "Spintax-рандомизация текста",
        "Авто-пауза при FloodWait",
    ]
    px = 140
    for p in protections:
        draw.text((px, 710), f"•  {p}", fill=(200, 190, 170), font=font(22))
        px += 420

    draw_bottom_bar(draw, 5, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "05.png"))


def slide_06_spintax():
    """Spintax и упоминания"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, CYAN, GREEN)

    draw.text((100, 50), "Spintax и массовые упоминания", fill=WHITE, font=font(48, True))

    # Spintax блок
    draw.rounded_rectangle((80, 160, 920, 540), radius=18, fill=BG_CARD)
    draw.text((120, 185), "Spintax — уникальность каждого сообщения",
              fill=CYAN, font=font(26, True))

    draw.text((120, 245), "Шаблон:", fill=GRAY, font=font(20))
    draw.rounded_rectangle((120, 275, 880, 335), radius=8, fill=(22, 22, 30))
    draw.text((140, 288), '{Привет|Здравствуйте|Добрый день}! Наш {новый|свежий} проект',
              fill=WHITE, font=font_mono(20))

    draw.text((120, 360), "Результаты:", fill=GRAY, font=font(20))
    variants = [
        "→  Привет! Наш новый проект",
        "→  Здравствуйте! Наш свежий проект",
        "→  Добрый день! Наш новый проект",
    ]
    vy = 395
    for v in variants:
        draw.text((140, vy), v, fill=GREEN, font=font_mono(20))
        vy += 38

    # Упоминания блок
    draw.rounded_rectangle((1000, 160, 1840, 540), radius=18, fill=BG_CARD)
    draw.text((1040, 185), "Inline-упоминания в группах",
              fill=ORANGE, font=font(26, True))

    draw.text((1040, 245), "Формат сообщения:", fill=GRAY, font=font(20))
    draw.rounded_rectangle((1040, 275, 1800, 380), radius=8, fill=(22, 22, 30))
    draw.text((1060, 290), "Привет! Посмотрите наш проект!", fill=WHITE, font=font_mono(20))
    draw.text((1060, 325), "@user1 @user2 @user3 @user4 @user5", fill=ACCENT2, font=font_mono(20))

    features_m = [
        "5 упоминаний на сообщение (настраиваемо)",
        "UTF-16 offsets для корректных тегов",
        "Трекинг уже упомянутых — без дублей",
        "Авто-ротация при FloodWait",
    ]
    my = 405
    for f_text in features_m:
        draw.text((1060, my), f"•  {f_text}", fill=GRAY, font=font(21))
        my += 34

    # Нижний блок — Dry Run
    draw.rounded_rectangle((80, 590, 1840, 710), radius=16, fill=(20, 30, 25))
    draw.text((140, 610), "Режим Dry Run — безопасное тестирование", fill=GREEN, font=font(28, True))
    draw.text((140, 655), "Все действия логируются без реальной отправки. Идеально для проверки настроек перед запуском.",
              fill=(170, 200, 180), font=font(22))

    draw_bottom_bar(draw, 6, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "06.png"))


def slide_07_gui():
    """GUI-интерфейс"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, ACCENT, GREEN)

    draw.text((100, 50), "Удобный GUI-интерфейс", fill=WHITE, font=font(48, True))
    draw.text((100, 115), "Полноценное десктоп-приложение на CustomTkinter (темная тема)",
              fill=GRAY, font=font(24))

    # Мини-превью экранов
    screens = [
        ("Аккаунты", "Добавление, авторизация,\nвключение/выключение", ACCENT),
        ("Парсинг", "Обычный + смарт-парсинг\nв двух вкладках", GREEN),
        ("Рассылка", "Упоминания + broadcast\nиз задач", ORANGE),
        ("Задачи", "Управление задачами\nрассылки", CYAN),
        ("Статистика", "Карточки с метриками\nза N дней", PURPLE),
        ("Настройки", "Задержки, API ключи,\nOpenAI — всё в GUI", RED),
    ]

    for i, (title, desc, color) in enumerate(screens):
        col = i % 3
        row = i // 3
        x = 100 + col * 600
        y = 200 + row * 310

        # Карточка
        draw.rounded_rectangle((x, y, x + 540, y + 270), radius=16, fill=BG_CARD)

        # Мини «окно»
        draw.rounded_rectangle((x + 20, y + 20, x + 520, y + 60), radius=8, fill=(40, 40, 55))
        # Три точки
        for j, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            draw.ellipse((x + 35 + j * 22, y + 32, x + 47 + j * 22, y + 44), fill=c)
        draw.text((x + 110, y + 28), title, fill=WHITE, font=font(20, True))

        # Цветная линия
        draw.rectangle((x + 20, y + 65, x + 520, y + 69), fill=color)

        # Описание
        lines = desc.split("\n")
        dy = y + 90
        for line in lines:
            draw.text((x + 30, dy), line, fill=GRAY, font=font(24))
            dy += 34

        # Бейдж
        draw_pill(draw, (x + 30, y + 200), title, color, font_size=18)

    draw_bottom_bar(draw, 7, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "07.png"))


def slide_08_cli():
    """CLI-интерфейс"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, GREEN, CYAN)

    draw.text((100, 50), "Мощный CLI-интерфейс", fill=WHITE, font=font(48, True))
    draw.text((100, 115), "8 команд для полного управления из терминала. Идеально для автоматизации и cron-задач.",
              fill=GRAY, font=font(24))

    # Терминал
    tx, ty = 100, 190
    tw, th = 1720, 560
    draw.rounded_rectangle((tx, ty, tx + tw, ty + th), radius=14, fill=(15, 15, 20))
    # Title bar
    draw.rounded_rectangle((tx, ty, tx + tw, ty + 40), radius=14, fill=(35, 35, 45))
    draw.rectangle((tx, ty + 28, tx + tw, ty + 40), fill=(35, 35, 45))
    for j, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        draw.ellipse((tx + 18 + j * 22, ty + 10, tx + 32 + j * 22, ty + 24), fill=c)
    draw.text((tx + 100, ty + 7), "Terminal — Teleton CLI", fill=GRAY, font=font(18))

    commands = [
        ("$", "python main.py smart-parse --group @chat --mode keywords --keywords \"трафик,софт\"", (100, 200, 100)),
        ("$", "python main.py parse --group @chat --aggressive", (100, 200, 100)),
        ("$", "python main.py mention --target @group --source @chat --message \"Привет!\"", (100, 200, 100)),
        ("$", "python main.py broadcast --dry-run", (100, 200, 100)),
        ("$", "python main.py add-account --phone +79001234567", (100, 200, 100)),
        ("$", "python main.py auth --phone +79001234567", (100, 200, 100)),
        ("$", "python main.py stats --days 30", (100, 200, 100)),
        ("", "", None),
        ("", "[+] Спарсено 1247 пользователей из @chat", GREEN),
        ("", "[+] Совпадение: @user_alpha — трафик, твиттер", GREEN),
        ("", "[~] Ротация с +79001234567 → +79007654321", ORANGE),
        ("", "=== Итого: 42 совпадения найдено ===", ACCENT2),
    ]

    cy = ty + 55
    mono = font_mono(20)
    for prefix, text, color in commands:
        if not text:
            cy += 12
            continue
        if prefix:
            draw.text((tx + 25, cy), prefix, fill=GRAY, font=mono)
            draw.text((tx + 50, cy), text, fill=color, font=mono)
        else:
            draw.text((tx + 25, cy), text, fill=color, font=mono)
        cy += 32

    draw.text((100, 790), "Совместим с cron, Task Scheduler, shell-скриптами и пайплайнами",
              fill=DIM, font=font(22))

    draw_bottom_bar(draw, 8, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "08.png"))


def slide_09_tech():
    """Технологии"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 4, PURPLE, ACCENT)

    draw.text((100, 50), "Технологический стек", fill=WHITE, font=font(48, True))

    techs = [
        ("Python 3", "async/await, dataclasses", ACCENT, "Py"),
        ("Telethon", "Telegram MTProto API", CYAN, "TG"),
        ("SQLite", "Локальное хранилище", GREEN, "DB"),
        ("OpenAI", "GPT-4o-mini для ИИ", PURPLE, "AI"),
        ("CustomTkinter", "Современный GUI", ORANGE, "UI"),
        ("python-socks", "SOCKS5 прокси", RED, "PX"),
    ]

    for i, (name, desc, color, abbr) in enumerate(techs):
        col = i % 3
        row = i // 3
        x = 100 + col * 600
        y = 170 + row * 220

        draw.rounded_rectangle((x, y, x + 540, y + 180), radius=16, fill=BG_CARD)
        # Иконка
        draw.rounded_rectangle((x + 25, y + 25, x + 95, y + 85), radius=12, fill=color)
        f = font(28, True)
        bbox = draw.textbbox((0, 0), abbr, font=f)
        tw = bbox[2] - bbox[0]
        draw.text((x + 60 - tw // 2, y + 37), abbr, fill=WHITE, font=f)

        draw.text((x + 115, y + 30), name, fill=WHITE, font=font(28, True))
        draw.text((x + 115, y + 70), desc, fill=GRAY, font=font(22))

        # Детали
        details_map = {
            "Py": "Полностью асинхронный код",
            "TG": "Прямой доступ к API Telegram",
            "DB": "Без внешних серверов, всё локально",
            "AI": "Батч-обработка, JSON mode",
            "UI": "Темная тема, отзывчивый интерфейс",
            "PX": "Каждый аккаунт — свой прокси",
        }
        draw.text((x + 115, y + 110), details_map.get(abbr, ""), fill=DIM, font=font(19))

    # Архитектура
    draw.rounded_rectangle((100, 650, 1820, 770), radius=16, fill=BG_CARD)
    draw.text((140, 670), "Архитектура:", fill=ACCENT2, font=font(26, True))
    arch_items = ["main.py (CLI)", "gui.py (GUI)", "parser.py", "sender.py",
                  "mentioner.py", "ai_filter.py", "database.py", "spintax.py"]
    ax = 140
    for item in arch_items:
        draw_pill(draw, (ax, 715), item, BG_CARD2, text_color=GRAY, font_size=18)
        bbox = draw.textbbox((0, 0), item, font=font(18, True))
        ax += (bbox[2] - bbox[0]) + 50

    draw_bottom_bar(draw, 9, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "09.png"))


def slide_10_start():
    """Финальный слайд — быстрый старт"""
    img, draw = new_slide()
    draw_gradient_bar(draw, 0, 6, ACCENT, GREEN)

    draw.text((W // 2 - 320, 80), "Начните за 3 минуты", fill=WHITE, font=font(56, True))

    steps = [
        ("1", "Установка", "pip install -r requirements.txt", ACCENT),
        ("2", "Настройка", "Заполните .env (API ID + Hash)", GREEN),
        ("3", "Аккаунт", "python main.py add-account --phone +7...", ORANGE),
        ("4", "Авторизация", "python main.py auth --phone +7...", CYAN),
        ("5", "Запуск!", "python gui.py  или  python main.py --help", PURPLE),
    ]

    for i, (num, title, cmd, color) in enumerate(steps):
        y = 200 + i * 110
        # Линия
        if i < len(steps) - 1:
            draw.rectangle((200, y + 80, 206, y + 110), fill=DIM)

        draw.rounded_rectangle((120, y, 1800, y + 90), radius=14, fill=BG_CARD)
        # Номер
        draw.rounded_rectangle((145, y + 15, 195, y + 65), radius=12, fill=color)
        draw.text((160, y + 22), num, fill=WHITE, font=font(28, True))

        draw.text((220, y + 12), title, fill=WHITE, font=font(28, True))
        draw.rounded_rectangle((220, y + 50, 1780, y + 78), radius=6, fill=(22, 22, 30))
        draw.text((235, y + 52), cmd, fill=color, font=font_mono(18))

    # Call to action
    draw.rounded_rectangle((W // 2 - 300, 800, W // 2 + 300, 870), radius=35, fill=ACCENT)
    draw.text((W // 2 - 150, 818), "python gui.py", fill=WHITE, font=font(32, True))

    draw.text((W // 2 - 130, 900), "TELETON v2.0  •  2026",
              fill=DIM, font=font(22))

    draw_bottom_bar(draw, 10, TOTAL_SLIDES)
    img.save(os.path.join(SLIDES_DIR, "10.png"))


# ============================================================
# Сборка PDF
# ============================================================

def build_pdf():
    pdf = FPDF(orientation="L", unit="mm", format=(171.45, 304.8))  # 16:9

    slides = sorted(f for f in os.listdir(SLIDES_DIR) if f.endswith(".png"))
    for slide in slides:
        pdf.add_page()
        pdf.image(os.path.join(SLIDES_DIR, slide), x=0, y=0, w=304.8, h=171.45)

    pdf.output(OUTPUT_PDF)
    return OUTPUT_PDF


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    generators = [
        ("01 — Обложка", slide_01_cover),
        ("02 — Проблема и решение", slide_02_problem),
        ("03 — Ключевые возможности", slide_03_features_overview),
        ("04 — Смарт-парсинг", slide_04_smart_parse),
        ("05 — Мульти-аккаунт", slide_05_multi_account),
        ("06 — Spintax и упоминания", slide_06_spintax),
        ("07 — GUI-интерфейс", slide_07_gui),
        ("08 — CLI-интерфейс", slide_08_cli),
        ("09 — Технологии", slide_09_tech),
        ("10 — Быстрый старт", slide_10_start),
    ]

    print("Генерация слайдов...")
    for name, gen in generators:
        gen()
        print(f"  [+] {name}")

    print("\nСборка PDF...")
    path = build_pdf()
    print(f"\n=== Презентация создана: {path} ===")
