import os
import json
import logging
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from math import prod
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── KEEP-ALIVE ───────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def start_health_server():
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info("Health server запущен ✅")


# ─── САМОПИНГ ─────────────────────────────────────────────────────────────────
def self_ping_loop():
    url = os.environ.get("SELF_URL", "")
    if not url:
        return
    while True:
        time.sleep(8 * 60)
        try:
            urllib.request.urlopen(url, timeout=10)
            logger.info("Self-ping OK ✅")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")

def start_self_ping():
    url = os.environ.get("SELF_URL", "")
    if not url:
        logger.info("SELF_URL не задан — самопинг отключён")
        return
    t = threading.Thread(target=self_ping_loop, daemon=True)
    t.start()
    logger.info(f"Самопинг запущен ✅")

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ROSTOVBETS")
ADMIN_IDS  = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x]
STATS_FILE = "stats.json"

# ─── ШАГИ ДИАЛОГА ─────────────────────────────────────────────────────────────
(
    SPORT,
    BET_TYPE,
    EVENT, FIGHTERS, BET_ON, SINGLE_ODDS,
    EXPRESS_COUNT, EXPRESS_AMOUNT, EXPRESS_LEG,
    PHOTO, PREVIEW,
) = range(11)

# ─── ВИДЫ СПОРТА ──────────────────────────────────────────────────────────────
SPORTS = {
    "ufc":        {"emoji": "🥊", "name": "UFC / MMA",   "event": "Турнир",  "match": "Бой",     "teams": "Бойцы"},
    "football":   {"emoji": "⚽", "name": "Футбол",       "event": "Матч",    "match": "Матч",    "teams": "Команды"},
    "hockey":     {"emoji": "🏒", "name": "Хоккей",       "event": "Матч",    "match": "Матч",    "teams": "Команды"},
    "basketball": {"emoji": "🏀", "name": "Баскетбол",    "event": "Матч",    "match": "Матч",    "teams": "Команды"},
    "tennis":     {"emoji": "🎾", "name": "Теннис",       "event": "Турнир",  "match": "Матч",    "teams": "Игроки"},
    "boxing":     {"emoji": "🥋", "name": "Бокс",         "event": "Вечер",   "match": "Бой",     "teams": "Боксёры"},
    "other":      {"emoji": "🏆", "name": "Другое",       "event": "Событие", "match": "Событие", "teams": "Участники"},
}

# ─── ЭМОДЗИ ───────────────────────────────────────────────────────────────────
E = {
    "fire":  "🔥",
    "money": "💰",
    "odds":  "📊",
    "arrow": "➡️",
    "wait":  "⏳",
    "crown": "👑",
    "pin":   "📌",
    "chain": "🔗",
    "stat":  "📈",
}

# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────
def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"bets": [], "total_win": 0, "total_loss": 0, "total_profit": 0}

def save_stats(data: dict):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def get_sport(d: dict) -> dict:
    return SPORTS.get(d.get("sport", "other"), SPORTS["other"])

# ─── ПОСТРОЕНИЕ ПОСТА: ОРДИНАР ────────────────────────────────────────────────
def build_single_post(d: dict) -> str:
    sp     = get_sport(d)
    odds   = float(d["odds"])
    amount = float(d["amount"])
    total  = round(amount * odds, 2)
    profit = round(total - amount, 2)
    author = d.get("author_name", d.get("author", "Admin"))

    return (
        f"{sp['emoji']} *{sp['name'].upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['arrow']} *{sp['event']}:* {d.get('event','')}\n"
        f"👥 *{sp['teams']}:* {d.get('fighters','')}\n"
        f"{E['fire']} *Ставка:* {d.get('bet_on','')}\n"
        f"{E['odds']} *Коэффициент:* `{odds}`\n"
        f"{E['money']} *Сумма:* `{int(amount):,}` ₽\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['crown']} *Выигрыш:* `{int(total):,}` ₽\n"
        f"💵 *Чистая прибыль:* `+{int(profit):,}` ₽\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['wait']} *Статус:* Ожидаем результат...\n\n"
        f"{E['pin']} *Автор:* {author}"
    )

# ─── ПОСТРОЕНИЕ ПОСТА: ЭКСПРЕСС ───────────────────────────────────────────────
def build_express_post(d: dict) -> str:
    legs       = d["legs"]
    amount     = float(d["amount"])
    total_odds = round(prod(float(leg["odds"]) for leg in legs), 2)
    total      = round(amount * total_odds, 2)
    profit     = round(total - amount, 2)
    author     = d.get("author_name", d.get("author", "Admin"))
    count      = len(legs)

    legs_text = ""
    for i, leg in enumerate(legs, 1):
        sp_leg = SPORTS.get(leg.get("sport", "other"), SPORTS["other"])
        legs_text += f"  `{i}.` {sp_leg['emoji']} {leg['name']} — к`{leg['odds']}`\n"

    return (
        f"{E['chain']} *ЭКСПРЕСС | {count} события*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['fire']} *События:*\n"
        f"{legs_text}"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['odds']} *Суммарный коэф:* `{total_odds}`\n"
        f"{E['money']} *Сумма ставки:* `{int(amount):,}` ₽\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['crown']} *Выигрыш:* `{int(total):,}` ₽\n"
        f"💵 *Чистая прибыль:* `+{int(profit):,}` ₽\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['wait']} *Статус:* Ожидаем результат...\n\n"
        f"{E['pin']} *Автор:* {author}"
    )

def build_post(d: dict) -> str:
    if d.get("bet_type") == "express":
        return build_express_post(d)
    return build_single_post(d)

# ─── КЛАВИАТУРА ВЫБОРА СПОРТА ─────────────────────────────────────────────────
def sport_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 UFC / MMA",  callback_data="sport_ufc"),
            InlineKeyboardButton("⚽ Футбол",      callback_data="sport_football"),
        ],
        [
            InlineKeyboardButton("🏒 Хоккей",     callback_data="sport_hockey"),
            InlineKeyboardButton("🏀 Баскетбол",  callback_data="sport_basketball"),
        ],
        [
            InlineKeyboardButton("🎾 Теннис",     callback_data="sport_tennis"),
            InlineKeyboardButton("🥋 Бокс",       callback_data="sport_boxing"),
        ],
        [
            InlineKeyboardButton("🏆 Другое",     callback_data="sport_other"),
        ],
    ])

# ─── СТАРТ ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У тебя нет доступа к боту.")
        return

    kb = [[InlineKeyboardButton("➕ Новая ставка", callback_data="new_bet")]]
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        "Публикую ставки в канал @ROSTOVBETS.\n"
        "Поддерживаю UFC, футбол, хоккей и другие виды спорта 🔥",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ─── ИНИЦИАЛИЗАЦИЯ НОВОЙ СТАВКИ ───────────────────────────────────────────────
def _init_data(ctx, user):
    ctx.user_data.clear()
    ctx.user_data["author_name"] = user.first_name
    ctx.user_data["author_id"]   = user.id

async def _ask_sport(obj):
    await obj.reply_text(
        "🏆 *Новая ставка — Шаг 1*\n\nВыбери вид спорта:",
        parse_mode="Markdown",
        reply_markup=sport_keyboard()
    )
    return SPORT

async def new_bet_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    _init_data(ctx, update.effective_user)
    return await _ask_sport(update.message)

async def new_bet_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    _init_data(ctx, query.from_user)
    return await _ask_sport(query.message)

# ─── ВЫБОР СПОРТА ─────────────────────────────────────────────────────────────
async def choose_sport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sport_key = query.data.replace("sport_", "")
    ctx.user_data["sport"] = sport_key
    sp = SPORTS[sport_key]

    kb = [[
        InlineKeyboardButton("🎯 Ординар",  callback_data="type_single"),
        InlineKeyboardButton("🔗 Экспресс", callback_data="type_express"),
    ]]
    await query.message.reply_text(
        f"{sp['emoji']} *{sp['name']} — Шаг 2*\n\nВыбери тип ставки:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return BET_TYPE

# ─── ВЫБОР ТИПА СТАВКИ ────────────────────────────────────────────────────────
async def choose_bet_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sp = get_sport(ctx.user_data)

    if query.data == "type_single":
        ctx.user_data["bet_type"] = "single"
        await query.message.reply_text(
            f"{sp['emoji']} *Шаг 3 — {sp['event']}*\n\n"
            f"Напиши название {sp['event'].lower()}а/лиги.\n"
            f"_Например:_ `{'UFC 314' if ctx.user_data['sport']=='ufc' else 'Лига чемпионов' if ctx.user_data['sport']=='football' else sp['event']}`",
            parse_mode="Markdown"
        )
        return EVENT

    else:
        ctx.user_data["bet_type"] = "express"
        ctx.user_data["legs"]     = []
        await query.message.reply_text(
            f"🔗 *Экспресс — Сколько событий?*\n\n"
            f"Напиши количество событий в экспрессе.\n"
            f"_Например:_ `3`",
            parse_mode="Markdown"
        )
        return EXPRESS_COUNT

# ══════════════════════════════════════════════════════════════════════════════
#  ОРДИНАР
# ══════════════════════════════════════════════════════════════════════════════

async def get_event(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sp = get_sport(ctx.user_data)
    ctx.user_data["event"] = update.message.text.strip()
    await update.message.reply_text(
        f"👥 *Шаг 4 — {sp['teams']}*\n\n"
        f"Напиши {sp['teams'].lower()}.\n"
        f"_Например:_ `{'Махачев vs Оливейра' if ctx.user_data['sport'] in ('ufc','boxing') else 'Реал Мадрид vs Барселона'}`",
        parse_mode="Markdown"
    )
    return FIGHTERS

async def get_fighters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["fighters"] = update.message.text.strip()
    await update.message.reply_text(
        f"🎯 *Шаг 5 — Твоя ставка*\n\n"
        f"На что ставишь?\n"
        f"_Например:_ `Победа Махачева` или `Тотал больше 2.5`",
        parse_mode="Markdown"
    )
    return BET_ON

async def get_bet_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["bet_on"] = update.message.text.strip()
    await update.message.reply_text(
        f"📊 *Шаг 6 — Коэффициент и сумма*\n\n"
        f"Напиши через пробел: `коэф сумма`\n"
        f"_Например:_ `1.85 5000`",
        parse_mode="Markdown"
    )
    return SINGLE_ODDS

async def get_single_odds_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().replace(",", ".").split()
    if len(parts) != 2:
        await update.message.reply_text("⚠️ Два числа через пробел: `1.85 5000`", parse_mode="Markdown")
        return SINGLE_ODDS
    try:
        odds   = float(parts[0])
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text("⚠️ Некорректные числа, попробуй ещё раз.", parse_mode="Markdown")
        return SINGLE_ODDS

    ctx.user_data["odds"]   = str(odds)
    ctx.user_data["amount"] = str(amount)
    await update.message.reply_text(
        "📸 *Шаг 7 — Фото*\n\nОтправь фото или /skip чтобы пропустить.",
        parse_mode="Markdown"
    )
    return PHOTO

# ══════════════════════════════════════════════════════════════════════════════
#  ЭКСПРЕСС
# ══════════════════════════════════════════════════════════════════════════════

async def get_express_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 2 or int(text) > 20:
        await update.message.reply_text("⚠️ Введи число от 2 до 20.", parse_mode="Markdown")
        return EXPRESS_COUNT
    ctx.user_data["express_total"] = int(text)
    ctx.user_data["legs"]          = []
    await update.message.reply_text(
        f"💰 *Экспресс — Сумма ставки*\n\nСколько ставишь на весь экспресс?\n_Например:_ `3000`",
        parse_mode="Markdown"
    )
    return EXPRESS_AMOUNT

async def get_express_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("⚠️ Введи сумму числом, например `3000`", parse_mode="Markdown")
        return EXPRESS_AMOUNT
    ctx.user_data["amount"] = str(amount)
    return await _ask_next_leg(update, ctx)

async def _ask_next_leg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    legs  = ctx.user_data["legs"]
    total = ctx.user_data["express_total"]
    num   = len(legs) + 1

    # Клавиатура выбора спорта для этого события
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 UFC/MMA",    callback_data=f"legsp_ufc"),
            InlineKeyboardButton("⚽ Футбол",     callback_data=f"legsp_football"),
        ],
        [
            InlineKeyboardButton("🏒 Хоккей",    callback_data=f"legsp_hockey"),
            InlineKeyboardButton("🏀 Баскетбол", callback_data=f"legsp_basketball"),
        ],
        [
            InlineKeyboardButton("🎾 Теннис",    callback_data=f"legsp_tennis"),
            InlineKeyboardButton("🥋 Бокс",      callback_data=f"legsp_boxing"),
        ],
        [
            InlineKeyboardButton("🏆 Другое",    callback_data=f"legsp_other"),
        ],
    ])
    await update.message.reply_text(
        f"🔗 *Событие {num} из {total} — Вид спорта:*",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return EXPRESS_LEG

async def get_express_leg_sport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sport_key = query.data.replace("legsp_", "")
    ctx.user_data["current_leg_sport"] = sport_key
    sp = SPORTS[sport_key]

    legs  = ctx.user_data["legs"]
    total = ctx.user_data["express_total"]
    num   = len(legs) + 1

    await query.message.reply_text(
        f"{sp['emoji']} *Событие {num} из {total}*\n\n"
        f"Напиши через `|`:\n"
        f"`Название ставки | коэф`\n\n"
        f"_Например:_ `{'Махачев победа | 1.72' if sport_key in ('ufc','boxing') else 'Реал Мадрид победа | 1.85'}`",
        parse_mode="Markdown"
    )
    return EXPRESS_LEG

async def get_express_leg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return await get_express_leg_sport(update, ctx)

    text = update.message.text.strip()

    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Формат: `Название | коэф`", parse_mode="Markdown")
            return EXPRESS_LEG
        name_part, odds_part = parts[0], parts[1]
    else:
        parts = text.rsplit(" ", 1)
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Формат: `Название | коэф`", parse_mode="Markdown")
            return EXPRESS_LEG
        name_part, odds_part = parts[0], parts[1]

    try:
        odds_val = float(odds_part.replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Коэффициент должен быть числом, например `1.72`", parse_mode="Markdown")
        return EXPRESS_LEG

    sport_key = ctx.user_data.pop("current_leg_sport", "other")
    ctx.user_data["legs"].append({
        "name":  name_part,
        "odds":  str(odds_val),
        "sport": sport_key,
    })

    legs  = ctx.user_data["legs"]
    total = ctx.user_data["express_total"]

    if len(legs) < total:
        return await _ask_next_leg(update, ctx)

    # Все ноги собраны
    total_odds = round(prod(float(l["odds"]) for l in legs), 2)
    amount     = float(ctx.user_data["amount"])
    win        = round(amount * total_odds, 2)
    profit     = round(win - amount, 2)
    ctx.user_data["odds"] = str(total_odds)

    legs_preview = "\n".join(
        f"  {SPORTS.get(l.get('sport','other'),SPORTS['other'])['emoji']} {l['name']} — к{l['odds']}"
        for l in legs
    )
    await update.message.reply_text(
        f"✔️ *Все события добавлены!*\n\n"
        f"{legs_preview}\n\n"
        f"📊 Суммарный коэф: *{total_odds}*\n"
        f"💰 Ставка: *{int(amount):,} ₽*\n"
        f"👑 Выигрыш: *{int(win):,} ₽* (+{int(profit):,} ₽)\n\n"
        f"📸 Отправь фото или /skip чтобы пропустить.",
        parse_mode="Markdown"
    )
    return PHOTO

# ══════════════════════════════════════════════════════════════════════════════
#  ФОТО + ПРЕВЬЮ + ПУБЛИКАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def get_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["photo_id"] = update.message.photo[-1].file_id if update.message.photo else None
    return await show_preview(update, ctx)

async def skip_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["photo_id"] = None
    return await show_preview(update, ctx)

async def show_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d        = ctx.user_data
    post_txt = build_post(d)
    kb = [[
        InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
        InlineKeyboardButton("❌ Отменить",     callback_data="cancel"),
    ]]
    await update.message.reply_text("👀 *Превью поста:*\n\n" + post_txt, parse_mode="Markdown")
    if d.get("photo_id"):
        await update.message.reply_photo(photo=d["photo_id"], caption="📎 Фото к посту")
    await update.message.reply_text("Всё выглядит хорошо? Публикуем?", reply_markup=InlineKeyboardMarkup(kb))
    return PREVIEW

async def publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d        = ctx.user_data
    post_txt = build_post(d)
    photo_id = d.get("photo_id")

    if photo_id:
        msg = await ctx.bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=post_txt, parse_mode="Markdown")
    else:
        msg = await ctx.bot.send_message(chat_id=CHANNEL_ID, text=post_txt, parse_mode="Markdown")

    stats = load_stats()
    record = {
        "channel_msg_id": msg.message_id,
        "bet_type":  d.get("bet_type", "single"),
        "sport":     d.get("sport", "other"),
        "odds":      float(d["odds"]),
        "amount":    float(d["amount"]),
        "author":    d.get("author_name", "?"),
        "date":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "result":    "pending",
        "has_photo": bool(photo_id),
        "event":     d.get("event", ""),
        "fighters":  d.get("fighters", ""),
        "bet_on":    d.get("bet_on", ""),
        "legs":      d.get("legs", []),
    }
    stats["bets"].append(record)
    save_stats(stats)

    kb = [[
        InlineKeyboardButton("✅ Выиграл",  callback_data=f"win_{msg.message_id}"),
        InlineKeyboardButton("❌ Проиграл", callback_data=f"loss_{msg.message_id}"),
    ]]
    await query.message.reply_text(
        "🚀 *Опубликовано!*\n\nКогда узнаешь результат — отметь его ниже.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ConversationHandler.END

# ─── ОТМЕНА ───────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("❌ Публикация отменена.")
    else:
        await update.message.reply_text("❌ Отменено. /stavka — начать заново.")
    ctx.user_data.clear()
    return ConversationHandler.END

# ─── РЕЗУЛЬТАТ ────────────────────────────────────────────────────────────────
async def set_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    parts  = query.data.split("_")
    result = parts[0]
    msg_id = int(parts[1])

    stats = load_stats()
    bet   = next((b for b in stats["bets"] if b["channel_msg_id"] == msg_id), None)

    if not bet:
        await query.message.reply_text("⚠️ Ставка не найдена.")
        return
    if bet["result"] != "pending":
        await query.message.reply_text("ℹ️ Результат уже отмечен.")
        return

    amount = bet["amount"]
    odds   = bet["odds"]
    profit = round(amount * odds - amount, 2)

    if result == "win":
        bet["result"] = "win"
        stats["total_win"]    += 1
        stats["total_profit"] += profit
        status_line = f"✅ *Статус:* ВЫИГРАЛИ | +{int(profit):,} ₽"
        reply_text  = f"✅ Отмечено как *выигрыш* (+{int(profit):,} ₽)"
    else:
        bet["result"] = "loss"
        stats["total_loss"]   += 1
        stats["total_profit"] -= amount
        status_line = f"❌ *Статус:* ПРОИГРАЛИ | -{int(amount):,} ₽"
        reply_text  = f"❌ Отмечено как *проигрыш* (-{int(amount):,} ₽)"

    save_stats(stats)

    try:
        updated = build_post(bet).replace(
            f"{E['wait']} *Статус:* Ожидаем результат...", status_line
        )
        if bet.get("has_photo"):
            await ctx.bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=msg_id, caption=updated, parse_mode="Markdown")
        else:
            await ctx.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=msg_id, text=updated, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Не удалось обновить пост: {e}")

    await query.message.reply_text(reply_text, parse_mode="Markdown")

# ─── /bets — АКТИВНЫЕ СТАВКИ ──────────────────────────────────────────────────
async def bets_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    stats   = load_stats()
    pending = [b for b in stats["bets"] if b["result"] == "pending"]

    if not pending:
        await update.message.reply_text("⏳ *Активных ставок нет*", parse_mode="Markdown")
        return

    text = f"⏳ *Активные ставки ({len(pending)}):*\n━━━━━━━━━━━━━━━━━━━\n"
    for b in pending:
        sp     = SPORTS.get(b.get("sport", "other"), SPORTS["other"])
        amount = int(b["amount"])
        win    = int(round(b["amount"] * b["odds"]))
        if b.get("bet_type") == "express":
            legs_str = "\n".join(f"    • {SPORTS.get(l.get('sport','other'),SPORTS['other'])['emoji']} {l['name']} к{l['odds']}" for l in b.get("legs", []))
            text += f"🔗 *Экспресс*\n{legs_str}\n📊 к`{b['odds']}` | 💰 `{amount:,}` ₽ → 👑 `{win:,}` ₽\n📌 {b.get('author','?')} | {b.get('date','')}\n━━━━━━━━━━━━━━━━━━━\n"
        else:
            text += f"{sp['emoji']} *{b.get('event','')}* — {b.get('bet_on','')}\n📊 к`{b['odds']}` | 💰 `{amount:,}` ₽ → 👑 `{win:,}` ₽\n📌 {b.get('author','?')} | {b.get('date','')}\n━━━━━━━━━━━━━━━━━━━\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /history — ИСТОРИЯ ───────────────────────────────────────────────────────
async def history_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    stats    = load_stats()
    finished = [b for b in stats["bets"] if b["result"] != "pending"]

    if not finished:
        await update.message.reply_text("📊 *История пуста*", parse_mode="Markdown")
        return

    last = list(reversed(finished[-20:]))
    text = f"📊 *История ставок (последние {len(last)}):*\n━━━━━━━━━━━━━━━━━━━\n"
    for b in last:
        icon   = "✅" if b["result"] == "win" else "❌"
        sp     = SPORTS.get(b.get("sport", "other"), SPORTS["other"])
        amount = int(b["amount"])
        profit = int(round(b["amount"] * b["odds"] - b["amount"]))
        res_str = f"+{profit:,} ₽" if b["result"] == "win" else f"-{amount:,} ₽"
        if b.get("bet_type") == "express":
            text += f"{icon} 🔗 *Экспресс* | к`{b['odds']}` | 💰`{amount:,}` ₽ | {res_str}\n📌 {b.get('author','?')} | {b.get('date','')}\n━━━━━━━━━━━━━━━━━━━\n"
        else:
            text += f"{icon} {sp['emoji']} *{b.get('event','')}* — {b.get('bet_on','')}\nк`{b['odds']}` | 💰`{amount:,}` ₽ | {res_str}\n📌 {b.get('author','?')} | {b.get('date','')}\n━━━━━━━━━━━━━━━━━━━\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /stats ────────────────────────────────────────────────────────────────────
async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    s       = load_stats()
    bets    = s["bets"]
    total   = len(bets)
    wins    = s["total_win"]
    losses  = s["total_loss"]
    pending = total - wins - losses
    profit  = s["total_profit"]
    wr      = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0
    p_str   = f"+{int(profit):,} ₽" if profit >= 0 else f"{int(profit):,} ₽"
    p_em    = "📈" if profit >= 0 else "📉"

    text = (
        f"{E['stat']} *Статистика @ROSTOVBETS*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Всего ставок: *{total}*\n"
        f"✅ Выиграно: *{wins}*\n"
        f"❌ Проиграно: *{losses}*\n"
        f"⏳ В ожидании: *{pending}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Винрейт: *{wr}%*\n"
        f"{p_em} Итого прибыль: *{p_str}*\n"
    )
    if bets:
        text += "\n━━━━━━━━━━━━━━━━━━━\n📌 *Последние ставки:*\n"
        for b in reversed(bets[-5:]):
            icon = "✅" if b["result"] == "win" else ("❌" if b["result"] == "loss" else "⏳")
            sp   = SPORTS.get(b.get("sport", "other"), SPORTS["other"])
            label = b.get("bet_on") or "Экспресс"
            text += f"{icon} {sp['emoji']} {label} | к{b['odds']}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /help ────────────────────────────────────────────────────────────────────
async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды бота:*\n\n"
        "/stavka — ➕ новая ставка\n"
        "/bets — ⏳ активные ставки\n"
        "/history — 📊 история ставок\n"
        "/stats — 📈 статистика\n"
        "/help — справка\n"
        "/cancel — отменить ввод",
        parse_mode="Markdown"
    )

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("stavka", new_bet_start),
            CallbackQueryHandler(new_bet_callback, pattern="^new_bet$"),
        ],
        states={
            SPORT:    [CallbackQueryHandler(choose_sport, pattern="^sport_")],
            BET_TYPE: [CallbackQueryHandler(choose_bet_type, pattern="^type_")],
            # ординар
            EVENT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_event)],
            FIGHTERS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fighters)],
            BET_ON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bet_on)],
            SINGLE_ODDS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_single_odds_amount)],
            # экспресс
            EXPRESS_COUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_count)],
            EXPRESS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_amount)],
            EXPRESS_LEG: [
                CallbackQueryHandler(get_express_leg_sport, pattern="^legsp_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_leg),
            ],
            # общее
            PHOTO: [
                MessageHandler(filters.PHOTO, get_photo),
                CommandHandler("skip", skip_photo),
            ],
            PREVIEW: [
                CallbackQueryHandler(publish, pattern="^publish$"),
                CallbackQueryHandler(cancel,  pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("stats",   stats_command))
    app.add_handler(CommandHandler("bets",    bets_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(set_result, pattern=r"^(win|loss)_\d+$"))

    start_health_server()
    start_self_ping()
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
