import os
import json
import logging
import threading
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

# ─── KEEP-ALIVE (чтобы Render не усыплял бота) ────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # не спамим в логи

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def start_health_server():
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info("Health server запущен ✅")

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ROSTOVBETS")
ADMIN_IDS  = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x]
STATS_FILE = "stats.json"

# ─── ШАГИ ДИАЛОГА ─────────────────────────────────────────────────────────────
(
    BET_TYPE,           # ординар / экспресс
    # --- ординар ---
    EVENT, FIGHTERS, BET_ON, SINGLE_ODDS, SINGLE_AMOUNT,
    # --- экспресс ---
    EXPRESS_COUNT, EXPRESS_AMOUNT, EXPRESS_LEG,
    # --- общее ---
    PHOTO, PREVIEW,
) = range(11)

# ─── ЭМОДЗИ ───────────────────────────────────────────────────────────────────
E = {
    "ufc":   "🥊",
    "fire":  "🔥",
    "money": "💰",
    "odds":  "📊",
    "arrow": "➡️",
    "wait":  "⏳",
    "crown": "👑",
    "win":   "✅",
    "loss":  "❌",
    "stat":  "📈",
    "pin":   "📌",
    "acc":   "🎯",
    "chain": "🔗",
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

# ─── ПОСТРОЕНИЕ ПОСТА: ОРДИНАР ────────────────────────────────────────────────
def build_single_post(d: dict) -> str:
    odds   = float(d["odds"])
    amount = float(d["amount"])
    total  = round(amount * odds, 2)
    profit = round(total - amount, 2)
    author = d.get("author_name", "Admin")

    return (
        f"{E['ufc']} *{d['event']}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E['arrow']} *Бой:* {d['fighters']}\n"
        f"{E['fire']} *Ставка:* {d['bet_on']}\n"
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
    legs   = d["legs"]           # [{"name": ..., "odds": ...}, ...]
    amount = float(d["amount"])
    total_odds = round(prod(float(leg["odds"]) for leg in legs), 2)
    total  = round(amount * total_odds, 2)
    profit = round(total - amount, 2)
    author = d.get("author_name", "Admin")

    legs_text = ""
    for i, leg in enumerate(legs, 1):
        legs_text += f"  `{i}.` {leg['name']} — к`{leg['odds']}`\n"

    return (
        f"{E['chain']} *ЭКСПРЕСС | {d.get('express_label', '')}*\n"
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

# ─── СТАРТ ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У тебя нет доступа к боту.")
        return ConversationHandler.END

    kb = [[InlineKeyboardButton("➕ Новая ставка", callback_data="new_bet")]]
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        "Я помогу публиковать ставки в канал @ROSTOVBETS.\n"
        "Всё будет красиво оформлено 🔥",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ─── ИНИЦИАЛИЗАЦИЯ НОВОЙ СТАВКИ ───────────────────────────────────────────────
def _init_data(ctx, user):
    ctx.user_data.clear()
    ctx.user_data["author_name"] = user.first_name
    ctx.user_data["author_id"]   = user.id

async def _ask_bet_type(obj):
    """obj — message или callback_query.message"""
    kb = [[
        InlineKeyboardButton("🎯 Ординар",  callback_data="type_single"),
        InlineKeyboardButton("🔗 Экспресс", callback_data="type_express"),
    ]]
    await obj.reply_text(
        "🥊 *Новая ставка*\n\nВыбери тип ставки:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return BET_TYPE

async def new_bet_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    _init_data(ctx, update.effective_user)
    return await _ask_bet_type(update.message)

async def new_bet_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    _init_data(ctx, query.from_user)
    return await _ask_bet_type(query.message)

# ─── ВЫБОР ТИПА СТАВКИ ────────────────────────────────────────────────────────
async def choose_bet_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data  # "type_single" или "type_express"

    if choice == "type_single":
        ctx.user_data["bet_type"] = "single"
        await query.message.reply_text(
            "🥊 *Шаг 1 — Событие*\n\n"
            "Напиши название турнира/события.\n"
            "_Например:_ `UFC 314`",
            parse_mode="Markdown"
        )
        return EVENT

    else:  # express
        ctx.user_data["bet_type"] = "express"
        await query.message.reply_text(
            "🔗 *Экспресс — Сколько событий?*\n\n"
            "Напиши количество событий в экспрессе.\n"
            "_Например:_ `3`",
            parse_mode="Markdown"
        )
        return EXPRESS_COUNT

# ══════════════════════════════════════════════════════════════════════════════
#  ОРДИНАР
# ══════════════════════════════════════════════════════════════════════════════

async def get_event(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["event"] = update.message.text.strip()
    await update.message.reply_text(
        "👥 *Шаг 2 — Бойцы*\n\n"
        "Напиши участников боя.\n"
        "_Например:_ `Махачев vs Оливейра`",
        parse_mode="Markdown"
    )
    return FIGHTERS

async def get_fighters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["fighters"] = update.message.text.strip()
    await update.message.reply_text(
        "🎯 *Шаг 3 — Твоя ставка*\n\n"
        "На кого/что ставишь?\n"
        "_Например:_ `Махачев (победа)`",
        parse_mode="Markdown"
    )
    return BET_ON

async def get_bet_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["bet_on"] = update.message.text.strip()
    await update.message.reply_text(
        "📊 *Шаг 4 — Коэффициент и сумма*\n\n"
        "Напиши через пробел: `коэф сумма`\n"
        "_Например:_ `1.85 5000`",
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
        "📸 *Шаг 5 — Фото*\n\n"
        "Отправь фото или /skip чтобы пропустить.",
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
        f"💰 *Экспресс — Сумма ставки*\n\n"
        f"Сколько ставишь на весь экспресс?\n"
        f"_Например:_ `3000`",
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

    await update.message.reply_text(
        f"🔗 *Событие {num} из {total}*\n\n"
        f"Напиши в одном сообщении через пробел:\n"
        f"`Название ставки | коэф`\n\n"
        f"_Например:_ `Махачев (победа) | 1.72`\n"
        f"или: `Перейра нокаут | 2.10`",
        parse_mode="Markdown"
    )
    return EXPRESS_LEG

async def get_express_leg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Поддерживаем разделитель | или последнее слово как коэф
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) != 2:
            await update.message.reply_text(
                "⚠️ Формат: `Название | коэф`\n_Например:_ `Махачев (победа) | 1.72`",
                parse_mode="Markdown"
            )
            return EXPRESS_LEG
        name_part, odds_part = parts[0], parts[1]
    else:
        # последнее слово = коэф
        parts = text.rsplit(" ", 1)
        if len(parts) != 2:
            await update.message.reply_text(
                "⚠️ Формат: `Название | коэф`\n_Например:_ `Махачев (победа) | 1.72`",
                parse_mode="Markdown"
            )
            return EXPRESS_LEG
        name_part, odds_part = parts[0], parts[1]

    try:
        odds_val = float(odds_part.replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Коэффициент должен быть числом, например `1.72`", parse_mode="Markdown")
        return EXPRESS_LEG

    ctx.user_data["legs"].append({"name": name_part, "odds": str(odds_val)})

    legs  = ctx.user_data["legs"]
    total = ctx.user_data["express_total"]

    if len(legs) < total:
        return await _ask_next_leg(update, ctx)

    # Все ноги собраны — считаем суммарный коэф и просим фото
    total_odds = round(prod(float(l["odds"]) for l in legs), 2)
    amount     = float(ctx.user_data["amount"])
    win        = round(amount * total_odds, 2)
    profit     = round(win - amount, 2)

    # Подпись для поста (лейбл: "UFC 314 + Bellator 300")
    ctx.user_data["odds"]          = str(total_odds)
    ctx.user_data["express_label"] = f"{total}-событийный экспресс"

    legs_preview = "\n".join(f"  {i}. {l['name']} — к{l['odds']}" for i, l in enumerate(legs, 1))
    await update.message.reply_text(
        f"✔️ *Все события добавлены!*\n\n"
        f"{legs_preview}\n\n"
        f"📊 Суммарный коэф: *{total_odds}*\n"
        f"💰 Ставка: *{int(amount):,} ₽*\n"
        f"👑 Выигрыш: *{int(win):,} ₽* (+{int(profit):,} ₽)\n\n"
        f"📸 Теперь отправь фото или /skip чтобы пропустить.",
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

    await update.message.reply_text(
        "👀 *Превью поста:*\n\n" + post_txt,
        parse_mode="Markdown"
    )
    if d.get("photo_id"):
        await update.message.reply_photo(photo=d["photo_id"], caption="📎 Фото к посту")

    await update.message.reply_text(
        "Всё выглядит хорошо? Публикуем?",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return PREVIEW

async def publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    d        = ctx.user_data
    post_txt = build_post(d)
    photo_id = d.get("photo_id")

    if photo_id:
        msg = await ctx.bot.send_photo(
            chat_id=CHANNEL_ID, photo=photo_id,
            caption=post_txt, parse_mode="Markdown"
        )
    else:
        msg = await ctx.bot.send_message(
            chat_id=CHANNEL_ID, text=post_txt, parse_mode="Markdown"
        )

    # Сохраняем
    stats = load_stats()
    record = {
        "channel_msg_id": msg.message_id,
        "bet_type":  d.get("bet_type", "single"),
        "odds":      float(d["odds"]),
        "amount":    float(d["amount"]),
        "author":    d.get("author_name", "?"),
        "date":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "result":    "pending",
        "has_photo": bool(photo_id),
        # ординар
        "event":    d.get("event", ""),
        "fighters": d.get("fighters", ""),
        "bet_on":   d.get("bet_on", ""),
        # экспресс
        "legs":     d.get("legs", []),
        "express_label": d.get("express_label", ""),
    }
    stats["bets"].append(record)
    save_stats(stats)

    kb = [[
        InlineKeyboardButton("✅ Выиграл",  callback_data=f"win_{msg.message_id}"),
        InlineKeyboardButton("❌ Проиграл", callback_data=f"loss_{msg.message_id}"),
    ]]
    await query.message.reply_text(
        "🚀 *Опубликовано!*\n\nКогда узнаешь результат — отметь его ниже.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
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

    result, msg_id = query.data.split("_")[0], int(query.data.split("_")[1])
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

    # Обновляем пост в канале
    try:
        updated = build_post(bet).replace(
            f"{E['wait']} *Статус:* Ожидаем результат...", status_line
        )
        if bet.get("has_photo"):
            await ctx.bot.edit_message_caption(
                chat_id=CHANNEL_ID, message_id=msg_id,
                caption=updated, parse_mode="Markdown"
            )
        else:
            await ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID, message_id=msg_id,
                text=updated, parse_mode="Markdown"
            )
    except Exception as e:
        logger.warning(f"Не удалось обновить пост: {e}")

    await query.message.reply_text(reply_text, parse_mode="Markdown")

# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────
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
            label = b.get("express_label") or b.get("bet_on", "?")
            text += f"{icon} {'🔗' if b['bet_type']=='express' else '🎯'} {label} | к{b['odds']}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── АКТИВНЫЕ СТАВКИ (/bets) ──────────────────────────────────────────────────
async def bets_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    stats   = load_stats()
    pending = [b for b in stats["bets"] if b["result"] == "pending"]

    if not pending:
        await update.message.reply_text(
            "⏳ *Активных ставок нет*\n\nВсе ставки уже завершены.",
            parse_mode="Markdown"
        )
        return

    text = f"⏳ *Активные ставки ({len(pending)}):*\n━━━━━━━━━━━━━━━━━━━\n"
    for b in pending:
        bet_type = b.get("bet_type", "single")
        amount   = int(b["amount"])
        odds     = b["odds"]
        win      = int(round(b["amount"] * b["odds"]))
        date     = b.get("date", "?")
        author   = b.get("author", "?")

        if bet_type == "express":
            label = b.get("express_label", "Экспресс")
            legs  = b.get("legs", [])
            legs_str = "\n".join(f"    • {l['name']} к{l['odds']}" for l in legs)
            text += (
                f"🔗 *{label}*\n"
                f"{legs_str}\n"
                f"📊 Суммарный коэф: `{odds}` | 💰 `{amount:,}` ₽ → 👑 `{win:,}` ₽\n"
                f"📌 {author} | 🗓 {date}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
            )
        else:
            event   = b.get("event", "?")
            bet_on  = b.get("bet_on", "?")
            text += (
                f"🎯 *{event}*\n"
                f"    • {bet_on}\n"
                f"📊 Коэф: `{odds}` | 💰 `{amount:,}` ₽ → 👑 `{win:,}` ₽\n"
                f"📌 {author} | 🗓 {date}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
            )

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── ИСТОРИЯ СТАВОК (/history) ────────────────────────────────────────────────
async def history_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    stats    = load_stats()
    finished = [b for b in stats["bets"] if b["result"] != "pending"]

    if not finished:
        await update.message.reply_text(
            "📊 *История пуста*\n\nЗавершённых ставок ещё нет.",
            parse_mode="Markdown"
        )
        return

    # Показываем последние 20
    last = list(reversed(finished[-20:]))
    text = f"📊 *История ставок (последние {len(last)}):*\n━━━━━━━━━━━━━━━━━━━\n"

    for b in last:
        result   = b["result"]
        icon     = "✅" if result == "win" else "❌"
        bet_type = b.get("bet_type", "single")
        amount   = int(b["amount"])
        odds     = b["odds"]
        profit   = int(round(b["amount"] * b["odds"] - b["amount"]))
        date     = b.get("date", "?")
        author   = b.get("author", "?")

        if result == "win":
            result_str = f"+{profit:,} ₽"
        else:
            result_str = f"-{amount:,} ₽"

        if bet_type == "express":
            label = b.get("express_label", "Экспресс")
            text += (
                f"{icon} 🔗 *{label}*\n"
                f"📊 к`{odds}` | 💰 `{amount:,}` ₽ | {result_str}\n"
                f"📌 {author} | 🗓 {date}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
            )
        else:
            event  = b.get("event", "?")
            bet_on = b.get("bet_on", "?")
            text += (
                f"{icon} 🎯 *{event}* — {bet_on}\n"
                f"📊 к`{odds}` | 💰 `{amount:,}` ₽ | {result_str}\n"
                f"📌 {author} | 🗓 {date}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
            )

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── ПОМОЩЬ ───────────────────────────────────────────────────────────────────
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
            BET_TYPE:       [CallbackQueryHandler(choose_bet_type, pattern="^type_")],
            # ординар
            EVENT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, get_event)],
            FIGHTERS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fighters)],
            BET_ON:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bet_on)],
            SINGLE_ODDS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_single_odds_amount)],
            # экспресс
            EXPRESS_COUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_count)],
            EXPRESS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_amount)],
            EXPRESS_LEG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_express_leg)],
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
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
