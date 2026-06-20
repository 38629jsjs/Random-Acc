"""
==============================================================================
 VINZY SHOP - Telegram Account Selling Bot  (worker / long-polling)
==============================================================================

A single-file Telegram bot for selling "Random Account" stock directly
inside Telegram. Customers pick a language, get offered 50% off for
joining your channel, top up their balance by scanning your KHQR and
sending a receipt, and get an account instantly delivered the moment they
buy. Everything lives in Postgres (Neon). Three files only: bot.py,
requirements.txt, Procfile. Deploy on Koyeb as a WORKER service (no web
port needed - this bot only long-polls Telegram, it never listens for
HTTP traffic).

------------------------------------------------------------------------------
HOW THE STORE WORKS (customer side)
------------------------------------------------------------------------------
1. /start -> pick language (English / Khmer), saved per user forever.
2. Right after picking a language: a promo screen offers 50% OFF every
   account if they join your channel. Three buttons:
     - "🎉 Join Channel"        -> opens t.me/vinzystore168
     - "✅ I've Joined - Verify" -> bot checks real channel membership
     - "➡️ Continue Without Joining" -> skips the discount, shop normally
   They can come back and verify any time later from the Account screen.
3. Main menu (persistent keyboard): Account, Product, Add Funds,
   Order History, Payment History.
4. Product -> "Random Account" card shows price (with 50% OFF shown if
   they're a verified channel member), in-stock count, total sold, and a
   Buy Now button.
5. Buy Now deducts balance, pops exactly ONE stock entry out of the
   database (so it can never be sold twice), delivers it instantly, and
   pings the stock group with the sale + remaining stock.
6. Add Funds: customer sends an amount -> bot sends your KHQR photo ->
   customer sends a screenshot of the paid receipt -> request goes to the
   payment group with the customer's name/username/ID and Confirm/Decline
   buttons -> whichever staff member taps a button approves or declines,
   and the customer is notified automatically either way.

------------------------------------------------------------------------------
HOW THE STORE WORKS (stock group - admins only)
------------------------------------------------------------------------------
    /addstock                Start a stock drop. The bot asks which product
                             (skipped if you only have one), then asks you
                             to send a .txt file. It automatically pulls
                             out the Gmail, Password, ID, Name, Level, Hero
                             Count, Skin Count, Banned, V2L Status and
                             Collector Title from each account, then asks
                             how many to actually add.
    /setprice <code> <price>     Change a product's price
    /productprice <code> <price> Same thing, alias
    /addproduct <code> <price> <name...>   Create a brand new product
    /stocklist               Show stock count + total sold per product
    /khqr                    Replace the KHQR payment photo customers see
    /addbal <user_id> <amount>      Manually add balance to a customer
    /removebal <user_id> <amount>   Manually remove balance from a customer
    /announce <text>         Broadcast a message to every customer who has
                             ever used the bot
    /stats                   Shop-wide statistics
    /myid                    Show this chat's ID + your user ID
    /commands or /help       Show this list
    /cancel                  Abort whatever admin flow is in progress

------------------------------------------------------------------------------
HOW THE STORE WORKS (payment group - admins only)
------------------------------------------------------------------------------
    Every deposit request lands here automatically with Confirm / Decline
    buttons attached - just tap one.
    /stat                    Total money successfully added to balances
    /myid
    /commands or /help

------------------------------------------------------------------------------
REQUIRED ENVIRONMENT VARIABLES
------------------------------------------------------------------------------
    BOT_TOKEN              Telegram bot token from @BotFather
    PAYMENT_GROUP_ID       Chat ID of the deposit-approval group (negative)
    STOCK_GROUP_ID         Chat ID of the stock/admin group (negative)
    DATABASE_URL           Neon Postgres connection string

------------------------------------------------------------------------------
OPTIONAL ENVIRONMENT VARIABLES
------------------------------------------------------------------------------
    CHANNEL_USERNAME        Channel for the 50% off promo, without the @
                             (default: vinzystore168). The bot must be an
                             admin/member of this channel to verify joins.
    DISCOUNT_PERCENT         Discount % for channel members (default: 50)
    SUPPORT_USERNAME         Shown after a purchase if they need help
                             (default: vinzyproof)
    PAYMENT_SUPPORT_USERNAME Shown when a deposit is declined
                             (default: Vinzy168)
    LOW_STOCK_ALERT          Stock count that triggers a low-stock warning
                             in the stock group (default: 5)

==============================================================================
"""

import html
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "vinzystore168").lstrip("@")
DISCOUNT_PERCENT = Decimal(os.getenv("DISCOUNT_PERCENT", "50"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "vinzyproof")
PAYMENT_SUPPORT_USERNAME = os.getenv("PAYMENT_SUPPORT_USERNAME", "Vinzy168")
LOW_STOCK_ALERT = int(os.getenv("LOW_STOCK_ALERT", "5"))

# Seed data for the one product this shop ships with. More products can be
# added later from the stock group with /addproduct without touching code.
PRODUCT_SEED = {
    "code": "random_account",
    "name": "Random Account",
    "price": Decimal("3.50"),
}
PRODUCT_DESCRIPTION_EN = (
    "✅\n"
    "⚠️ NOTIFY: Some accounts can be logged into with your own Gmail, while "
    "others need a Service (🕺 you can still change the email though)\n"
    "✅ FEEDBACK: t.me/vinzyproof"
)
PRODUCT_DESCRIPTION_KM = (
    "✅\n"
    "⚠️NOTIFY: Account ខ្លះដាក់ Gmail ខ្លួនអែងចូលបាន តែ Account ខ្លះត្រូវការ "
    "Service ( 🕺តែ ដូរ Email បាន ដូចគ្នា )\n"
    "✅FEEDBACK: t.me/vinzyproof"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("vinzy-bot")

# Conversation states.
ASK_AMOUNT, ASK_RECEIPT = range(2)                 # Add Funds (customer)
ASK_STOCK_PRODUCT, ASK_STOCK_FILE, ASK_STOCK_QUANTITY = range(10, 13)  # /addstock (stock group)
ASK_KHQR_PHOTO = 20                                # /khqr (stock group)
CLEAR_ASK_PRODUCT, CLEAR_ASK_QUANTITY, CLEAR_CONFIRM = range(40, 43)  # /clearstock (stock group)

db_pool: Optional[asyncpg.Pool] = None


# ==============================================================================
# TRANSLATIONS  (customer-facing only - the stock & payment groups always
# get plain English, as requested, so there are no translation lookups in
# any of the group-admin functions further down this file)
# ==============================================================================
TXT = {
    "lang_confirm": {"en": "✅ Language set to English.", "km": "✅ បានកំណត់ភាសាជាខ្មែរ។"},
    "welcome": {
        "en": "✨ Welcome, {name}!\n\nUse the menu below to view your account or browse our products.",
        "km": "✨ សូមស្វាគមន៍ {name}!\n\nប្រើប្រាស់ម៉ឺនុយខាងក្រោម ដើម្បីពិនិត្យមើលគណនី ឬមើលផលិតផលរបស់យើង។",
    },
    "promo_text": {
        "en": (
            "🎉 Special Offer!\n\n"
            "Join our channel and get {percent}% OFF every account, forever!\n"
            "👉 {channel_link}\n\n"
            "Already joined? Tap Verify below. Not interested? You can still "
            "shop at full price."
        ),
        "km": (
            "🎉 ការផ្តល់ជូនពិសេស!\n\n"
            "ចូលរួម Channel របស់យើង ហើយទទួលបានការបញ្ចុះតម្លៃ {percent}% "
            "លើគណនីទាំងអស់ ជារៀងរហូត!\n"
            "👉 {channel_link}\n\n"
            "ចូលរួមរួចហើយមែនទេ? ចុច Verify ខាងក្រោម។ មិនចាប់អារម្មណ៍ទេ? "
            "អ្នកនៅតែអាចទិញតាមតម្លៃធម្មតាបាន។"
        ),
    },
    "btn_join_channel": {"en": "🎉 Join Channel", "km": "🎉 ចូលរួម Channel"},
    "btn_verify_join": {"en": "✅ I've Joined - Verify", "km": "✅ ខ្ញុំបានចូលរួមហើយ - ផ្ទៀងផ្ទាត់"},
    "btn_skip_join": {"en": "➡️ Continue Without Joining", "km": "➡️ បន្តដោយមិនចូលរួម"},
    "verify_success": {
        "en": "🎉 Verified! You now get {percent}% OFF every account. Enjoy!",
        "km": "🎉 បានផ្ទៀងផ្ទាត់! ឥឡូវនេះអ្នកទទួលបានការបញ្ចុះតម្លៃ {percent}% លើគណនីទាំងអស់។ សូមរីករាយ!",
    },
    "verify_fail": {
        "en": "❌ We couldn't find you in the channel yet. Please join first, then tap Verify again.",
        "km": "❌ យើងមិនទាន់ឃើញអ្នកនៅក្នុង Channel នៅឡើយទេ។ សូមចូលរួមជាមុនសិន រួចចុច Verify ម្តងទៀត។",
    },
    "already_verified": {
        "en": "✅ You're already a verified channel member with {percent}% OFF active.",
        "km": "✅ អ្នកគឺជាសមាជិក Channel ដែលបានផ្ទៀងផ្ទាត់រួចហើយ ដោយមានការបញ្ចុះតម្លៃ {percent}% សកម្ម។",
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
            "🛍 Orders: {orders}\n"
            "🎟 Channel Discount: {discount_status}"
        ),
        "km": (
            "👤 គណនីរបស់អ្នក\n"
            "🆔 ID: {id}\n"
            "📛 ឈ្មោះ: {name}\n"
            "🔗 Username: @{username}\n"
            "💰 សមតុល្យ: ${balance}\n"
            "🛍 ការបញ្ជាទិញ: {orders}\n"
            "🎟 ការបញ្ចុះតម្លៃ Channel: {discount_status}"
        ),
    },
    "discount_active": {"en": "✅ Active ({percent}% OFF)", "km": "✅ សកម្ម ({percent}% បញ្ចុះតម្លៃ)"},
    "discount_inactive": {"en": "❌ Not active - tap below to unlock", "km": "❌ មិនទាន់សកម្ម - ចុចខាងក្រោមដើម្បីដោះសោ"},
    "btn_unlock_discount": {"en": "🎉 Unlock {percent}% OFF", "km": "🎉 ដោះសោការបញ្ចុះតម្លៃ {percent}%"},
    "products_title": {"en": "🛒 Products\nPick one:", "km": "🛒 ផលិតផល\nជ្រើសរើសមួយ:"},
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
    "qr_not_ready": {
        "en": "⚠️ Payment QR isn't set up yet. Please contact @{support}.",
        "km": "⚠️ QR សម្រាប់បង់ប្រាក់មិនទាន់រួចរាល់ទេ។ សូមទាក់ទង @{support}។",
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
        "en": "❌ Your payment was declined or didn't go through. Please contact @{support} if there's a problem.",
        "km": "❌ ការបង់ប្រាក់របស់អ្នកត្រូវបានបដិសេធ ឬមិនបានជោគជ័យទេ។ សូមទាក់ទង @{support} ប្រសិនបើមានបញ្ហា។",
    },
    "order_history_title": {"en": "📜 Order History", "km": "📜 ប្រវត្តិការបញ្ជាទិញ"},
    "order_history_empty": {
        "en": "📜 Order History\n\nYou haven't bought anything yet.",
        "km": "📜 ប្រវត្តិការបញ្ជាទិញ\n\nអ្នកមិនទាន់បានទិញអ្វីនៅឡើយទេ។",
    },
    "order_history_item": {"en": "🛍 {product} — ${price} — {date}", "km": "🛍 {product} — ${price} — {date}"},
    "payment_history_title": {"en": "🧾 Payment History", "km": "🧾 ប្រវត្តិការទូទាត់"},
    "payment_history_empty": {
        "en": "🧾 Payment History\n\nNo deposits yet.",
        "km": "🧾 ប្រវត្តិការទូទាត់\n\nមិនទាន់មានការបញ្ចូលលុយនៅឡើយទេ។",
    },
    "payment_history_item": {"en": "💵 ${amount} — {status} — {date}", "km": "💵 ${amount} — {status} — {date}"},
    "status_pending": {"en": "⏳ Pending", "km": "⏳ កំពុងរង់ចាំ"},
    "status_approved": {"en": "✅ Approved", "km": "✅ បានយល់ព្រម"},
    "status_rejected": {"en": "❌ Rejected", "km": "❌ បានបដិសេធ"},
    "cancel_done": {"en": "❎ Cancelled.", "km": "❎ បានបោះបង់។"},
    "nothing_to_cancel": {"en": "Nothing to cancel.", "km": "មិនមានអ្វីត្រូវបោះបង់ទេ។"},
    "balance_added_notify": {
        "en": "💰 An admin added ${amount} to your balance. New balance: ${balance}.",
        "km": "💰 អ្នកគ្រប់គ្រងបានបញ្ចូលលុយ ${amount} ទៅក្នុងសមតុល្យរបស់អ្នក។ សមតុល្យថ្មី: ${balance}។",
    },
    "balance_removed_notify": {
        "en": "⚠️ An admin adjusted your balance by -${amount}. New balance: ${balance}.",
        "km": "⚠️ អ្នកគ្រប់គ្រងបានកែសម្រួលសមតុល្យរបស់អ្នកដោយ -${amount}។ សមតុល្យថ្មី: ${balance}។",
    },
}

CHOOSE_LANG_TEXT = "🌐 Please choose your language / សូមជ្រើសរើសភាសារបស់អ្នក:"
MENU_KEYS = ["btn_account", "btn_product", "btn_addfunds", "btn_orders", "btn_payments"]

CHANNEL_LINK = f"https://t.me/{CHANNEL_USERNAME}"


def tr(key: str, lang: str, **kwargs) -> str:
    """Look up a translated string by key + language and format it."""
    template = TXT[key][lang]
    return template.format(**kwargs) if kwargs else template


def resolve_menu_key(text: str) -> Optional[str]:
    """Map raw reply-keyboard text back to a menu key, regardless of language."""
    for key in MENU_KEYS:
        if text in (TXT[key]["en"], TXT[key]["km"]):
            return key
    return None


def main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [TXT["btn_account"][lang], TXT["btn_product"][lang]],
            [TXT["btn_addfunds"][lang], TXT["btn_orders"][lang]],
            [TXT["btn_payments"][lang]],
        ],
        resize_keyboard=True,
    )


def fmt_money(value: Decimal) -> str:
    return f"{value:.2f}"


def effective_price(base_price: Decimal, discount_eligible: bool) -> Decimal:
    """Apply the channel-join discount to a base price, if the customer has
    earned it. Recomputed fresh every time it's needed - never cached -
    so a price change or a freshly-unlocked discount always applies
    immediately."""
    if not discount_eligible:
        return base_price
    multiplier = (Decimal("100") - DISCOUNT_PERCENT) / Decimal("100")
    return (base_price * multiplier).quantize(Decimal("0.01"))


# ==============================================================================
# DATABASE SCHEMA
# ==============================================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    language TEXT,
    balance NUMERIC(12,2) NOT NULL DEFAULT 0,
    discount_eligible BOOLEAN NOT NULL DEFAULT FALSE,
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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS balance_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    admin_id BIGINT,
    amount NUMERIC(12,2) NOT NULL,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stock_product ON stock (product_code);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits (user_id);

-- Self-healing migration: if any of these tables already existed from an
-- earlier version of this bot (with fewer columns), CREATE TABLE IF NOT
-- EXISTS above does nothing - these ALTER statements patch any missing
-- column on every boot instead, so the bot never crashes on a stale schema.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS balance NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS discount_eligible BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE products ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS description_en TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS description_km TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS price NUMERIC(12,2) NOT NULL DEFAULT 0;

ALTER TABLE stock ADD COLUMN IF NOT EXISTS product_code TEXT;
ALTER TABLE stock ADD COLUMN IF NOT EXISTS credentials TEXT;
ALTER TABLE stock ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_code TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS price NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS credentials TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE deposits ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS amount NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS receipt_file_id TEXT;
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS receipt_text TEXT;
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS group_message_id BIGINT;
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE deposits ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
"""


async def init_db() -> None:
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
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, username, full_name)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO UPDATE SET username = $2, full_name = $3""",
            user.id,
            user.username,
            user.full_name,
        )


async def get_user_row(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)


async def get_user_lang(user_id: int) -> Optional[str]:
    row = await get_user_row(user_id)
    return row["language"] if row else None


async def get_stock_count(product_code: str) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM stock WHERE product_code = $1", product_code)


async def get_sold_count(product_code: str) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM orders WHERE product_code = $1", product_code)


async def get_setting(key: str) -> Optional[str]:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)


async def set_setting(key: str, value: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO settings (key, value) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET value = $2""",
            key,
            value,
        )


async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.warning("Could not verify admin status for %s in %s", user_id, chat_id)
        return False


async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Ask Telegram whether this user is actually in the promo channel.
    Requires the bot to be a member/admin of that channel."""
    try:
        member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logger.warning("Could not verify channel membership for %s", user_id)
        return False


def generate_fallback_qr(amount: Decimal) -> BytesIO:
    """Used only if no KHQR photo has been uploaded yet via /khqr - keeps
    the Add Funds flow from breaking on a fresh deployment."""
    payload = f"Pay Vinzy Shop | Amount: ${fmt_money(amount)}"
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "payment_qr.png"
    return buf


def parse_stock_dump(text: str) -> list[str]:
    """Split a pasted/uploaded block of accounts into individual raw
    entries, before any field extraction happens.

    Accounts are expected to be separated by a line of three or more
    dashes (---), matching how the shop owner already formats stock
    drops, e.g.:

        1. email1:pass1
           Level: 125
        ---
        2. email2:pass2
           Level: 77

    Leading numbering like "1. " / "2. " on the first line of each block
    is stripped automatically (the numbers in real dumps aren't even
    sequential/unique, so they're never relied on for anything). Blank
    blocks are ignored.
    """
    blocks = re.split(r"\n?-{3,}\n?", text)
    entries = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        block = re.sub(r"^\d+\.\s*", "", block, count=1)
        entries.append(block)
    return entries


# The exact fields kept from each raw account dump, in display order.
# Add or remove a (label, emoji) pair here any time the shop wants to
# show different stats on a stock card - nothing else needs to change.
SMART_FIELDS = [
    ("ID", "🆔"),
    ("Name", "📛"),
    ("Level", "⭐"),
    ("Hero Count", "🦸"),
    ("Skin Count", "🎨"),
    ("Banned", "🚫"),
    ("V2L Status", "🔄"),
    ("Collector Title", "🏆"),
]


def smart_extract_account(block: str) -> str:
    """Pull just the fields buyers actually care about out of a raw,
    stat-heavy account dump (the kind with 50+ lines of KDA/match stats)
    and format them into a clean, compact entry. Any field missing from
    a particular block just shows "N/A" instead of breaking the import -
    dumps are never perfectly uniform between scrapes.
    """
    lines = block.splitlines()
    first_line = lines[0].strip() if lines else ""
    if ":" in first_line:
        email, _, password = first_line.partition(":")
    else:
        email, password = first_line, ""

    def field(label: str) -> str:
        match = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", block, re.MULTILINE)
        return match.group(1).strip() if match else "N/A"

    def collector_title() -> str:
        # Most dumps have it on its own line: "Collector Title: X".
        match = re.search(r"^\s*Collector Title:\s*(.+)$", block, re.MULTILINE)
        if match:
            return match.group(1).strip()
        # Some dumps embed it instead: "Collector Level: 47750  Title: X".
        match = re.search(r"Title:\s*(.+)$", block, re.MULTILINE)
        return match.group(1).strip() if match else "N/A"

    lines_out = [f"📧 Gmail: {email.strip()}", f"🔑 Password: {password.strip()}"]
    for label, emoji in SMART_FIELDS:
        value = collector_title() if label == "Collector Title" else field(label)
        lines_out.append(f"{emoji} {label}: {value}")
    return "\n".join(lines_out)


# ==============================================================================
# /start, language selection, channel-join promo
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
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


async def send_promo_screen(chat_id: int, lang: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = tr("promo_text", lang, percent=int(DISCOUNT_PERCENT), channel_link=CHANNEL_LINK)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(tr("btn_join_channel", lang), url=CHANNEL_LINK)],
            [InlineKeyboardButton(tr("btn_verify_join", lang), callback_data="verify_join")],
            [InlineKeyboardButton(tr("btn_skip_join", lang), callback_data="skip_join")],
        ]
    )
    await context.bot.send_message(chat_id, text, reply_markup=keyboard)


async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = "en" if query.data == "lang_en" else "km"

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET language = $1 WHERE user_id = $2", lang, query.from_user.id)

    await query.edit_message_text(tr("lang_confirm", lang), reply_markup=None)
    await send_promo_screen(query.message.chat_id, lang, context)


async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"

    is_member = await check_channel_membership(context, user.id)
    if is_member:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET discount_eligible = TRUE WHERE user_id = $1", user.id
            )
        # Clear the Join/Verify/Skip buttons now that verification succeeded
        # - nothing left for the customer to tap on this message.
        await query.edit_message_text(
            tr("verify_success", lang, percent=int(DISCOUNT_PERCENT)), reply_markup=None
        )
    else:
        # Leave the buttons in place on failure so they can join then retry
        # Verify without having to dig up the promo message again.
        await query.edit_message_text(tr("verify_fail", lang))
        return

    await context.bot.send_message(
        query.message.chat_id,
        tr("welcome", lang, name=user.full_name),
        reply_markup=main_menu_keyboard(lang),
    )


async def skip_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        query.message.chat_id,
        tr("welcome", lang, name=user.full_name),
        reply_markup=main_menu_keyboard(lang),
    )


async def unlock_discount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The "Unlock X% OFF" button shown on the Account screen for anyone
    who hasn't verified channel membership yet."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"
    await send_promo_screen(query.message.chat_id, lang, context)


# ==============================================================================
# /help and /commands - branches by chat: customer / stock group / payment group
# ==============================================================================
STOCK_HELP_TEXT = (
    "🛠 Vinzy Shop - Stock Group Commands\n\n"
    "/addstock - Start a stock drop (bot asks for a product, then a .txt file of accounts)\n"
    "/clearstock - Permanently remove stock from a product (asks which product, how many, then confirms)\n"
    "/setprice <code> <price> - Change a product's price\n"
    "/productprice <code> <price> - Same as /setprice\n"
    "/addproduct <code> <price> <name...> - Create a new product\n"
    "/stocklist - Show stock count + total sold per product\n"
    "/khqr - Replace the KHQR payment photo shown to customers\n"
    "/addbal <user_id> <amount> - Manually add balance to a customer\n"
    "/removebal <user_id> <amount> - Manually remove balance from a customer\n"
    "/announce <text> - Broadcast a message to every customer who has used the bot\n"
    "/stats - Shop-wide statistics\n"
    "/myid - Show this chat's ID and your user ID\n"
    "/cancel - Abort whatever admin flow is in progress\n"
    "/commands or /help - Show this list"
)

PAYMENT_HELP_TEXT = (
    "💳 Vinzy Shop - Payment Group Commands\n\n"
    "Deposit requests appear here automatically with Confirm/Decline buttons "
    "- just tap one.\n\n"
    "/stat - Total money successfully added to customer balances\n"
    "/myid - Show this chat's ID and your user ID\n"
    "/commands or /help - Show this list"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.id == STOCK_GROUP_ID:
        await update.message.reply_text(STOCK_HELP_TEXT)
        return
    if chat.id == PAYMENT_GROUP_ID:
        await update.message.reply_text(PAYMENT_HELP_TEXT)
        return
    if chat.type != "private":
        return

    user = update.effective_user
    await ensure_user(user)
    lang = await get_user_lang(user.id) or "en"
    await update.message.reply_text(
        tr("welcome", lang, name=user.full_name),
        reply_markup=main_menu_keyboard(lang),
    )


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(f"💬 Chat ID: {chat.id}\n👤 Your User ID: {user.id}")


# ==============================================================================
# Main menu router (private chats only)
# ==============================================================================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    key = resolve_menu_key(text)
    if key is None:
        return

    await ensure_user(update.effective_user)

    if key == "btn_account":
        await show_account(update, context)
    elif key == "btn_product":
        await show_products(update, context)
    elif key == "btn_orders":
        await show_order_history(update, context)
    elif key == "btn_payments":
        await show_payment_history(update, context)
    # btn_addfunds is handled by its own ConversationHandler.


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = await get_user_lang(user.id) or "en"
    row = await get_user_row(user.id)
    balance = row["balance"] if row else Decimal("0")
    discount_eligible = row["discount_eligible"] if row else False

    async with db_pool.acquire() as conn:
        orders_count = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id = $1", user.id)

    discount_status = (
        tr("discount_active", lang, percent=int(DISCOUNT_PERCENT))
        if discount_eligible
        else tr("discount_inactive", lang)
    )

    text = tr(
        "account_info",
        lang,
        id=user.id,
        name=user.full_name,
        username=user.username or "-",
        balance=fmt_money(balance),
        orders=orders_count,
        discount_status=discount_status,
    )

    if discount_eligible:
        await update.message.reply_text(text)
    else:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                tr("btn_unlock_discount", lang, percent=int(DISCOUNT_PERCENT)),
                callback_data="unlock_discount",
            )]]
        )
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await get_user_lang(update.effective_user.id) or "en"
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")

    buttons = [[InlineKeyboardButton(f"🎲 {p['name']}", callback_data=f"prod_{p['code']}")] for p in products]
    await update.message.reply_text(tr("products_title", lang), reply_markup=InlineKeyboardMarkup(buttons))


def build_product_card_text(product, lang: str, in_stock: int, sold: int, discount_eligible: bool) -> str:
    description = product["description_en"] if lang == "en" else product["description_km"]
    base_price = product["price"]

    if discount_eligible:
        discounted = effective_price(base_price, True)
        price_line = f"💵 Price: <s>${fmt_money(base_price)}</s> ${fmt_money(discounted)} ({int(DISCOUNT_PERCENT)}% OFF)"
    else:
        price_line = f"💵 Price: ${fmt_money(base_price)}"

    return (
        f"<b>{html.escape(product['name'])}</b>\n"
        f"{html.escape(description)}\n"
        f"{price_line}\n"
        f"📦 In stock: {in_stock}\n"
        f"🧾 Total sold: {sold}"
    )


async def show_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    code = query.data.split("_", 1)[1]
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"
    row = await get_user_row(user.id)
    discount_eligible = row["discount_eligible"] if row else False

    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE code = $1", code)
    if not product:
        return

    in_stock = await get_stock_count(code)
    sold = await get_sold_count(code)
    text = build_product_card_text(product, lang, in_stock, sold, discount_eligible)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(tr("btn_buy", lang), callback_data=f"buy_{code}")],
            [InlineKeyboardButton(tr("btn_back", lang), callback_data="back_products")],
        ]
    )
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def back_to_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = await get_user_lang(query.from_user.id) or "en"

    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")

    buttons = [[InlineKeyboardButton(f"🎲 {p['name']}", callback_data=f"prod_{p['code']}")] for p in products]
    await query.edit_message_text(
        tr("products_title", lang), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=None
    )


# ==============================================================================
# Buy flow
# ==============================================================================
async def notify_stock_group(context: ContextTypes.DEFAULT_TYPE, buyer, product, price, remaining: int) -> None:
    if not STOCK_GROUP_ID:
        return
    text = (
        f"🔔 {product['name']} sold!\n"
        f"Buyer: {buyer.full_name} (@{buyer.username or '-'}) - ID {buyer.id}\n"
        f"Price charged: ${fmt_money(price)}\n"
        f"Remaining stock: {remaining}"
    )
    if remaining <= LOW_STOCK_ALERT:
        text += f"\n\n⚠️ LOW STOCK WARNING - only {remaining} left. Use /addstock to top up."
    try:
        await context.bot.send_message(STOCK_GROUP_ID, text)
    except Exception:
        logger.exception("Failed to notify stock group about a sale")


async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buy Now handler. Pops exactly one row out of `stock` atomically
    (deleted the instant it's sold, so it can never go to two buyers),
    charges the discounted price if the buyer has verified channel
    membership, writes an `orders` row, and delivers the credentials."""
    query = update.callback_query
    code = query.data.split("_", 1)[1]
    user = query.from_user
    lang = await get_user_lang(user.id) or "en"

    credentials = None
    price_charged = None
    product_row = None
    error_key = None

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                product_row = await conn.fetchrow("SELECT * FROM products WHERE code = $1", code)
                if not product_row:
                    await query.answer()
                    return

                user_row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user.id)
                balance = user_row["balance"] if user_row else Decimal("0")
                discount_eligible = user_row["discount_eligible"] if user_row else False
                price = effective_price(product_row["price"], discount_eligible)

                if balance < price:
                    error_key = "insufficient_balance"
                else:
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
                            price,
                            user.id,
                        )
                        await conn.execute(
                            """INSERT INTO orders (user_id, product_code, price, credentials)
                               VALUES ($1, $2, $3, $4)""",
                            user.id,
                            code,
                            price,
                            popped["credentials"],
                        )
                        credentials = popped["credentials"]
                        price_charged = price
    except Exception:
        logger.exception("buy_product failed for user %s product %s", user.id, code)
        await query.answer()
        await context.bot.send_message(user.id, tr("out_of_stock", lang))
        return

    await query.answer()

    if error_key:
        await context.bot.send_message(user.id, tr(error_key, lang))
        return

    await context.bot.send_message(
        user.id,
        tr("purchase_success", lang, credentials=html.escape(credentials), support=html.escape(SUPPORT_USERNAME)),
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
               FROM orders o JOIN products p ON p.code = o.product_code
               WHERE o.user_id = $1 ORDER BY o.created_at DESC LIMIT 20""",
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
               WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20""",
            user.id,
        )

    if not rows:
        await update.message.reply_text(tr("payment_history_empty", lang))
        return

    status_key_map = {"pending": "status_pending", "approved": "status_approved", "rejected": "status_rejected"}
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
# Add Funds conversation (customer side): amount -> KHQR -> receipt -> review
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

    try:
        khqr_file_id = await get_setting("khqr_file_id")
    except Exception:
        khqr_file_id = None

    caption = tr("qr_caption", lang, amount=fmt_money(amount))
    try:
        if khqr_file_id:
            await update.message.reply_photo(photo=khqr_file_id, caption=caption)
        else:
            await update.message.reply_photo(photo=generate_fallback_qr(amount), caption=caption)
    except Exception:
        logger.exception("Failed to send payment QR to user %s", update.effective_user.id)
        await update.message.reply_text(tr("qr_not_ready", lang, support=SUPPORT_USERNAME))

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

    try:
        async with db_pool.acquire() as conn:
            deposit_id = await conn.fetchval(
                """INSERT INTO deposits (user_id, amount, receipt_file_id, receipt_text)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                user.id,
                amount,
                photo_file_id,
                receipt_text,
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        caption = (
            f"New Deposit Request\n"
            f"Sent by: {user.full_name} (@{user.username or '-'}) - ID: {user.id}\n"
            f"Amount: ${fmt_money(amount)}\n"
            f"Time: {timestamp}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data=f"dep_ok_{deposit_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"dep_no_{deposit_id}"),
                ]
            ]
        )

        if photo_file_id:
            sent = await context.bot.send_photo(PAYMENT_GROUP_ID, photo_file_id, caption=caption, reply_markup=keyboard)
        else:
            sent = await context.bot.send_message(
                PAYMENT_GROUP_ID, caption + f"\nReceipt text: {receipt_text}", reply_markup=keyboard
            )

        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE deposits SET group_message_id = $1 WHERE id = $2", sent.message_id, deposit_id)

        await update.message.reply_text(tr("receipt_received", lang))
    except Exception:
        logger.exception("Failed to process deposit receipt for user %s", user.id)
        await update.message.reply_text(tr("receipt_received", lang))

    context.user_data.pop("deposit_amount", None)
    return ConversationHandler.END


async def add_funds_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("lang", "en")
    context.user_data.pop("deposit_amount", None)
    await update.message.reply_text(tr("cancel_done", lang))
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel sent OUTSIDE any active conversation."""
    if update.effective_chat.type == "private":
        lang = await get_user_lang(update.effective_user.id) or "en"
        await update.message.reply_text(tr("nothing_to_cancel", lang))
    else:
        await update.message.reply_text("Nothing to cancel.")


# ==============================================================================
# Deposit approval / decline (payment group admins only)
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

    try:
        async with db_pool.acquire() as conn:
            deposit = await conn.fetchrow("SELECT * FROM deposits WHERE id = $1", deposit_id)
            if not deposit or deposit["status"] != "pending":
                await query.answer("Already processed.", show_alert=True)
                return

            user_lang = (
                await conn.fetchval("SELECT language FROM users WHERE user_id = $1", deposit["user_id"]) or "en"
            )

            if approve:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        deposit["amount"],
                        deposit["user_id"],
                    )
                    await conn.execute(
                        "UPDATE deposits SET status = 'approved', resolved_at = now() WHERE id = $1", deposit_id
                    )
                    new_balance = await conn.fetchval(
                        "SELECT balance FROM users WHERE user_id = $1", deposit["user_id"]
                    )
                try:
                    await context.bot.send_message(
                        deposit["user_id"],
                        tr(
                            "deposit_approved_user",
                            user_lang,
                            amount=fmt_money(deposit["amount"]),
                            balance=fmt_money(new_balance),
                        ),
                    )
                except Exception:
                    logger.warning("Could not notify user %s of approved deposit", deposit["user_id"])
                stamp = f"\n\n✅ Confirmed by {query.from_user.full_name}"
            else:
                await conn.execute(
                    "UPDATE deposits SET status = 'rejected', resolved_at = now() WHERE id = $1", deposit_id
                )
                try:
                    await context.bot.send_message(
                        deposit["user_id"],
                        tr("deposit_rejected_user", user_lang, support=PAYMENT_SUPPORT_USERNAME),
                    )
                except Exception:
                    logger.warning("Could not notify user %s of declined deposit", deposit["user_id"])
                stamp = f"\n\n❌ Declined by {query.from_user.full_name}"
    except Exception:
        logger.exception("deposit_decision failed for deposit %s", deposit_id)
        await query.answer("Something went wrong. Check logs.", show_alert=True)
        return

    await query.answer()
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=(query.message.caption or "") + stamp)
        else:
            await query.edit_message_text(text=(query.message.text or "") + stamp)
    except Exception:
        logger.warning("Could not edit deposit message %s after decision", deposit_id)


async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stat in the payment group - total money successfully topped up."""
    if update.effective_chat.id != PAYMENT_GROUP_ID:
        return
    if not await is_group_admin(context, PAYMENT_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only admins can view stats.")
        return

    try:
        async with db_pool.acquire() as conn:
            total_approved = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'approved'"
            )
            approved_count = await conn.fetchval("SELECT COUNT(*) FROM deposits WHERE status = 'approved'")
            pending_count = await conn.fetchval("SELECT COUNT(*) FROM deposits WHERE status = 'pending'")
            declined_count = await conn.fetchval("SELECT COUNT(*) FROM deposits WHERE status = 'rejected'")
    except Exception:
        logger.exception("stat_cmd failed")
        await update.message.reply_text("⚠️ Could not load stats right now.")
        return

    text = (
        "💰 Payment Stats\n\n"
        f"Total successfully added: ${fmt_money(total_approved)}\n"
        f"Confirmed deposits: {approved_count}\n"
        f"Pending review: {pending_count}\n"
        f"Declined: {declined_count}"
    )
    await update.message.reply_text(text)


# ==============================================================================
# Stock-group: /addstock conversation (file-driven, smart field extraction)
# ==============================================================================
# Flow: /addstock -> pick a product (auto-skipped if there's only one) ->
# bot asks you to send a .txt FILE (no more pasting huge blocks of text)
# -> bot downloads it, splits it into individual accounts the same way as
# before (separated by --- lines), then runs each one through
# smart_extract_account() to keep only the fields that matter (Gmail,
# Password, ID, Name, Level, Hero Count, Skin Count, Banned, V2L Status,
# Collector Title) instead of storing the entire 50+ line raw dump ->
# bot tells you how many it found and asks how many to actually add ->
# inserts that many into the chosen product's stock.
async def addstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != STOCK_GROUP_ID:
        return ConversationHandler.END
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can add stock.")
        return ConversationHandler.END

    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")

    if not products:
        await update.message.reply_text("⚠️ No products exist yet. Use /addproduct first.")
        return ConversationHandler.END

    if len(products) == 1:
        context.user_data["addstock_product_code"] = products[0]["code"]
        context.user_data["addstock_product_name"] = products[0]["name"]
        await update.message.reply_text(
            f"📦 Adding stock to '{products[0]['name']}'.\n\n"
            f"📄 Send me the .txt file with the accounts now. I'll automatically pull out "
            f"the Gmail, Password, ID, Name, Level, Hero Count, Skin Count, Banned, "
            f"V2L Status, and Collector Title from each one.\n\n"
            f"Send /cancel to abort."
        )
        return ASK_STOCK_FILE

    buttons = [[InlineKeyboardButton(p["name"], callback_data=f"addstock_pick_{p['code']}")] for p in products]
    await update.message.reply_text("Which product is this stock for?", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_STOCK_PRODUCT


async def addstock_pick_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.split("addstock_pick_", 1)[1]

    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT name FROM products WHERE code = $1", code)
    name = product["name"] if product else code

    context.user_data["addstock_product_code"] = code
    context.user_data["addstock_product_name"] = name

    await query.edit_message_text(
        f"📦 Adding stock to '{name}'.\n\n"
        f"📄 Send me the .txt file with the accounts now. I'll automatically pull out "
        f"the Gmail, Password, ID, Name, Level, Hero Count, Skin Count, Banned, "
        f"V2L Status, and Collector Title from each one.\n\n"
        f"Send /cancel to abort."
    )
    return ASK_STOCK_FILE


async def addstock_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    if not document:
        await update.message.reply_text("⚠️ Please send the accounts as a .txt file, or /cancel to abort.")
        return ASK_STOCK_FILE

    try:
        tg_file = await document.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(out=buf)
        buf.seek(0)
        text = buf.read().decode("utf-8", errors="replace")
    except Exception:
        logger.exception("Failed to download/decode stock file %s", document.file_name)
        await update.message.reply_text(
            "⚠️ I couldn't read that file. Please make sure it's a plain .txt file and try again, "
            "or /cancel to abort."
        )
        return ASK_STOCK_FILE

    raw_entries = parse_stock_dump(text)
    if not raw_entries:
        await update.message.reply_text(
            "⚠️ I couldn't find any accounts in that file. Make sure entries are separated "
            "by a line of --- between each one, then send it again, or /cancel to abort."
        )
        return ASK_STOCK_FILE

    smart_entries = [smart_extract_account(block) for block in raw_entries]
    context.user_data["addstock_entries"] = smart_entries

    await update.message.reply_text(
        f"📄 Found {len(smart_entries)} account(s) in '{document.file_name}'.\n\n"
        f"How many do you want to add? Send a number (1-{len(smart_entries)}), or 'all'.\n"
        f"Send /cancel to abort."
    )
    return ASK_STOCK_QUANTITY


async def addstock_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    entries = context.user_data.get("addstock_entries", [])
    if not entries:
        await update.message.reply_text("⚠️ Something went wrong - please send the file again.")
        return ConversationHandler.END

    raw = update.message.text.strip().lower()
    if raw == "all":
        quantity = len(entries)
    else:
        try:
            quantity = int(raw)
        except ValueError:
            await update.message.reply_text(f"⚠️ Send a number between 1 and {len(entries)}, or 'all'.")
            return ASK_STOCK_QUANTITY

    if quantity < 1 or quantity > len(entries):
        await update.message.reply_text(f"⚠️ Send a number between 1 and {len(entries)}, or 'all'.")
        return ASK_STOCK_QUANTITY

    code = context.user_data.get("addstock_product_code")
    name = context.user_data.get("addstock_product_name", code)
    batch = entries[:quantity]
    leftover = len(entries) - len(batch)

    try:
        async with db_pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO stock (product_code, credentials) VALUES ($1, $2)",
                [(code, entry) for entry in batch],
            )
    except Exception:
        logger.exception("Failed to insert stock for product %s", code)
        await update.message.reply_text("⚠️ Something went wrong saving that batch. Please try again.")
        return ASK_STOCK_QUANTITY

    in_stock = await get_stock_count(code)
    summary = f"✅ Added {len(batch)} account(s) to '{name}'.\n📦 Now in stock: {in_stock}"
    if leftover > 0:
        summary += f"\n\nℹ️ {leftover} account(s) from the file were left over (not added)."

    await update.message.reply_text(summary)

    context.user_data.pop("addstock_entries", None)
    context.user_data.pop("addstock_product_code", None)
    context.user_data.pop("addstock_product_name", None)
    return ConversationHandler.END


async def addstock_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("addstock_entries", None)
    context.user_data.pop("addstock_product_code", None)
    context.user_data.pop("addstock_product_name", None)
    await update.message.reply_text("❎ Stock drop cancelled. Nothing was added.")
    return ConversationHandler.END


# ==============================================================================
# Stock-group: /clearstock conversation (pick product, pick quantity, confirm)
# ==============================================================================
# Deleting stock is permanent, so this always ends with an explicit Yes/No
# confirmation button before anything actually gets removed from the
# database - there's no way to accidentally wipe stock with a typo.
async def clearstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != STOCK_GROUP_ID:
        return ConversationHandler.END
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can clear stock.")
        return ConversationHandler.END

    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT code, name FROM products ORDER BY code")

    if not products:
        await update.message.reply_text("⚠️ No products exist yet.")
        return ConversationHandler.END

    if len(products) == 1:
        return await _clearstock_prompt_quantity(update, context, products[0]["code"], products[0]["name"])

    buttons = [[InlineKeyboardButton(p["name"], callback_data=f"clearstock_pick_{p['code']}")] for p in products]
    await update.message.reply_text(
        "Which product do you want to clear stock from?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CLEAR_ASK_PRODUCT


async def clearstock_pick_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.split("clearstock_pick_", 1)[1]

    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT name FROM products WHERE code = $1", code)
    name = product["name"] if product else code

    return await _clearstock_prompt_quantity(update, context, code, name, via_callback=True)


async def _clearstock_prompt_quantity(
    update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, name: str, via_callback: bool = False
) -> int:
    context.user_data["clearstock_product_code"] = code
    context.user_data["clearstock_product_name"] = name
    in_stock = await get_stock_count(code)

    if in_stock == 0:
        text = f"📦 '{name}' already has 0 accounts in stock - nothing to clear."
        if via_callback:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END

    text = (
        f"📦 '{name}' currently has {in_stock} account(s) in stock.\n\n"
        f"How many do you want to permanently remove? Send a number (1-{in_stock}), or 'all'.\n"
        f"Send /cancel to abort."
    )
    if via_callback:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    return CLEAR_ASK_QUANTITY


async def clearstock_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = context.user_data.get("clearstock_product_code")
    name = context.user_data.get("clearstock_product_name", code)
    in_stock = await get_stock_count(code)

    raw = update.message.text.strip().lower()
    if raw == "all":
        quantity = in_stock
    else:
        try:
            quantity = int(raw)
        except ValueError:
            await update.message.reply_text(f"⚠️ Send a number between 1 and {in_stock}, or 'all'.")
            return CLEAR_ASK_QUANTITY

    if quantity < 1 or quantity > in_stock:
        await update.message.reply_text(f"⚠️ Send a number between 1 and {in_stock}, or 'all'.")
        return CLEAR_ASK_QUANTITY

    context.user_data["clearstock_quantity"] = quantity
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, delete them", callback_data="clearstock_confirm_yes"),
                InlineKeyboardButton("❌ No, cancel", callback_data="clearstock_confirm_no"),
            ]
        ]
    )
    await update.message.reply_text(
        f"⚠️ This will permanently delete {quantity} account(s) from '{name}'. This cannot be undone.\n\nConfirm?",
        reply_markup=keyboard,
    )
    return CLEAR_CONFIRM


async def clearstock_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    code = context.user_data.get("clearstock_product_code")
    name = context.user_data.get("clearstock_product_name", code)
    quantity = context.user_data.get("clearstock_quantity", 0)

    if query.data == "clearstock_confirm_no":
        await query.edit_message_text("❎ Clear cancelled. Nothing was deleted.")
    else:
        try:
            async with db_pool.acquire() as conn:
                deleted_rows = await conn.fetch(
                    """DELETE FROM stock
                       WHERE id IN (
                           SELECT id FROM stock WHERE product_code = $1 ORDER BY id ASC LIMIT $2
                       )
                       RETURNING id""",
                    code,
                    quantity,
                )
            remaining = await get_stock_count(code)
            await query.edit_message_text(
                f"✅ Deleted {len(deleted_rows)} account(s) from '{name}'.\n📦 Remaining in stock: {remaining}"
            )
        except Exception:
            logger.exception("clearstock_confirm failed for product %s", code)
            await query.edit_message_text("⚠️ Something went wrong while deleting. Please try again.")

    context.user_data.pop("clearstock_product_code", None)
    context.user_data.pop("clearstock_product_name", None)
    context.user_data.pop("clearstock_quantity", None)
    return ConversationHandler.END


async def clearstock_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("clearstock_product_code", None)
    context.user_data.pop("clearstock_product_name", None)
    context.user_data.pop("clearstock_quantity", None)
    await update.message.reply_text("❎ Clear stock cancelled. Nothing was deleted.")
    return ConversationHandler.END



# ==============================================================================
# Stock-group: /khqr conversation (replace payment QR photo)
# ==============================================================================
async def khqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != STOCK_GROUP_ID:
        return ConversationHandler.END
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can update the KHQR photo.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📷 Send the new KHQR photo now. It will replace the old one immediately. /cancel to abort."
    )
    return ASK_KHQR_PHOTO


async def khqr_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("⚠️ Please send a photo, or /cancel to abort.")
        return ASK_KHQR_PHOTO

    file_id = update.message.photo[-1].file_id
    try:
        await set_setting("khqr_file_id", file_id)
    except Exception:
        logger.exception("Failed to save new KHQR photo")
        await update.message.reply_text("⚠️ Something went wrong saving that photo. Please try again.")
        return ASK_KHQR_PHOTO

    await update.message.reply_text("✅ KHQR updated. This will now be sent to customers when they Add Funds.")
    return ConversationHandler.END


async def khqr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❎ Cancelled. KHQR photo was not changed.")
    return ConversationHandler.END


# ==============================================================================
# Stock-group: simple one-shot admin commands
# ==============================================================================
async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setprice <code> <price>  (alias: /productprice)"""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can change prices.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setprice <product_code> <price>")
        return

    code, price_raw = context.args
    try:
        price = Decimal(price_raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await update.message.reply_text("Invalid price.")
        return

    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE products SET price = $1 WHERE code = $2", price, code)
    except Exception:
        logger.exception("setprice_cmd failed")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")
        return

    if result == "UPDATE 0":
        await update.message.reply_text(f"⚠️ Product '{code}' not found.")
    else:
        await update.message.reply_text(f"✅ Price for '{code}' set to ${fmt_money(price)}.")


async def addproduct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /addproduct <code> <price> <name...>"""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can add products.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: /addproduct <code> <price> <name...>")
        return

    code, price_raw = context.args[0], context.args[1]
    name = " ".join(context.args[2:])
    try:
        price = Decimal(price_raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await update.message.reply_text("Invalid price.")
        return

    try:
        async with db_pool.acquire() as conn:
            existing = await conn.fetchval("SELECT 1 FROM products WHERE code = $1", code)
            if existing:
                await update.message.reply_text(f"⚠️ Product '{code}' already exists.")
                return
            await conn.execute(
                """INSERT INTO products (code, name, description_en, description_km, price)
                   VALUES ($1, $2, $3, $4, $5)""",
                code,
                name,
                "",
                "",
                price,
            )
    except Exception:
        logger.exception("addproduct_cmd failed")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")
        return

    await update.message.reply_text(
        f"✅ Product '{name}' ({code}) created at ${fmt_money(price)}. Use /addstock to add inventory."
    )


async def stocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != STOCK_GROUP_ID:
        return

    try:
        async with db_pool.acquire() as conn:
            products = await conn.fetch("SELECT code, name, price FROM products ORDER BY code")
    except Exception:
        logger.exception("stocklist_cmd failed")
        await update.message.reply_text("⚠️ Could not load stock list right now.")
        return

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
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can view stats.")
        return

    try:
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
            total_revenue = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM orders")
            total_topped_up = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'approved'"
            )
            pending_deposits = await conn.fetchval("SELECT COUNT(*) FROM deposits WHERE status = 'pending'")
            discount_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE discount_eligible = TRUE")
    except Exception:
        logger.exception("stats_cmd failed")
        await update.message.reply_text("⚠️ Could not load stats right now.")
        return

    text = (
        "📊 Vinzy Shop Stats\n\n"
        f"Total users: {total_users}\n"
        f"Total orders: {total_orders}\n"
        f"Total revenue: ${fmt_money(total_revenue)}\n"
        f"Total topped up (approved): ${fmt_money(total_topped_up)}\n"
        f"Pending deposits: {pending_deposits}\n"
        f"Verified channel members (discount active): {discount_users}"
    )
    await update.message.reply_text(text)


async def addbal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /addbal <user_id> <amount>"""
    await _adjust_balance(update, context, direction=1)


async def removebal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /removebal <user_id> <amount>"""
    await _adjust_balance(update, context, direction=-1)


async def _adjust_balance(update: Update, context: ContextTypes.DEFAULT_TYPE, direction: int) -> None:
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can adjust balances.")
        return

    cmd_name = "/addbal" if direction > 0 else "/removebal"
    if len(context.args) != 2:
        await update.message.reply_text(f"Usage: {cmd_name} <user_id> <amount>")
        return

    try:
        target_id = int(context.args[0])
        amount = Decimal(context.args[1]).quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation):
        await update.message.reply_text("Invalid user_id or amount.")
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be greater than 0.")
        return

    try:
        async with db_pool.acquire() as conn:
            user_row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", target_id)
            if not user_row:
                await update.message.reply_text("⚠️ That user hasn't started the bot yet.")
                return

            async with conn.transaction():
                if direction > 0:
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target_id
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET balance = GREATEST(balance - $1, 0) WHERE user_id = $2",
                        amount,
                        target_id,
                    )
                new_balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", target_id)
                await conn.execute(
                    """INSERT INTO balance_log (user_id, admin_id, amount, action)
                       VALUES ($1, $2, $3, $4)""",
                    target_id,
                    update.effective_user.id,
                    amount,
                    "add" if direction > 0 else "remove",
                )
    except Exception:
        logger.exception("_adjust_balance failed")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")
        return

    lang = user_row["language"] or "en"
    key = "balance_added_notify" if direction > 0 else "balance_removed_notify"
    try:
        await context.bot.send_message(target_id, tr(key, lang, amount=fmt_money(amount), balance=fmt_money(new_balance)))
    except Exception:
        logger.warning("Could not notify user %s about balance adjustment", target_id)

    verb = "added to" if direction > 0 else "removed from"
    await update.message.reply_text(f"✅ ${fmt_money(amount)} {verb} user {target_id}. New balance: ${fmt_money(new_balance)}.")


async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /announce <text> - broadcast to every user who has used the bot."""
    if update.effective_chat.id != STOCK_GROUP_ID:
        return
    if not await is_group_admin(context, STOCK_GROUP_ID, update.effective_user.id):
        await update.message.reply_text("Only group admins can send announcements.")
        return

    text = update.message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text("Usage: /announce <message text>")
        return

    message = text[1].strip()

    try:
        async with db_pool.acquire() as conn:
            user_ids = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users")]
    except Exception:
        logger.exception("announce_cmd failed to load users")
        await update.message.reply_text("⚠️ Could not load the user list right now.")
        return

    await update.message.reply_text(f"📢 Sending to {len(user_ids)} users...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📢 Announcement\n\n{message}")
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Announcement sent to {sent} users ({failed} failed - likely blocked the bot).")


# ==============================================================================
# Global error handler - keeps the bot alive no matter what goes wrong in a
# single update; this is the safety net behind every try/except above.
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
        logger.warning("STOCK_GROUP_ID is not set - stock-group commands will not work.")
    await init_db()


def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    private_only = filters.ChatType.PRIVATE
    stock_chat = filters.Chat(STOCK_GROUP_ID) if STOCK_GROUP_ID else filters.ALL

    add_funds_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                private_only & filters.Text([TXT["btn_addfunds"]["en"], TXT["btn_addfunds"]["km"]]),
                add_funds_start,
            )
        ],
        states={
            ASK_AMOUNT: [MessageHandler(private_only & filters.TEXT & ~filters.COMMAND, add_funds_amount)],
            ASK_RECEIPT: [
                MessageHandler(private_only & (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)), add_funds_receipt)
            ],
        },
        fallbacks=[CommandHandler("cancel", add_funds_cancel, filters=private_only)],
    )

    addstock_conversation = ConversationHandler(
        entry_points=[CommandHandler("addstock", addstock_cmd, filters=stock_chat)],
        states={
            ASK_STOCK_PRODUCT: [CallbackQueryHandler(addstock_pick_product, pattern="^addstock_pick_")],
            ASK_STOCK_FILE: [MessageHandler(stock_chat & filters.Document.ALL, addstock_receive_file)],
            ASK_STOCK_QUANTITY: [MessageHandler(stock_chat & filters.TEXT & ~filters.COMMAND, addstock_quantity)],
        },
        fallbacks=[CommandHandler("cancel", addstock_cancel, filters=stock_chat)],
    )

    khqr_conversation = ConversationHandler(
        entry_points=[CommandHandler("khqr", khqr_cmd, filters=stock_chat)],
        states={ASK_KHQR_PHOTO: [MessageHandler(stock_chat & filters.PHOTO, khqr_receive_photo)]},
        fallbacks=[CommandHandler("cancel", khqr_cancel, filters=stock_chat)],
    )

    clearstock_conversation = ConversationHandler(
        entry_points=[CommandHandler("clearstock", clearstock_cmd, filters=stock_chat)],
        states={
            CLEAR_ASK_PRODUCT: [CallbackQueryHandler(clearstock_pick_product, pattern="^clearstock_pick_")],
            CLEAR_ASK_QUANTITY: [MessageHandler(stock_chat & filters.TEXT & ~filters.COMMAND, clearstock_quantity)],
            CLEAR_CONFIRM: [CallbackQueryHandler(clearstock_confirm, pattern="^clearstock_confirm_")],
        },
        fallbacks=[CommandHandler("cancel", clearstock_cancel, filters=stock_chat)],
    )

    # Order matters: commands and conversations before the catch-all router.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["help", "commands"], help_command))
    application.add_handler(CommandHandler("myid", myid_cmd))
    application.add_handler(CallbackQueryHandler(set_language_callback, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
    application.add_handler(CallbackQueryHandler(skip_join_callback, pattern="^skip_join$"))
    application.add_handler(CallbackQueryHandler(unlock_discount_callback, pattern="^unlock_discount$"))

    application.add_handler(add_funds_conversation)
    application.add_handler(addstock_conversation)
    application.add_handler(khqr_conversation)
    application.add_handler(clearstock_conversation)
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(CommandHandler("setprice", setprice_cmd))
    application.add_handler(CommandHandler("productprice", setprice_cmd))
    application.add_handler(CommandHandler("addproduct", addproduct_cmd))
    application.add_handler(CommandHandler("stocklist", stocklist_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("addbal", addbal_cmd))
    application.add_handler(CommandHandler("removebal", removebal_cmd))
    application.add_handler(CommandHandler("announce", announce_cmd))
    application.add_handler(CommandHandler("stat", stat_cmd))

    application.add_handler(CallbackQueryHandler(show_product_detail, pattern="^prod_"))
    application.add_handler(CallbackQueryHandler(back_to_products, pattern="^back_products$"))
    application.add_handler(CallbackQueryHandler(buy_product, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(deposit_decision, pattern="^dep_"))

    application.add_handler(MessageHandler(private_only & filters.TEXT & ~filters.COMMAND, menu_router))

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required.")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL environment variable is required.")

    application = build_application()
    logger.info("Vinzy Shop bot starting (worker / long polling, no web port)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# ==============================================================================
# NOTES
# ==============================================================================
# - This is a WORKER process: it never listens on a port. Deploy it on
#   Koyeb as a Worker service type, not a Web service.
# - The bot must be added as a member (ideally admin) of the promo channel
#   (CHANNEL_USERNAME) for the "Verify" button to be able to check joins.
# - Every external/DB operation that could plausibly fail (group sends,
#   broadcasts, DB writes during admin flows) is wrapped in try/except so
#   one failure never takes the whole bot down; uncaught errors anywhere
#   else are still caught by error_handler and logged instead of crashing.
# - Discount is recomputed fresh from the database on every product view
#   and every purchase - there's no cached/stale discounted price anywhere.
