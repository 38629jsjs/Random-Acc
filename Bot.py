import html
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from typing import Optional
 
import asyncpg
import qrcode
 
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
 
# ==============================================================================
# CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_GROUP_ID = int(os.getenv("PAYMENT_GROUP_ID", "0"))
STOCK_GROUP_ID = int(os.getenv("STOCK_GROUP_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
 
QR_PAYMENT_INFO = os.getenv("QR_PAYMENT_INFO", "Pay Vinzy Shop")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "vinzyproof")
LOW_STOCK_ALERT = int(os.getenv("LOW_STOCK_ALERT", "5"))
PORT = int(os.getenv("PORT", "8000"))
 
# The single product this shop currently sells. If you ever want a second
# product, add another row to PRODUCT_SEED-style values and insert it the
# same way init_db() seeds this one - the rest of the bot (menus, stock
# commands, buy flow) already works generically with product codes.
PRODUCT_SEED = {
    "code": "random_account",
    "name": "Random Account",
    "price": Decimal("5.00"),
}
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("vinzy-bot")
 
# Conversation states for the Add Funds flow.
ASK_AMOUNT, ASK_RECEIPT = range(2)
 
# Global asyncpg connection pool, created once on startup in on_startup().
db_pool: Optional[asyncpg.Pool] = None
 
 
# ==============================================================================
# TRANSLATIONS
# ==============================================================================
# Everything the bot says to a *customer* is bilingual (English / Khmer).
# Messages aimed at the shop's admins (deposit requests, stock alerts,
# /stats output) are kept in English on purpose since they're internal.
TXT = {
    "lang_confirm": {
        "en": "✅ Language set to English.",
        "km": "✅ បានកំណត់ភាសាជាខ្មែរ។",
    },
    "welcome": {
        "en": "✨ Welcome, {name}!\n\nUse the menu below to view your account or browse our products.",
        "km": "✨ សូមស្វាគមន៍ {name}!\n\nប្រើប្រាស់ម៉ឺនុយខាងក្រោម ដើម្បីពិនិត្យមើលគណនី ឬមើលផលិតផលរបស់យើង។",
    },
    "btn_account": {"en": "👤 Account", "km": "👤 គណនី"},
    "btn_product": {"en": "🛒 Product", "km": "🛒 ផលិតផល"},
    "btn_addfunds": {"en": "💵 Add Funds", "km": "💵 បញ្ចូលលុយ"},
    "btn_orders": {"en": "📜 Order History", "km": "📜 ប្រវត្តិការបញ្ជាទិញ"},
    "btn_payments": {"en": "🧾 Payment History", "km": "🧾 ប្រវត្តិការទូទាត់"},
    "account_info": {
        "en": (
            "👤 Your Account\n"
            "🆔 ID: {id}\n"
            "📛 Name: {name}\n"
            "🔗 Username: @{username}\n"
            "💰 Balance: ${balance}\n"
            "🛍 Orders: {orders}"
        ),
        "km": (
            "👤 គណនីរបស់អ្នក\n"
            "🆔 ID: {id}\n"
            "📛 ឈ្មោះ: {name}\n"
            "🔗 Username: @{username}\n"
            "💰 សមតុល្យ: ${balance}\n"
            "🛍 ការបញ្ជាទិញ: {orders}"
        ),
    },
    "products_title": {
        "en": "🛒 Products\nPick one:",
        "km": "🛒 ផលិតផល\nជ្រើសរើសមួយ:",
    },
    "btn_buy": {"en": "🛍 Buy Now", "km": "🛍 ទិញឥឡូវនេះ"},
    "btn_back": {"en": "🔙 Back", "km": "🔙 ត្រឡប់ក្រោយ"},
    "insufficient_balance": {
        "en": "⚠️ Insufficient balance. Please add funds first.",
        "km": "⚠️ សមតុល្យមិនគ្រប់គ្រាន់។ សូមបញ្ចូលលុយជាមុនសិន។",
    },
    "out_of_stock": {
        "en": "😢 Sorry, this product is currently out of stock. Please check back later.",
        "km": "😢 សុំទោស ផលិតផលនេះអស់ស្តុកហើយ។ សូមមកមើលម្តងទៀតក្រោយ។",
    },
    "purchase_success": {
        "en": (
            "✅ Purchase successful!\n\n"
            "Here is your account:\n<code>{credentials}</code>\n\n"
            "Enjoy! Need help? Contact @{support}."
        ),
        "km": (
            "✅ ការទិញបានជោគជ័យ!\n\n"
            "នេះជាគណនីរបស់អ្នក:\n<code>{credentials}</code>\n\n"
            "សូមរីករាយ! ត្រូវការជំនួយ? ទាក់ទង @{support}។"
        ),
    },
    "ask_amount": {
        "en": "💵 How much do you want to add? Send the amount in $.\nSend /cancel to abort.",
        "km": "💵 អ្នកចង់បញ្ចូលលុយប៉ុន្មាន? សូមផ្ញើចំនួនជាដុល្លារ។\nផ្ញើ /cancel ដើម្បីបោះបង់។",
    },
    "invalid_amount": {
        "en": "⚠️ Please send a valid number greater than 0.",
        "km": "⚠️ សូមផ្ញើលេខត្រឹមត្រូវ ដែលធំជាង 0។",
    },
    "qr_caption": {
        "en": "📷 Scan this QR code to pay ${amount}.\n\nAfter you've paid, send your receipt (screenshot) here.",
        "km": "📷 សូមស្កេន QR Code នេះ ដើម្បីបង់ប្រាក់ ${amount}។\n\nបន្ទាប់ពីបង់រួច សូមផ្ញើវិក្កយបត្រ (Screenshot) មកទីនេះ។",
    },
    "receipt_received": {
        "en": "⏳ Please wait a moment, Vinzy Team is verifying your receipt. This usually takes 1–15 minutes.",
        "km": "⏳ សូមរង់ចាំបន្តិច ក្រុម Vinzy កំពុងផ្ទៀងផ្ទាត់វិក្កយបត្ររបស់អ្នក។ ជាធម្មតាចំណាយពេលប្រហែល ១–១៥ នាទី។",
    },
    "deposit_approved_user": {
        "en": "🎉 Your deposit of ${amount} was successful! New balance: ${balance}.",
        "km": "🎉 ការបញ្ចូលលុយ ${amount} របស់អ្នកជោគជ័យ! សមតុល្យថ្មី: ${balance}។",
    },
    "deposit_rejected_user": {
        "en": "❌ We couldn't verify your payment. Please contact support if you believe this is a mistake.",
        "km": "❌ យើងមិនអាចផ្ទៀងផ្ទាត់ការបង់ប្រាក់របស់អ្នកបានទេ។ សូមទាក់ទងផ្នែកជំនួយ ប្រសិនបើអ្នកគិតថានេះជាកំហុស។",
    },
    "order_history_title": {"en": "📜 Order History", "km": "📜 ប្រវត្តិការបញ្ជាទិញ"},
    "order_history_empty": {
        "en": "📜 Order History\n\nYou haven't bought anything yet.",
        "km": "📜 ប្រវត្តិការបញ្ជាទិញ\n\nអ្នកមិនទាន់បានទិញអ្វីនៅឡើយទេ។",
    },
    "order_history_item": {
        "en": "🛍 {product} — ${price} — {date}",
        "km": "🛍 {product} — ${price} — {date}",
    },
    "payment_history_title": {"en": "🧾 Payment History", "km": "🧾 ប្រវត្តិការទូទាត់"},
    "payment_history_empty": {
        "en": "🧾 Payment History\n\nNo deposits yet.",
        "km": "🧾 ប្រវត្តិការទូទាត់\n\nមិនទាន់មានការបញ្ចូលលុយនៅឡើយទេ។",
    },
    "payment_history_item": {
        "en": "💵 ${amount} — {status} — {date}",
        "km": "💵 ${amount} — {status} — {date}",
    },
    "status_pending": {"en": "⏳ Pending", "km": "⏳ កំពុងរង់ចាំ"},
    "status_approved": {"en": "✅ Approved", "km": "✅ បានយល់ព្រម"},
    "status_rejected": {"en": "❌ Rejected", "km": "❌ បានបដិសេធ"},
    "cancel_done": {"en": "❎ Cancelled.", "km": "❎ បានបោះបង់។"},
    "nothing_to_cancel": {"en": "Nothing to cancel.", "km": "មិនមានអ្វីត្រូវបោះបង់ទេ។"},
}
 
# Shown before a language has been chosen, so it has to be bilingual itself.
CHOOSE_LANG_TEXT = "🌐 Please choose your language / សូមជ្រើសរើសភាសារបស់អ្នក:"
 
# The product description shown on the "Random Account" card. Kept exactly
# as the shop owner wrote it (Khmer version) plus an English translation
# for English-speaking customers. Edit these two strings any time you want
# to change the marketing copy - no other code needs to change.
PRODUCT_DESCRIPTION_EN = (
    "✅ Captured from Renown Collector - World Collector\n"
    "⚠️ NOTIFY: Some accounts can be logged into with your own Gmail, while "
    "others need a Service (🕺 you can still change the email though)\n"
    "‼️ All accounts can be played forever 💥\n"
    "✅ FEEDBACK: t.me/vinzyproof"
)
PRODUCT_DESCRIPTION_KM = (
    "✅បានចាប់ពី Renown Collector- World Collector\n"
    "⚠️NOTIFY: Account ខ្លះដាក់ Gmail ខ្លួនអែងចូលបាន តែ Account ខ្លះត្រូវការ "
    "Service ( 🕺តែ ដូរ Email បាន ដូចគ្នា )\n"
    "‼️Account ទាំងអស់ ជា Account យកទៅ លេងបាន រហូត💥\n"
    "✅FEEDBACK: t.me/vinzyproof"
)
 
# The five main-menu button labels, used to figure out which key a reply
# keyboard tap corresponds to regardless of which language is active.
MENU_KEYS = ["btn_account", "btn_product", "btn_addfunds", "btn_orders", "btn_payments"]
 
 
def tr(key: str, lang: str, **kwargs) -> str:
    """Look up a translated string by key + language and format it.
 
    Example: tr("welcome", "en", name="John") -> "✨ Welcome, John! ..."
    """
    template = TXT[key][lang]
    return template.format(**kwargs) if kwargs else template
 
 
def resolve_menu_key(text: str) -> Optional[str]:
    """Given raw message text from a reply-keyboard tap, return which menu
    item it corresponds to ("btn_account", "btn_product", ...) regardless
    of whether the customer is using English or Khmer. Returns None if the
    text doesn't match any menu button (e.g. it was just a random message).
    """
    for key in MENU_KEYS:
        if text in (TXT[key]["en"], TXT[key]["km"]):
            return key
    return None
 
 
def main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """Build the persistent 2-column reply keyboard for the given language."""
    return ReplyKeyboardMarkup(
        [
            [TXT["btn_account"][lang], TXT["btn_product"][lang]],
            [TXT["btn_addfunds"][lang], TXT["btn_orders"][lang]],
            [TXT["btn_payments"][lang]],
        ],
        resize_keyboard=True,
    )
 
 
def fmt_money(value: Decimal) -> str:
    """Format a Decimal as a 2-decimal-place money string, e.g. '5.00'."""
    return f"{value:.2f}"
 
 
# ==============================================================================
# DATABASE SCHEMA
# ==============================================================================
# `stock` only ever holds *unsold* accounts. The moment one is bought it is
# deleted from this table (and copied into `orders` so the buyer's purchase
# history still shows what they got). That keeps "how many are left" a
# simple COUNT(*) with no extra boolean flags to manage.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    language TEXT,
    balance NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
 
CREATE TABLE IF NOT EXISTS products (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description_en TEXT,
    description_km TEXT,
    price NUMERIC(12,2) NOT NULL DEFAULT 0
);
 
CREATE TABLE IF NOT EXISTS stock (
    id BIGSERIAL PRIMARY KEY,
    product_code TEXT NOT NULL REFERENCES products(code),
    credentials TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
 
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    product_code TEXT NOT NULL,
    price NUMERIC(12,2) NOT NULL,
    credentials TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
 
CREATE TABLE IF NOT EXISTS deposits (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    receipt_file_id TEXT,
    receipt_text TEXT,
    group_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
 
CREATE INDEX IF NOT EXISTS idx_stock_product ON stock (product_code);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits (user_id);
"""
 
 
async def init_db() -> None:
    """Create the asyncpg pool, run the schema, and seed the one product
    this shop sells (only inserts it the very first time, harmless to
    re-run on every restart thanks to ON CONFLICT DO NOTHING)."""
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(
            """INSERT INTO products (code, name, description_en, description_km, price)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (code) DO NOTHING""",
            PRODUCT_SEED["code"],
            PRODUCT_SEED["name"],
            PRODUCT_DESCRIPTION_EN,
            PRODUCT_DESCRIPTION_KM,
            PRODUCT_SEED["price"],
        )
    logger.info("Database ready (product seeded: %s)", PRODUCT_SEED["code"])
 
 
async def ensure_user(user) -> None:
    """Insert a new row for this Telegram user the first time we see them,
    or refresh their cached name/username on every subsequent message."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, username, full_name)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO UPDATE SET username = $2, full_name = $3""",
            user.id,
            user.username,
            user.full_name,
        )
 
 
async def get_user_lang(user_id: int) -> Optional[str]:
    """Return 'en' / 'km' if the user has picked a language before, else None."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT language FROM users WHERE user_id = $1", user_id)
        return row["language"] if row else None
 
 
async def get_stock_count(product_code: str) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM stock WHERE product_code = $1", product_code
        )
 
 
async def get_sold_count(product_code: str) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE product_code = $1", product_code
        )
 
 
async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Check whether a user is an admin/creator of the given group. Used to
    stop random group members from approving deposits or adding stock."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.warning("Could not verify admin status for %s in %s", user_id, chat_id)
        return False
 
 
def generate_qr(amount: Decimal) -> BytesIO:
    """Render a QR code encoding the payment info + amount as a PNG in memory.
 
    NOTE: this just encodes plain text (QR_PAYMENT_INFO + the amount) - it
    is a placeholder you can scan with any QR app to see the payment
    instructions. If you have a real KHQR / bank merchant integration,
    swap the body of this function to build that payload instead.
    """
    payload = f"{QR_PAYMENT_INFO} | Amount: ${fmt_money(amount)}"
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "payment_qr.png"
    return buf
 
 
# ==============================================================================
# /start, language selection, /help
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point. New users are asked to pick a language; returning users
    go straight to the welcome message + main menu in their saved language."""
    user = update.effective_user
    await ensure_user(user)
    lang = await get_user_lang(user.id)
 
    if lang is None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                    InlineKeyboardButton("🇰🇭 ខ្មែរ", callback_data="lang_km"),
                ]
            ]
        )
        await update.message.reply_text(CHOOSE_LANG_TEXT, reply_markup=keyboard)
    else:
        await update.message.reply_text(
            tr("welcome", lang, name=user.full_name),
            reply_markup=main_menu_keyboard(lang),
        )
 
 
async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline 🇬🇧/🇰🇭 buttons shown by /start for first-time users."""
    query = update.callback_query
    await query.answer()
    lang = "en" if query.data == "lang_en" else "km"
 
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET language = $1 WHERE user_id = $2", lang, query.from_user.id
        )
 
    await query.edit_message_text(tr("lang_confirm", lang), reply_markup=None)
    await context.bot.send_message(
        query.message.chat_id,
        tr("welcome", lang, name=query.from_user.full_name),
        reply_markup=main_menu_keyboard(lang),
    )
 
 
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help just re-sends the welcome message + main menu as a convenience."""
    user = update.effective_user
    await ensure_user(user)
    lang = await get_user_lang(user.id) or "en"
    await update.message.reply_text(
        tr("welcome", lang, name=user.full_name),
        reply_markup=main_menu_keyboard(lang),
    )
 
 
# ==============================================================================
# Main menu router: Account / Product / Order History / Payment History.
# "Add Funds" is its own ConversationHandler defined further down.
# ==============================================================================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch a plain text message to the right handler if (and only if)
    it exactly matches one of the main-menu button labels."""
    text = update.message.text
    key = resolve_menu_key(text)
    if key is None:
        return  # Not a menu button tap - ignore silently.
 
    await ensure_user(update.effective_user)
 
    if key == "btn_account":
        await show_account(update, context)
    elif key == "btn_product":
        await show_products(update, context)
    elif key == "btn_orders":
        await show_order_history(update, context)
    elif key == "btn_payments":
        await show_payment_history(update, context)
    # btn_addfunds is intentionally not handled here - the ConversationHandler
    # registered earlier in main() always intercepts it first.
 
 
async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = await get_user_lang(user.id) or "en"
 
    async with db_pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user.id
        ) or Decimal("0")
        orders_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1", user.id
        )
 
    await update.message.reply_text(
        tr(
            "account_info",
            lang,
            id=user.id,
            name=user.full_name,
            username=user.username or "-",
            balance=fmt_money(balance),
            orders=orders_count,
        )
    )
 
 
async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the product list. There is only one product today, but this
    stays data-driven so adding a second one later needs zero code changes
    here - just another row in the `products` table."""
    lang = await get_user_lang(update.effective_user.id) or "en"
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")
 
    buttons = [
        [InlineKeyboardButton(f"🎲 {p['name']}", callback_data=f"prod_{p['code']}")]
        for p in products
    ]
    await update.message.reply_text(
        tr("products_title", lang), reply_markup=InlineKeyboardMarkup(buttons)
    )
 
 
async def show_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show price / in-stock / sold count for one product plus Buy Now / Back."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("_", 1)[1]
    lang = await get_user_lang(query.from_user.id) or "en"
 
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE code = $1", code)
    if not product:
        return
 
    in_stock = await get_stock_count(code)
    sold = await get_sold_count(code)
    description = product["description_en"] if lang == "en" else product["description_km"]
 
    text = (
        f"{product['name']}\n"
        f"{description}\n"
        f"💵 Price: ${fmt_money(product['price'])}\n"
        f"📦 In stock: {in_stock}\n"
        f"🧾 Total sold: {sold}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(tr("btn_buy", lang), callback_data=f"buy_{code}")],
            [InlineKeyboardButton(tr("btn_back", lang), callback_data="back_products")],
        ]
    )
    await query.edit_message_text(text, reply_markup=keyboard)
 
 
async def back_to_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 🔙 Back button on a product card - just re-renders the list."""
    query = update.callback_query
    await query.answer()
    lang = await get_user_lang(query.from_user.id) or "en"
 
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")
 
    buttons = [
        [InlineKeyboardButton(f"🎲 {p['name']}", callback_data=f"prod_{p['code']}")]
        for p in products
    ]
    await query.edit_message_text(
        tr("products_title", lang), reply_markup=InlineKeyboardMarkup(buttons)
    )
 
 
# ==============================================================================
# Buy flow
# ==============================================================================
async def notify_stock_group(context: ContextTypes.DEFAULT_TYPE, buyer, product, price, remaining: int) -> None:
    """Tell the stock group whenever an account is sold, and pile on an
    extra low-stock warning once the pool gets thin. This is the
    notification the shop owner asked for: every sale pings the stock
    group automatically, no manual checking required."""
    if not STOCK_GROUP_ID:
        return
    text = (
        f"🔔 {product['name']} sold!\n"
        f"👤 {buyer.full_name} (@{buyer.username or '-'}) — ID {buyer.id}\n"
        f"💵 ${fmt_money(price)}\n"
        f"📦 Remaining stock: {remaining}"
    )
    if remaining <= LOW_STOCK_ALERT:
        text += f"\n\n⚠️ Low stock warning! Only {remaining} left. Use /addstock to top up."
    try:
        await context.bot.send_message(STOCK_GROUP_ID, text)
    except Exception:
        logger.exception("Failed to notify stock group about a sale")
 
 
async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buy Now handler. Pops one row out of `stock` atomically (deleting it
    from the database the instant it's sold), deducts the buyer's balance,
    writes an `orders` row, and delivers the credentials - all inside a
    single DB transaction so two simultaneous buyers can never receive the
    same account or get double-charged."""
    query = update.callback_query
    code = query.data.split("_", 1)[1]
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"
 
    credentials = None
    price_charged = None
    product_row = None
    error_key = None
 
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            product_row = await conn.fetchrow("SELECT * FROM products WHERE code = $1", code)
            if not product_row:
                await query.answer()
                return
 
            balance = await conn.fetchval(
                "SELECT balance FROM users WHERE user_id = $1", user.id
            ) or Decimal("0")
 
            if balance < product_row["price"]:
                error_key = "insufficient_balance"
            else:
                # Atomically grab-and-remove exactly one stock row. The
                # subquery's FOR UPDATE SKIP LOCKED means concurrent buyers
                # never fight over (or duplicate) the same row - and the
                # DELETE means it is gone from the database the instant
                # it's sold, exactly as requested.
                popped = await conn.fetchrow(
                    """DELETE FROM stock
                       WHERE id = (
                           SELECT id FROM stock
                           WHERE product_code = $1
                           ORDER BY id
                           LIMIT 1
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING credentials""",
                    code,
                )
                if not popped:
                    error_key = "out_of_stock"
                else:
                    await conn.execute(
                        "UPDATE users SET balance = balance - $1 WHERE user_id = $2",
                        product_row["price"],
                        user.id,
                    )
                    await conn.execute(
                        """INSERT INTO orders (user_id, product_code, price, credentials)
                           VALUES ($1, $2, $3, $4)""",
                        user.id,
                        code,
                        product_row["price"],
                        popped["credentials"],
                    )
                    credentials = popped["credentials"]
                    price_charged = product_row["price"]
 
    await query.answer()
 
    if error_key:
        await context.bot.send_message(user.id, tr(error_key, lang))
        return
 
    await context.bot.send_message(
        user.id,
        tr(
            "purchase_success",
            lang,
            credentials=html.escape(credentials),
            support=html.escape(SUPPORT_USERNAME),
        ),
        parse_mode=ParseMode.HTML,
    )
 
    remaining_after = await get_stock_count(code)
    await notify_stock_group(context, user, product_row, price_charged, remaining_after)
 
 
# ==============================================================================
# Order History / Payment History
# ==============================================================================
async def show_order_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = await get_user_lang(user.id) or "en"
 
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.name AS product_name, o.price, o.created_at
               FROM orders o
               JOIN products p ON p.code = o.product_code
               WHERE o.user_id = $1
               ORDER BY o.created_at DESC
               LIMIT 20""",
            user.id,
        )
 
    if not rows:
        await update.message.reply_text(tr("order_history_empty", lang))
        return
 
    lines = [tr("order_history_title", lang), ""]
    for row in rows:
        lines.append(
            tr(
                "order_history_item",
                lang,
                product=row["product_name"],
                price=fmt_money(row["price"]),
                date=row["created_at"].strftime("%Y-%m-%d %H:%M"),
            )
        )
    await update.message.reply_text("\n".join(lines))
 
 
async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = await get_user_lang(user.id) or "en"
 
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT amount, status, created_at FROM deposits
               WHERE user_id = $1
               ORDER BY created_at DESC
               LIMIT 20""",
            user.id,
        )
 
    if not rows:
        await update.message.reply_text(tr("payment_history_empty", lang))
        return
 
    status_key_map = {
        "pending": "status_pending",
        "approved": "status_approved",
        "rejected": "status_rejected",
    }
    lines = [tr("payment_history_title", lang), ""]
    for row in rows:
        status_text = tr(status_key_map.get(row["status"], "status_pending"), lang)
        lines.append(
            tr(
                "payment_history_item",
                lang,
                amount=fmt_money(row["amount"]),
                status=status_text,
                date=row["created_at"].strftime("%Y-%m-%d %H:%M"),
            )
        )
    await update.message.reply_text("\n".join(lines))
 
 
# ==============================================================================
# Add Funds conversation: amount -> QR -> receipt -> pending admin review
# ==============================================================================
async def add_funds_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user(update.effective_user)
    lang = await get_user_lang(update.effective_user.id) or "en"
    context.user_data["lang"] = lang
    await update.message.reply_text(tr("ask_amount", lang))
    return ASK_AMOUNT
 
 
async def add_funds_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    raw = update.message.text.strip().replace("$", "").replace(",", "")
 
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await update.message.reply_text(tr("invalid_amount", lang))
        return ASK_AMOUNT
 
    if amount <= 0:
        await update.message.reply_text(tr("invalid_amount", lang))
        return ASK_AMOUNT
 
    context.user_data["deposit_amount"] = amount
    qr_image = generate_qr(amount)
    await update.message.reply_photo(
        photo=qr_image, caption=tr("qr_caption", lang, amount=fmt_money(amount))
    )
    return ASK_RECEIPT
 
 
async def add_funds_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    amount = context.user_data.get("deposit_amount")
    user = update.effective_user
 
    photo_file_id = None
    receipt_text = None
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    else:
        receipt_text = update.message.text
 
    async with db_pool.acquire() as conn:
        deposit_id = await conn.fetchval(
            """INSERT INTO deposits (user_id, amount, receipt_file_id, receipt_text)
               VALUES ($1, $2, $3, $4)
               RETURNING id""",
            user.id,
            amount,
            photo_file_id,
            receipt_text,
        )
 
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    caption = (
        f"🧾 New Deposit Request\n"
        f"👤 Name: {user.full_name}\n"
        f"🔗 Username: @{user.username or '-'}\n"
        f"🆔 ID: {user.id}\n"
        f"💵 Amount: ${fmt_money(amount)}\n"
        f"🕐 {timestamp}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Add Fund", callback_data=f"dep_ok_{deposit_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"dep_no_{deposit_id}"),
            ]
        ]
    )
 
    if photo_file_id:
        sent = await context.bot.send_photo(
            PAYMENT_GROUP_ID, photo_file_id, caption=caption, reply_markup=keyboard
        )
    else:
        sent = await context.bot.send_message(
            PAYMENT_GROUP_ID,
            caption + f"\n🧾 Receipt: {receipt_text}",
            reply_markup=keyboard,
        )
 
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE deposits SET group_message_id = $1 WHERE id = $2",
            sent.message_id,
            deposit_id,
        )
 
    await update.message.reply_text(tr("receipt_received", lang))
    context.user_data.pop("deposit_amount", None)
    return ConversationHandler.END
 
 
async def add_funds_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback for /cancel while INSIDE the Add Funds conversation."""
    lang = context.user_data.get("lang", "en")
    context.user_data.pop("deposit_amount", None)
    await update.message.reply_text(tr("cancel_done", lang))
    return ConversationHandler.END
 
 
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel sent OUTSIDE any active conversation - nothing to abort."""
    lang = await get_user_lang(update.effective_user.id) or "en"
    await update.message.reply_text(tr("nothing_to_cancel", lang))
 
 
# ==============================================================================
# Deposit approval / rejection (payment group admins only)
# ==============================================================================
async def deposit_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
 
    if query.message.chat_id != PAYMENT_GROUP_ID:
        await query.answer()
        return
 
    if not await is_group_admin(context, PAYMENT_GROUP_ID, query.from_user.id):
        await query.answer("Only admins can do this.", show_alert=True)
        return
 
    approve = query.data.startswith("dep_ok_")
    deposit_id = int(query.data.rsplit("_", 1)[1])
 
    async with db_pool.acquire() as conn:
        deposit = await conn.fetchrow("SELECT * FROM deposits WHERE id = $1", deposit_id)
        if not deposit or deposit["status"] != "pending":
            await query.answer("Already processed.", show_alert=True)
            return
 
        user_lang = (
            await conn.fetchval(
                "SELECT language FROM users WHERE user_id = $1", deposit["user_id"]
            )
            or "en"
        )
 
        if approve:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    deposit["amount"],
                    deposit["user_id"],
                )
                await conn.execute(
                    "UPDATE deposits SET status = 'approved', resolved_at = now() WHERE id = $1",
                    deposit_id,
                )
                new_balance = await conn.fetchval(
                    "SELECT balance FROM users WHERE user_id = $1", deposit["user_id"]
                )
            await context.bot.send_message(
                deposit["user_id"],
                tr(
                    "deposit_approved_user",
                    user_lang,
                    amount=fmt_money(deposit["amount"]),
                    balance=fmt_money(new_balance),
                ),
            )
            stamp = f"\n\n✅ Approved by {query.from_user.full_name}"
        else:
            await conn.execute(
                "UPDATE deposits SET status = 'rejected', resolved_at = now() WHERE id = $1",
                deposit_id,
            )
            await context.bot.send_message(
                deposit["user_id"], tr("deposit_rejected_user", user_lang)
            )
            stamp = f"\n\n❌ Rejected by {query.from_user.full_name}"
 
    await query.answer()
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=(query.message.caption or "") + stamp)
        else:
            await query.edit_message_text(text=(query.message.text or "") + stamp)
    except Exception:
        # Editing can fail for harmless reasons (message too old, etc).
        # The deposit itself is already resolved in the DB either way.
        logger.warning("Could not edit deposit message %s after decision", deposit_id)
 
 
# ==============================================================================
# Stock-group admin commands: /addstock, /setprice, /stocklist, /stats, /myid
# ==============================================================================
async def addstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage (sent inside the stock group):
 
        /addstock random_account
        email1:password1
        email2:password2
        email3:password3
 
    Each line after the command becomes one separate stock item, ready to
    be sold the moment it's saved. Paste as many as you want at once - the
    bot stores them one by one and tells you the new total in stock.
    """
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can add stock.")
        return
 
    lines = update.message.text.split("\n")
    first_line_parts = lines[0].split(maxsplit=1)
    if len(first_line_parts) < 2:
        await update.message.reply_text(
            "Usage:\n/addstock <product_code>\n<account 1>\n<account 2>\n..."
        )
        return
 
    code = first_line_parts[1].strip()
    entries = [line.strip() for line in lines[1:] if line.strip()]
    if not entries:
        await update.message.reply_text("Paste at least one account, one per line.")
        return
 
    async with db_pool.acquire() as conn:
        product_exists = await conn.fetchval("SELECT 1 FROM products WHERE code = $1", code)
        if not product_exists:
            await update.message.reply_text(f"⚠️ Product '{code}' not found.")
            return
        await conn.executemany(
            "INSERT INTO stock (product_code, credentials) VALUES ($1, $2)",
            [(code, entry) for entry in entries],
        )
 
    in_stock = await get_stock_count(code)
    await update.message.reply_text(
        f"✅ Added {len(entries)} account(s) to '{code}'.\n📦 Now in stock: {in_stock}"
    )
 
 
async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setprice random_account 6.50"""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can change prices.")
        return
 
    parts = update.message.text.split()
    if len(parts) != 3:
        await update.message.reply_text("Usage: /setprice <product_code> <price>")
        return
 
    code, price_raw = parts[1], parts[2]
    try:
        price = Decimal(price_raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await update.message.reply_text("Invalid price.")
        return
 
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE products SET price = $1 WHERE code = $2", price, code
        )
 
    if result == "UPDATE 0":
        await update.message.reply_text(f"⚠️ Product '{code}' not found.")
    else:
        await update.message.reply_text(f"✅ Price for '{code}' set to ${fmt_money(price)}.")
 
 
async def stocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show in-stock and total-sold counts for every product."""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
 
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name, price FROM products ORDER BY code")
 
    lines = ["📦 Stock overview:"]
    for product in products:
        in_stock = await get_stock_count(product["code"])
        sold = await get_sold_count(product["code"])
        lines.append(
            f"• {product['name']} ({product['code']}) — ${fmt_money(product['price'])} "
            f"— in stock: {in_stock} — sold: {sold}"
        )
    await update.message.reply_text("\n".join(lines))
 
 
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick shop-wide stats: users, orders, revenue, pending deposits."""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can view stats.")
        return
 
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM orders")
        total_topped_up = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'approved'"
        )
        pending_deposits = await conn.fetchval(
            "SELECT COUNT(*) FROM deposits WHERE status = 'pending'"
        )
 
    text = (
        "📊 Vinzy Shop Stats\n\n"
        f"👥 Total users: {total_users}\n"
        f"🛍 Total orders: {total_orders}\n"
        f"💰 Total revenue: ${fmt_money(total_revenue)}\n"
        f"💵 Total topped up (approved): ${fmt_money(total_topped_up)}\n"
        f"⏳ Pending deposits awaiting review: {pending_deposits}"
    )
    await update.message.reply_text(text)
 
 
async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handy helper for setting up PAYMENT_GROUP_ID / STOCK_GROUP_ID - add the
    bot to a group, run /myid there, and copy the Chat ID into Koyeb."""
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(f"💬 Chat ID: {chat.id}\n👤 Your User ID: {user.id}")
 
 
# ==============================================================================
# Tiny built-in health-check server
# ==============================================================================
# Koyeb's "web" process type expects something listening on $PORT to answer
# health checks. This bot is otherwise a pure long-polling worker, so this
# tiny HTTP server just exists to keep Koyeb happy - it does nothing else.
def run_health_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
 
        def log_message(self, *args):
            pass  # Keep the bot's own logs free of HTTP noise.
 
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
 
 
# ==============================================================================
# Global error handler
# ==============================================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
 
 
# ==============================================================================
# App wiring
# ==============================================================================
async def on_startup(app: Application) -> None:
    if not PAYMENT_GROUP_ID:
        logger.warning("PAYMENT_GROUP_ID is not set - deposit verification will not work.")
    if not STOCK_GROUP_ID:
        logger.warning("STOCK_GROUP_ID is not set - /addstock and friends will not work.")
    await init_db()
 
 
def build_application() -> Application:
    """Construct the PTB Application and register every handler. Pulled out
    of main() so it's easy to unit-test the wiring later if you want to."""
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
 
    add_funds_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Text([TXT["btn_addfunds"]["en"], TXT["btn_addfunds"]["km"]]),
                add_funds_start,
            )
        ],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_funds_amount)],
            ASK_RECEIPT: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), add_funds_receipt)
            ],
        },
        fallbacks=[CommandHandler("cancel", add_funds_cancel)],
    )
 
    # Order matters: more specific handlers (commands, the conversation,
    # callback patterns) are registered before the catch-all menu_router.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid_cmd))
    application.add_handler(CallbackQueryHandler(set_language_callback, pattern="^lang_"))
 
    application.add_handler(add_funds_conversation)
    application.add_handler(CommandHandler("cancel", cancel_command))
 
    application.add_handler(CommandHandler("addstock", addstock_cmd))
    application.add_handler(CommandHandler("setprice", setprice_cmd))
    application.add_handler(CommandHandler("stocklist", stocklist_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
 
    application.add_handler(CallbackQueryHandler(show_product_detail, pattern="^prod_"))
    application.add_handler(CallbackQueryHandler(back_to_products, pattern="^back_products$"))
    application.add_handler(CallbackQueryHandler(buy_product, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(deposit_decision, pattern="^dep_"))
 
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))
 
    application.add_error_handler(error_handler)
    return application
 
 
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required.")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL environment variable is required.")
 
    application = build_application()
 
    threading.Thread(target=run_health_server, daemon=True).start()
 
    logger.info("Vinzy Shop bot starting (long polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
 
 
if __name__ == "__main__":
    main()
 
# ==============================================================================
# POSSIBLE FUTURE ENHANCEMENTS (not implemented, just notes for later)
# ==============================================================================
# - A real KHQR / bank API integration in generate_qr() instead of a plain
#   text QR, so payments could be confirmed automatically without an admin
#   having to look at a screenshot.
# - A /addproduct command in the stock group so new product types can be
#   created without touching the database directly.
# - Pagination on /stocklist, Order History and Payment History once those
#   lists grow past ~20 entries.
# - Persisting ConversationHandler state to the database so an in-progress
#   Add Funds flow survives a bot restart (currently it does not - if the
#   bot restarts mid-flow, the customer just needs to tap Add Funds again).
# - A "minimum top-up amount" check in add_funds_amount() if you want to
#   stop people sending tiny deposits like $0.01.
# - Rate-limiting /addstock so a typo-filled paste can't flood the stock
#   table with junk entries - currently every non-empty line is trusted.
 
