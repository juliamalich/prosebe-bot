"""
Telegram bot for the "Заяви про себе" funnel.

Flow:
  /start  -> welcome message + PDF guide
  +30 min -> photo + Module 1 offer for 129 грн
             (buttons: buy 129 / about / buy full 699 / question -> @ym2812)
  /about  -> full program description + buy 699 грн
  payment -> user gets a personal one-time invite link to the closed channel

Payments (priority order):
  1. WayForPay API (WFP_MERCHANT + WFP_SECRET set) -> bot creates an invoice,
     WayForPay calls our webhook on success, access granted automatically.
  2. Native Telegram Payments (PROVIDER_TOKEN set).
  3. Static payment links (PAY_URL_129 / PAY_URL_699) + manual /grant.

Closed channel:
  Set CHANNEL_ID and make the bot an ADMIN of the channel with the
  "Invite users via link" permission -> personal single-use invite links.
  Otherwise the static CHANNEL_LINK is sent.

Admin commands (ADMIN_CHAT_ID only):
  /grant <id|@username>    mark as paid + send personal channel invite
  /link <id|@username>     resend a fresh invite link
  /revoke <id|@username>   remove from channel + mark as unpaid
  /users                   last 20 users with status
  /broadcast               message to all users (text or reply-to-copy)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import httpx
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("zayavy-bot")

# ------------------------------------------------------------------ config

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))            # closed channel id, e.g. -1001234567890
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")              # static fallback invite link

# --- WayForPay (option 1, recommended if you already have a WFP merchant)
WFP_MERCHANT = os.getenv("WFP_MERCHANT", "")              # merchantAccount (merchant login)
WFP_SECRET = os.getenv("WFP_SECRET", "")                  # SecretKey from the WFP dashboard
WFP_DOMAIN = os.getenv("WFP_DOMAIN", "")                  # site domain the merchant is registered for
PUBLIC_URL = os.getenv("PUBLIC_URL", "")                  # this service's public URL on Railway
PORT = int(os.getenv("PORT", "8080"))

# --- Native Telegram Payments (option 2)
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

# --- Static payment pages (option 3, manual /grant)
PAY_URL_129 = os.getenv("PAY_URL_129", "")
PAY_URL_699 = os.getenv("PAY_URL_699", "")

PDF_PATH = os.getenv("PDF_PATH", "assets/guide.pdf")
PHOTO_PATH = os.getenv("PHOTO_PATH", "assets/offer.jpg")
DB_PATH = os.getenv("DB_PATH", "/data/bot.db") if os.path.isdir("/data") else os.getenv("DB_PATH", "bot.db")

FOLLOWUP_MINUTES = int(os.getenv("FOLLOWUP_MINUTES", "30"))
QUESTION_URL = os.getenv("QUESTION_URL", "https://t.me/ym2812")

WFP_ENABLED = bool(WFP_MERCHANT and WFP_SECRET)

PRODUCTS = {
    "buy_129": ("Модуль 1 «Знай собі ціну» + воркбук", 129, PAY_URL_129),
    "buy_699": ("Повна програма «Заяви про себе» (4 модулі)", 699, PAY_URL_699),
}  # prices in whole hryvnias

# ------------------------------------------------------------------ texts

TEXT_WELCOME = (
    "Привіт! Це ми — Юлія та Алеся 💙\n\n"
    "Ти залишила контакт — а ми обіцяли гайд. Ось він 📎\n\n"
    "«5 шаблонів самопрезентації, що працюють у 2026»\n\n"
    "Що там:\n"
    "→ Як описувати досягнення в цифрах\n"
    "→ 30-секундна самопрезентація\n"
    "→ Як говорити про ціну без вибачень\n"
    "→ 15 фраз-замінників на нараді\n"
    "→ LinkedIn-профіль, що чіпляє\n"
    "+ бонусний чек-ліст наприкінці\n\n"
    "Якщо після прочитання захочеться більше, ніж шаблонів — "
    "на останній сторінці є деталі про програму «Заяви про себе».\n\n"
    "Гарного читання!"
)

TEXT_FOLLOWUP = (
    "Привіт! 👋\n\n"
    "Це знову Юлія Маліч та Алеся Стоковська.\n\n"
    "Якщо ти робиш багато, але це не конвертується в підвищення або визнання — "
    "почни з малого.\n\n"
    "Ми хочемо запропонувати тобі пройти Модуль 1 курсу «Заяви про себе» 🎟\n\n"
    "Відеоурок «Сформулюй власну цінність» + практичний воркбук\n\n"
    "Якщо ти впізнаєш хоч один пункт, це для тебе:\n"
    "— роблю багато, але це не конвертується в підвищення або визнання;\n"
    "— не вмію коротко і в цифрах пояснити, який у мене вплив;\n"
    "— гублюся у важливий момент і звучу слабше, ніж я є;\n"
    "— хочу зростання, але бракує структури і часу на довгі програми.\n\n"
    "Цей модуль — твій швидкий старт.\n\n"
    "Що буде всередині:\n"
    "— формула «внесок → ефект → наступний крок» (щоб говорити про себе впевнено);\n"
    "— як перестати знецінювати себе словами;\n"
    "— вправи з воркбуку, щоб одразу застосувати.\n\n"
    "⏳ Спецціна 129 грн діє тільки 3 дні.\n\n"
    "Якщо готова зробити перший крок — натискай «Оплатити» нижче 👇"
)

TEXT_ABOUT = (
    "ПРОГРАМА «ЗАЯВИ ПРО СЕБЕ»\n\n"
    "Це твій шлях до кар'єрного зростання та професійної впевненості!\n\n"
    "🎯 ДЛЯ КОГО ЦЕЙ КУРС?\n"
    "Для амбітних жінок, які хочуть:\n"
    "— Впевнено заявляти про свої досягнення\n"
    "— Подолати синдром самозванця\n"
    "— Отримати заслужене визнання на роботі\n"
    "— Побудувати успішну кар'єру\n\n"
    "📖 ЩО ВКЛЮЧАЄ ПРОГРАМА?\n\n"
    "МОДУЛЬ 1: «Знай собі ціну»\n"
    "→ Професійна самооцінка та розуміння своєї цінності\n"
    "→ Робота з обмежуючими переконаннями\n"
    "→ Постановка кар'єрних цілей\n\n"
    "МОДУЛЬ 2: «Твоя унікальна експертиза»\n"
    "→ Визначення твоїх сильних сторін\n"
    "→ Робота з синдромом самозванця\n"
    "→ Ефективна комунікація експертизи\n\n"
    "МОДУЛЬ 3: «Формулювання та комунікація цінності»\n"
    "→ Чітке позиціювання себе\n"
    "→ Комунікація з різними аудиторіями\n\n"
    "МОДУЛЬ 4: «Робота з командою та критикою»\n"
    "→ Принципи впливу та лідерства\n"
    "→ Конструктивна робота зі зворотнім зв'язком\n"
    "→ Ефективна командна взаємодія\n\n"
    "✨ ЩО ТИ ОТРИМАЄШ?\n"
    "— 4 відеолекції від менторок Юлії та Алесі\n"
    "— Workbook для кожного модуля з практичними вправами\n"
    "— Покрокові інструменти для застосування\n"
    "— Трансформацію професійної впевненості\n\n"
    "Готова змінити своє кар'єрне життя? 💪"
)

TEXT_AFTER_PAYMENT = (
    "Оплата пройшла успішно, вітаємо! 🎉\n\n"
    "Ось твій особистий доступ — приєднуйся до закритого каналу:\n{link}\n\n"
    "Лінк одноразовий, тож не пересилай його 😉 До зустрічі всередині 💙"
)

# ------------------------------------------------------------------ db

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            started_at INTEGER,
            paid INTEGER DEFAULT 0,
            f1_sent INTEGER DEFAULT 0,
            username TEXT,
            first_name TEXT
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    for col in ("username TEXT", "first_name TEXT"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return conn

def get_user(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT started_at, paid, f1_sent FROM users WHERE user_id=?", (user_id,)
        ).fetchone()

def set_paid(user_id: int, value: int):
    with db() as conn:
        conn.execute("UPDATE users SET paid=? WHERE user_id=?", (value, user_id))

def set_f1_sent(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET f1_sent=1 WHERE user_id=?", (user_id,))

def kv_get(key: str):
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else None

def kv_set(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (key, value))

def resolve_user(arg: str):
    """Accepts a numeric id or @username; returns user_id or None."""
    arg = arg.strip()
    if arg.startswith("@"):
        with db() as conn:
            row = conn.execute(
                "SELECT user_id FROM users WHERE lower(username)=lower(?)", (arg[1:],)
            ).fetchone()
        return row[0] if row else None
    try:
        return int(arg)
    except ValueError:
        return None

# ------------------------------------------------------------------ WayForPay

def wfp_sign(parts) -> str:
    s = ";".join(str(p) for p in parts)
    return hmac.new(WFP_SECRET.encode(), s.encode("utf-8"), hashlib.md5).hexdigest()

async def wfp_create_invoice(key: str, user_id: int):
    """Create a WayForPay invoice; returns (invoice_url, raw_response)."""
    title, amount, _ = PRODUCTS[key]
    ref = f"{key}-{user_id}-{int(time.time())}"
    order_date = int(time.time())
    signature = wfp_sign(
        [WFP_MERCHANT, WFP_DOMAIN, ref, order_date, amount, "UAH", title, 1, amount]
    )
    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WFP_MERCHANT,
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": WFP_DOMAIN,
        "merchantSignature": signature,
        "apiVersion": 1,
        "language": "ua",
        "serviceUrl": f"{PUBLIC_URL.rstrip('/')}/wfp" if PUBLIC_URL else "",
        "orderReference": ref,
        "orderDate": order_date,
        "amount": amount,
        "currency": "UAH",
        "orderTimeout": 86400,
        "productName": [title],
        "productPrice": [amount],
        "productCount": [1],
    }
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://api.wayforpay.com/api", json=payload)
        data = r.json()
    return data.get("invoiceUrl"), data

def _norm(v) -> str:
    """Normalize a callback value for signature verification."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)

async def wfp_webhook(request: web.Request) -> web.Response:
    """WayForPay calls this URL after every (successful) payment."""
    raw = await request.text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # WFP sometimes posts form-encoded where the whole JSON is the key
        try:
            data = json.loads(unquote_plus(raw).rstrip("=").strip())
        except Exception:
            log.error("WFP webhook: cannot parse body: %r", raw[:300])
            return web.json_response({"status": "bad request"}, status=400)

    ref = data.get("orderReference", "")
    status = data.get("transactionStatus", "")
    expected = wfp_sign(
        [_norm(data.get(f)) for f in (
            "merchantAccount", "orderReference", "amount", "currency",
            "authCode", "cardPan", "transactionStatus", "reasonCode",
        )]
    )
    valid = expected == data.get("merchantSignature")
    tg_app: Application = request.app["tg"]

    if status == "Approved" and ref:
        if kv_get(f"wfp_{ref}") is None:  # dedupe: WFP retries callbacks
            kv_set(f"wfp_{ref}", "1")
            try:
                key, uid_str, _ = ref.split("-")
                uid = int(uid_str)
            except ValueError:
                key, uid = ref, 0
            if valid and uid:
                await send_access(tg_app.bot, uid)
                if ADMIN_CHAT_ID:
                    await tg_app.bot.send_message(
                        ADMIN_CHAT_ID,
                        f"💰 WayForPay: {data.get('amount')} {data.get('currency')}, "
                        f"продукт: {key}, користувач id {uid}. Доступ надіслано автоматично.",
                    )
            elif ADMIN_CHAT_ID:
                await tg_app.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"⚠️ WayForPay-оплата ({data.get('amount')} грн, замовлення {ref}), "
                    f"але підпис не пройшов перевірку. Перевірте оплату в кабінеті WFP "
                    f"і за потреби надайте доступ вручну: /grant {uid}",
                )

    now = int(time.time())
    return web.json_response(
        {"orderReference": ref, "status": "accept", "time": now,
         "signature": wfp_sign([ref, "accept", now])}
    )

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")

# ------------------------------------------------------------------ keyboards

def kb_followup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [buy_button("buy_129", "Оплатити Модуль 1 — 129 грн 👇")],
            [InlineKeyboardButton("Про курс — програма ℹ️", callback_data="about")],
            [buy_button("buy_699", "Купити всю програму — 699 грн 💳")],
            [InlineKeyboardButton("Маю запитання ❓", url=QUESTION_URL)],
        ]
    )

def kb_single_buy(key: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[buy_button(key, label)]])

def buy_button(key: str, label: str) -> InlineKeyboardButton:
    _, _, url = PRODUCTS[key]
    if WFP_ENABLED or PROVIDER_TOKEN:
        return InlineKeyboardButton(label, callback_data=key)
    if url:
        return InlineKeyboardButton(label, url=url)
    return InlineKeyboardButton(label, callback_data=key)

# ------------------------------------------------------------------ media helpers

async def send_cached_file(chat_id, context, kv_key, path, kind, **kwargs):
    """Send a document/photo, caching Telegram's file_id after the first upload."""
    cached = kv_get(kv_key)
    send = context.bot.send_document if kind == "document" else context.bot.send_photo
    if cached:
        return await send(chat_id, cached, **kwargs)
    if not os.path.exists(path):
        log.warning("%s not found at %s — skipped", kind, path)
        return None
    with open(path, "rb") as f:
        msg = await send(chat_id, f, **kwargs)
    file_id = msg.document.file_id if kind == "document" else msg.photo[-1].file_id
    kv_set(kv_key, file_id)
    return msg

# ------------------------------------------------------------------ channel access

async def make_invite_link(bot, user_id: int) -> str:
    if CHANNEL_ID:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID, member_limit=1, name=f"user {user_id}"
            )
            return link.invite_link
        except Exception as e:
            log.error("create_chat_invite_link failed: %s (is the bot an admin?)", e)
    return CHANNEL_LINK or "(лінк на канал ще не налаштовано — напишіть нам)"

async def send_access(bot, user_id: int):
    set_paid(user_id, 1)
    link = await make_invite_link(bot, user_id)
    await bot.send_message(user_id, TEXT_AFTER_PAYMENT.format(link=link))

# ------------------------------------------------------------------ user handlers

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = int(datetime.now(timezone.utc).timestamp())
    existing = get_user(user.id)

    with db() as conn:
        if existing is None:
            conn.execute(
                "INSERT INTO users(user_id, started_at, username, first_name) VALUES(?,?,?,?)",
                (user.id, now, user.username, user.first_name),
            )
        else:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (user.username, user.first_name, user.id),
            )
    if existing is None:
        schedule_followup(context.application, user.id, started_at=now)

    await update.message.reply_text(TEXT_WELCOME)
    await send_cached_file(
        user.id, context, "pdf_file_id", PDF_PATH, "document",
        filename="Zayavy_pro_sebe_5_shabloniv.pdf",
    )

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        TEXT_ABOUT, reply_markup=kb_single_buy("buy_699", "Купити за 699 грн 💳")
    )

async def job_followup(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data
    row = get_user(user_id)
    if not row or row[1] or row[2]:  # missing, paid, or already sent
        return
    try:
        await send_cached_file(user_id, context, "photo_file_id", PHOTO_PATH, "photo")
        await context.bot.send_message(user_id, TEXT_FOLLOWUP, reply_markup=kb_followup())
        set_f1_sent(user_id)
    except Exception as e:
        log.warning("follow-up to %s failed: %s", user_id, e)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    data = q.data

    if data == "about":
        await context.bot.send_message(
            chat_id, TEXT_ABOUT, reply_markup=kb_single_buy("buy_699", "Купити за 699 грн 💳")
        )
    elif data in PRODUCTS:
        await start_payment(chat_id, data, context)

async def start_payment(chat_id: int, key: str, context: ContextTypes.DEFAULT_TYPE):
    title, amount, url = PRODUCTS[key]

    if WFP_ENABLED:
        try:
            invoice_url, raw = await wfp_create_invoice(key, chat_id)
        except Exception as e:
            invoice_url, raw = None, str(e)
        if invoice_url:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"Оплатити {amount} грн 💳", url=invoice_url)]]
            )
            await context.bot.send_message(
                chat_id,
                f"Рахунок створено: {title}.\nНатискай кнопку, щоб перейти до оплати 👇",
                reply_markup=kb,
            )
        else:
            log.error("WFP invoice failed: %s", raw)
            await context.bot.send_message(
                chat_id, "Ой, не вдалося створити рахунок 😔 Спробуй ще раз за хвилинку."
            )
            if ADMIN_CHAT_ID:
                await context.bot.send_message(
                    ADMIN_CHAT_ID, f"⚠️ WFP CREATE_INVOICE помилка: {str(raw)[:300]}"
                )
        return

    if PROVIDER_TOKEN:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description="Програма «Заяви про себе» — Юлія Маліч та Алеся Стоковська",
            payload=key,
            provider_token=PROVIDER_TOKEN,
            currency="UAH",
            prices=[LabeledPrice(title, amount * 100)],
        )
        return

    if url:
        await context.bot.send_message(chat_id, f"Оплатити можна тут: {url}")
    else:
        await context.bot.send_message(
            chat_id, "Оплата ще налаштовується 🛠 Напиши нам — кнопка «Маю запитання»."
        )

async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await send_access(context.bot, user.id)
    if ADMIN_CHAT_ID:
        sp = update.message.successful_payment
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"💰 Оплата: {sp.total_amount / 100:.0f} {sp.currency}, "
            f"продукт: {sp.invoice_payload}, "
            f"користувач: {user.full_name} (@{user.username or '—'}, id {user.id})",
        )

# ------------------------------------------------------------------ admin commands

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ADMIN_CHAT_ID and update.effective_user.id == ADMIN_CHAT_ID:
            await func(update, context)
    return wrapper

async def _resolve_from_args(update, context):
    if not context.args:
        await update.message.reply_text("Вкажіть користувача: id або @username")
        return None
    user_id = resolve_user(context.args[0])
    if user_id is None:
        await update.message.reply_text(
            f"Не знайшла користувача «{context.args[0]}». "
            "Перевірте @username (людина мала запустити бота) або використайте числовий id."
        )
    return user_id

@admin_only
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _resolve_from_args(update, context)
    if user_id is None:
        return
    try:
        await send_access(context.bot, user_id)
        await update.message.reply_text(f"✅ Доступ надано: {user_id}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не вдалося надіслати: {e}")

@admin_only
async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _resolve_from_args(update, context)
    if user_id is None:
        return
    link = await make_invite_link(context.bot, user_id)
    try:
        await context.bot.send_message(user_id, f"Твій новий лінк на закритий канал:\n{link}")
        await update.message.reply_text(f"✅ Новий лінк надіслано: {user_id}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не вдалося надіслати: {e}")

@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _resolve_from_args(update, context)
    if user_id is None:
        return
    set_paid(user_id, 0)
    if CHANNEL_ID:
        try:
            await context.bot.ban_chat_member(CHANNEL_ID, user_id)
            await context.bot.unban_chat_member(CHANNEL_ID, user_id)  # kick, not permanent ban
            await update.message.reply_text(f"✅ Видалено з каналу: {user_id}")
            return
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Позначено як неоплачено, але з каналу видалити не вдалося: {e}"
            )
            return
    await update.message.reply_text(f"✅ Позначено як неоплачено: {user_id} (CHANNEL_ID не задано)")

@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name, paid, started_at "
            "FROM users ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Поки що нікого немає.")
        return
    lines = []
    for uid, uname, fname, paid, started in rows:
        day = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%d.%m %H:%M")
        lines.append(f"{'💰' if paid else '👤'} {fname or ''} @{uname or '—'} — id {uid} — {day}")
    await update.message.reply_text("\n".join(lines))

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast <текст> — send text to everyone.
    Or reply /broadcast to any message (photo, formatted text) to copy it to everyone."""
    reply = update.message.reply_to_message
    text = update.message.text.partition(" ")[2].strip()
    if not reply and not text:
        await update.message.reply_text(
            "Використання:\n"
            "1) /broadcast Текст розсилки — надішле текст усім;\n"
            "2) надішліть боту повідомлення (можна з фото), потім відповідьте на нього "
            "командою /broadcast — бот скопіює його всім."
        )
        return
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    sent, failed = 0, 0
    for uid in ids:
        try:
            if reply:
                await reply.copy(uid)
            else:
                await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1  # user blocked the bot, etc.
        await asyncio.sleep(0.05)  # stay under Telegram rate limits (~20 msg/s)
    await update.message.reply_text(f"📣 Розсилка завершена: надіслано {sent}, не вдалося {failed}")

# ------------------------------------------------------------------ scheduling & startup

def schedule_followup(app: Application, user_id: int, started_at: int):
    now = datetime.now(timezone.utc).timestamp()
    t = started_at + FOLLOWUP_MINUTES * 60
    app.job_queue.run_once(job_followup, max(1, t - now), data=user_id, name=f"f_{user_id}")

async def post_init(app: Application):
    # Re-schedule pending follow-ups after a restart (Railway redeploys, etc.)
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, started_at FROM users WHERE paid=0 AND f1_sent=0"
        ).fetchall()
    for user_id, started_at in rows:
        schedule_followup(app, user_id, started_at)
    log.info("Re-scheduled follow-ups for %d users", len(rows))

    # Start the WayForPay webhook server
    if WFP_ENABLED:
        webapp = web.Application()
        webapp["tg"] = app
        webapp.router.add_post("/wfp", wfp_webhook)
        webapp.router.add_get("/", health)
        runner = web.AppRunner(webapp)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("WayForPay webhook listening on :%d/wfp", PORT)
        if not PUBLIC_URL:
            log.warning("PUBLIC_URL is not set — WayForPay won't know where to send callbacks!")

# ------------------------------------------------------------------ main

def main():
    db().close()  # create tables / run migrations
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))

    log.info("Bot starting (polling)…  payments: %s",
             "WayForPay" if WFP_ENABLED else ("Telegram Payments" if PROVIDER_TOKEN else "links/manual"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
