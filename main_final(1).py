import os
import sqlite3
import logging
import uuid

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    BusinessConnectionHandler,
    PreCheckoutQueryHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

DB = os.getenv("DB_PATH", "/data/bot.db")
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

GIFT_ACCOUNT_USERNAME = "@DragonBos69"
GIFT_ACCOUNT_URL = "https://t.me/DragonBos69"
ALLOWED_GIFT_PRICES = (15, 25, 50)

ADD_VIDEO, ADD_NAME, ADD_PRICE, ADD_FREE = range(4)
BULK_UPLOAD, BULK_NAME, BULK_PRICE = range(4, 7)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def get_db():
    connection = sqlite3.connect(DB, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def add_column_if_missing(connection, table, column, definition):
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    connection = get_db()

    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            approved_count INTEGER NOT NULL DEFAULT 0,
            approved_total INTEGER NOT NULL DEFAULT 0,
            free_attempts INTEGER NOT NULL DEFAULT 0,
            submitted_count INTEGER NOT NULL DEFAULT 0,
            viewed_count INTEGER NOT NULL DEFAULT 0,
            search_count INTEGER NOT NULL DEFAULT 0,
            donation_count INTEGER NOT NULL DEFAULT 0,
            donated_stars INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            is_free_prize INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            amount INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS donations (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bulk_batches (
            batch_id TEXT PRIMARY KEY,
            admin_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'uploading',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bulk_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            admin_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            name TEXT,
            price INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS gift_orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            expected_price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            owned_gift_id TEXT,
            gift_id TEXT,
            gift_star_count INTEGER,
            sender_user_id INTEGER,
            gift_message_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS business_connections (
            connection_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            can_view_gifts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Safe migration for the original database.
    add_column_if_missing(connection, "users", "approved_total", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "submitted_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "viewed_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "search_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "donation_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "donated_stars", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "submissions", "title", "TEXT")
    add_column_if_missing(connection, "content", "media_type", "TEXT NOT NULL DEFAULT 'video'")
    add_column_if_missing(connection, "bulk_queue", "media_type", "TEXT NOT NULL DEFAULT 'video'")

    connection.commit()
    connection.close()


def ensure_user(user_id):
    connection = get_db()
    connection.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (user_id,),
    )
    connection.commit()
    connection.close()


async def send_catalog_media(bot, chat_id, media_type, file_id, caption=None, reply_markup=None):
    """Отправляет сохранённый материал независимо от того, видео это или фото."""
    if media_type == "photo":
        return await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup,
        )
    return await bot.send_video(
        chat_id=chat_id,
        video=file_id,
        caption=caption,
        reply_markup=reply_markup,
    )


# =========================================================
# KEYBOARDS
# =========================================================

def bottom_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["☰ Меню", "🔎 Поиск"],
            ["👤 Профиль"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Видео и фото", callback_data="catalog")],
        [InlineKeyboardButton("⭐ Поддержать бота", callback_data="donate")],
        [InlineKeyboardButton("🎁 Бесплатный доступ", callback_data="free")],
        [InlineKeyboardButton("📤 Отправить видео", callback_data="submit")],
    ])


def catalog_keyboard(videos):
    keyboard = []

    for video in videos:
        name = video["name"].strip() or f"Контент #{video['id']}"
        # Telegram ограничивает текст inline-кнопки по ширине, поэтому
        # слишком длинные названия аккуратно сокращаем.
        if len(name) > 48:
            name = name[:45] + "…"

        keyboard.append([
            InlineKeyboardButton(
                f"🎬 {name}",
                callback_data=f"content:{video['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ Главное меню", callback_data="home")
    ])
    return InlineKeyboardMarkup(keyboard)


def back_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="home")]
    ])


# =========================================================
# START / HELP
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)

    await update.message.reply_text(
        "🎬 <b>Добро пожаловать!</b>\n\n"
        "Здесь ты можешь выбрать видео или фото из каталога.\n"
        "Сначала выбери контент — <b>цена появится после выбора</b>.\n\n"
        "👇 Нажми, чтобы открыть каталог:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )

    await update.message.reply_text(
        "Навигация:",
        reply_markup=bottom_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — главное меню\n"
        "/terms — условия\n"
        "/paysupport — помощь с оплатой подарками\n"
        "/donate — поддержать бота Stars\n"
        "/cancel — отменить действие",
        reply_markup=bottom_keyboard(),
    )


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 Условия использования\n\n"
        "Покупая цифровой контент через бота, пользователь подтверждает "
        "ознакомление с условиями покупки.\n\n"
        "Контент предоставляется в цифровом виде после подтверждения оплаты.",
        reply_markup=bottom_keyboard(),
    )


async def pay_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 Поддержка по оплате\n\n"
        f"Оплата контента выполняется подарком в Telegram на {GIFT_ACCOUNT_USERNAME}.\n"
        "Если подарок отправлен, но видео не пришло — обратись к администратору.",
        reply_markup=bottom_keyboard(),
    )


async def donate_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton("⭐ 15", callback_data="donate:15"),
            InlineKeyboardButton("⭐ 25", callback_data="donate:25"),
            InlineKeyboardButton("⭐ 50", callback_data="donate:50"),
        ],
        [
            InlineKeyboardButton("⭐ 100", callback_data="donate:100"),
            InlineKeyboardButton("⭐ 250", callback_data="donate:250"),
        ],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="home")],
    ]

    await query.edit_message_text(
        "⭐ <b>Поддержать бота</b>\n\n"
        "Спасибо! Выбери сумму поддержки в Telegram Stars.\n"
        "Это добровольный донат и не связан с покупкой видео или фото.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⭐ <b>Поддержать бота</b>\n\n"
        "Выбери сумму поддержки:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⭐ 15", callback_data="donate:15"),
                InlineKeyboardButton("⭐ 25", callback_data="donate:25"),
                InlineKeyboardButton("⭐ 50", callback_data="donate:50"),
            ],
            [
                InlineKeyboardButton("⭐ 100", callback_data="donate:100"),
                InlineKeyboardButton("⭐ 250", callback_data="donate:250"),
            ],
        ]),
    )


async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        amount = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text("Некорректная сумма доната.")
        return

    if amount <= 0 or amount > 10000:
        await query.message.reply_text("Некорректная сумма доната.")
        return

    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title="Поддержка бота",
        description=f"Добровольная поддержка бота на {amount} Telegram Stars.",
        payload=f"donation:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Поддержка бота", amount=amount)],
    )


async def donation_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    if not query.invoice_payload.startswith("donation:"):
        return

    try:
        amount = int(query.invoice_payload.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer(ok=False, error_message="Некорректный донат.")
        return

    if query.currency != "XTR" or query.total_amount != amount:
        await query.answer(ok=False, error_message="Сумма доната не совпадает.")
        return

    await query.answer(ok=True)


async def donation_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    if not payment or not payment.invoice_payload.startswith("donation:"):
        return

    amount = payment.total_amount
    user_id = update.effective_user.id
    charge_id = payment.telegram_payment_charge_id

    connection = get_db()
    connection.execute(
        "INSERT OR IGNORE INTO donations(charge_id, user_id, amount) VALUES(?, ?, ?)",
        (charge_id, user_id, amount),
    )
    connection.execute(
        """UPDATE users
           SET donation_count = donation_count + 1,
               donated_stars = donated_stars + ?
           WHERE user_id = ?""",
        (amount, user_id),
    )
    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"❤️ Спасибо за поддержку! Ты отправил {amount} ⭐.\n\n"
        "Твой донат помогает развивать бота.",
        reply_markup=bottom_keyboard(),
    )


# =========================================================
# CATALOG
# =========================================================

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    connection = get_db()
    videos = connection.execute(
        """
        SELECT id, name
        FROM content
        WHERE active = 1
        ORDER BY id
        """
    ).fetchall()
    connection.close()

    if not videos:
        await query.edit_message_text(
            "📂 <b>Каталог пока пуст</b>\n\n"
            "Новые видео и фото появятся здесь позже.",
            parse_mode="HTML",
            reply_markup=back_home_keyboard(),
        )
        return

    await query.edit_message_text(
        "🎬 <b>Видео и фото</b>\n\n"
        "Выбери нужный материал из каталога.\n"
        "<i>Цена будет показана после выбора.</i>",
        parse_mode="HTML",
        reply_markup=catalog_keyboard(videos),
    )


# =========================================================
# LEGACY PRICE CATEGORIES
# =========================================================

async def show_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    price = int(query.data.split(":")[1])

    connection = get_db()
    videos = connection.execute(
        """
        SELECT id, name
        FROM content
        WHERE price = ?
          AND active = 1
        ORDER BY id
        """,
        (price,),
    ).fetchall()
    connection.close()

    keyboard = []
    for video in videos:
        keyboard.append([
            InlineKeyboardButton(
                video["name"],
                callback_data=f"content:{video['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="home")
    ])

    if not videos:
        text = f"Пока нет контента за подарок {price} ⭐."
    else:
        text = f"🎬 Контент за подарок {price} ⭐:"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# CONTENT
# =========================================================

async def show_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    content_id = int(query.data.split(":")[1])

    connection = get_db()
    video = connection.execute(
        """
        SELECT id, name, price
        FROM content
        WHERE id = ?
          AND active = 1
        """,
        (content_id,),
    ).fetchone()
    connection.close()

    if not video:
        await query.edit_message_text(
            "Этот контент больше недоступен.",
            reply_markup=back_home_keyboard(),
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"🎁 Оплатить подарком {video['price']} ⭐",
                callback_data=f"buy:{video['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Вернуться в каталог",
                callback_data="catalog",
            )
        ],
    ]

    await query.edit_message_text(
        "🎬 <b>Выбранный контент</b>\n\n"
        f"<b>{video['name']}</b>\n\n"
        f"💰 <b>Цена: подарок {video['price']} ⭐</b>\n\n"
        "После оплаты подарок нужно отправить на @DragonBos69.\n"
        "Видео будет выдано автоматически после подтверждения подарка.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# TELEGRAM GIFTS PAYMENT
# =========================================================

async def buy_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    content_id = int(query.data.split(":")[1])
    user_id = query.from_user.id

    connection = get_db()
    video = connection.execute(
        """
        SELECT id, name, price
        FROM content
        WHERE id = ?
          AND active = 1
        """,
        (content_id,),
    ).fetchone()

    if not video:
        connection.close()
        await query.message.reply_text("Контент больше недоступен.")
        return

    if video["price"] not in ALLOWED_GIFT_PRICES:
        connection.close()
        await query.message.reply_text("Для этого контента сейчас недоступна оплата подарком.")
        return

    # Только один активный заказ на пользователя — так подарок
    # однозначно относится к выбранному видео.
    connection.execute(
        "UPDATE gift_orders SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )

    cursor = connection.execute(
        """
        INSERT INTO gift_orders(user_id, content_id, expected_price)
        VALUES (?, ?, ?)
        """,
        (user_id, content_id, video["price"]),
    )
    order_id = cursor.lastrowid
    connection.commit()
    connection.close()

    keyboard = [[
        InlineKeyboardButton(
            f"🎁 Открыть {GIFT_ACCOUNT_USERNAME}",
            url=GIFT_ACCOUNT_URL,
        )
    ]]

    await query.message.reply_text(
        f"🎁 Заказ #{order_id} создан\n\n"
        f"🎬 {video['name']}\n"
        f"💰 Цена: подарок {video['price']} ⭐\n\n"
        f"Отправь подарок стоимостью ровно {video['price']} ⭐ "
        f"на аккаунт {GIFT_ACCOUNT_USERNAME}.\n\n"
        "Подойдут обычные Telegram-подарки этой стоимости.\n"
        "После получения подарка бот автоматически проверит отправителя "
        "и выдаст видео. Ничего дополнительно писать не нужно.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    connection_update = update.business_connection
    if not connection_update:
        return

    rights = connection_update.rights
    can_view_gifts = bool(rights and rights.can_view_gifts_and_stars)

    connection = get_db()
    connection.execute(
        """
        INSERT INTO business_connections(
            connection_id, user_id, username, is_enabled, can_view_gifts, updated_at
        )
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(connection_id) DO UPDATE SET
            user_id = excluded.user_id,
            username = excluded.username,
            is_enabled = excluded.is_enabled,
            can_view_gifts = excluded.can_view_gifts,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            connection_update.id,
            connection_update.user.id,
            connection_update.user.username,
            1 if connection_update.is_enabled else 0,
            1 if can_view_gifts else 0,
        ),
    )
    connection.commit()
    connection.close()

    logger.info(
        "Business connection: user=%s username=@%s enabled=%s view_gifts=%s",
        connection_update.user.id,
        connection_update.user.username or "",
        connection_update.is_enabled,
        can_view_gifts,
    )


async def business_gift_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматически принимает подарок, пришедший на подключённый Business-аккаунт."""
    message = update.business_message
    if not message or not message.gift:
        return

    gift_info = message.gift
    gift = gift_info.gift
    sender = message.from_user

    if not sender:
        logger.warning("Получен подарок без известного отправителя: message_id=%s", message.message_id)
        return

    gift_price = gift.star_count
    owned_gift_id = gift_info.owned_gift_id
    gift_id = gift.id
    user_id = sender.id

    logger.info(
        "Business gift received: sender=%s gift_id=%s stars=%s owned_gift_id=%s",
        user_id, gift_id, gift_price, owned_gift_id,
    )

    connection = get_db()

    # Защита от повторной обработки одного и того же подарка.
    if owned_gift_id:
        already_used = connection.execute(
            "SELECT order_id FROM gift_orders WHERE owned_gift_id = ? LIMIT 1",
            (owned_gift_id,),
        ).fetchone()
        if already_used:
            connection.close()
            logger.info("Gift %s already processed for order #%s", owned_gift_id, already_used["order_id"])
            return

    order = connection.execute(
        """
        SELECT go.order_id, go.content_id, go.expected_price, c.name, c.file_id, c.media_type, c.active
        FROM gift_orders go
        JOIN content c ON c.id = go.content_id
        WHERE go.user_id = ?
          AND go.status = 'pending'
        ORDER BY go.order_id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    if not order:
        connection.close()
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎁 Подарок получен на {GIFT_ACCOUNT_USERNAME}, но активного заказа от тебя не найдено.\n\n"
                "Если ты хотел купить видео, создай заказ в боте заново."
            ),
        )
        return

    if not order["active"]:
        connection.execute(
            "UPDATE gift_orders SET status = 'cancelled' WHERE order_id = ?",
            (order["order_id"],),
        )
        connection.commit()
        connection.close()
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Видео из этого заказа больше недоступно. Обратись к администратору.",
        )
        return

    if gift_price != order["expected_price"]:
        connection.close()
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ Подарок получен, но его стоимость не совпадает с заказом.\n\n"
                f"Заказ #{order['order_id']}: {order['expected_price']} ⭐\n"
                f"Получено: {gift_price} ⭐\n\n"
                "Видео пока не выдано. Если подарок отправлен по ошибке, напиши администратору."
            ),
        )
        return

    # Помечаем подарок оплаченным до выдачи видео, чтобы повторное обновление
    # не могло выдать один и тот же материал дважды.
    connection.execute(
        """
        UPDATE gift_orders
        SET status = 'paid',
            owned_gift_id = ?,
            gift_id = ?,
            gift_star_count = ?,
            sender_user_id = ?,
            gift_message_id = ?
        WHERE order_id = ? AND status = 'pending'
        """,
        (
            owned_gift_id,
            gift_id,
            gift_price,
            user_id,
            message.message_id,
            order["order_id"],
        ),
    )
    changed = connection.execute("SELECT changes() AS changed").fetchone()["changed"]
    connection.commit()
    connection.close()

    if changed != 1:
        return

    try:
        await send_catalog_media(
            context.bot, user_id, order["media_type"], order["file_id"],
            caption=f"🎬 {order['name']}",
        )
    except TelegramError:
        logger.exception("Не удалось выдать видео по подарку, order #%s", order["order_id"])
        connection = get_db()
        connection.execute(
            "UPDATE gift_orders SET status = 'delivery_failed' WHERE order_id = ?",
            (order["order_id"],),
        )
        connection.commit()
        connection.close()
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Подарок принят по заказу #{order['order_id']}, но видео временно не отправилось.\n"
                "Администратор сможет повторить выдачу. Подарок повторно отправлять не нужно."
            ),
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "⚠️ Ошибка выдачи видео после подарка\n"
                        f"Заказ: #{order['order_id']}\n"
                        f"User ID: {user_id}\n"
                        f"Видео ID: {order['content_id']}\n"
                        f"Gift ID: {gift_id}\n"
                        f"Gift owned ID: {owned_gift_id}"
                    ),
                )
            except TelegramError:
                logger.exception("Не удалось уведомить админа %s", admin_id)
        return

    connection = get_db()
    connection.execute(
        "UPDATE gift_orders SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
        (order["order_id"],),
    )
    connection.execute(
        "UPDATE users SET viewed_count = viewed_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    connection.commit()
    connection.close()

    for admin_id in ADMIN_IDS:
        try:
            sender_name = sender.username or sender.full_name
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🎁 Новая оплата подарком\n\n"
                    f"Заказ: #{order['order_id']}\n"
                    f"User ID: {user_id} (@{sender_name if sender.username else 'без_username'})\n"
                    f"Видео: #{order['content_id']} — {order['name']}\n"
                    f"Подарок: {gift_id} — {gift_price} ⭐"
                ),
            )
        except TelegramError:
            logger.exception("Не удалось уведомить админа %s о подарке", admin_id)


async def gift_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    connection = get_db()
    row = connection.execute(
        """
        SELECT connection_id, user_id, username, is_enabled, can_view_gifts
        FROM business_connections
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    connection.close()

    if not row or not row["is_enabled"]:
        await update.message.reply_text(
            "❌ Business-аккаунт пока не подключён к боту.\n\n"
            "Сначала включи Secretary Mode в @BotFather и подключи бота к @DragonBos69."
        )
        return

    rights_text = "✅ View Gifts" if row["can_view_gifts"] else "❌ View Gifts"

    try:
        gifts = await context.bot.get_business_account_gifts(
            business_connection_id=row["connection_id"],
            limit=1,
        )
        gift_count = gifts.total_count
        api_text = "✅ Telegram Gifts API доступен"
    except TelegramError as exc:
        gift_count = "?"
        api_text = f"❌ Telegram Gifts API: {exc}"

    await update.message.reply_text(
        "🎁 Статус оплаты подарками\n\n"
        f"Аккаунт: {GIFT_ACCOUNT_USERNAME}\n"
        f"Business connection: {row['connection_id']}\n"
        f"{rights_text}\n"
        f"Подарков на аккаунте: {gift_count}\n"
        f"{api_text}"
    )


# =========================================================
# USER SUBMISSIONS
# =========================================================

async def start_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_submission"] = True

    await query.message.reply_text(
        "📤 Отправь своё видео.\n\n"
        "После отправки оно сразу попадёт админу на проверку.\n\n"
        "🎁 За каждые 5 одобренных видео ты получаешь возможность "
        "один раз бесплатно посмотреть любой материал.\n\n"
        "ℹ️ Ничего заполнять не нужно — просто отправь видео. "
        "Название, если оно понадобится, администратор добавит сам после проверки.",
        reply_markup=bottom_keyboard(),
    )


async def receive_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.user_data.get("waiting_submission"):
        return

    if not update.message.video:
        return

    context.user_data.pop("waiting_submission", None)

    user_id = update.effective_user.id
    file_id = update.message.video.file_id
    title = "Без названия"

    ensure_user(user_id)

    connection = get_db()
    cursor = connection.execute(
        """
        INSERT INTO submissions(user_id, file_id, title)
        VALUES (?, ?, ?)
        """,
        (user_id, file_id, title),
    )
    submission_id = cursor.lastrowid

    connection.execute(
        """
        UPDATE users
        SET submitted_count = submitted_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    connection.commit()
    connection.close()

    keyboard = [[
        InlineKeyboardButton(
            "✅ Одобрить",
            callback_data=f"approve:{submission_id}",
        ),
        InlineKeyboardButton(
            "❌ Отклонить",
            callback_data=f"reject:{submission_id}",
        ),
    ]]

    admin_caption = (
        f"📥 Новая заявка #{submission_id}\n"
        f"🎬 Название: пока не задано\n"
        f"👤 User ID: {user_id}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_video(
                chat_id=admin_id,
                video=file_id,
                caption=admin_caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError:
            logger.exception("Не удалось отправить заявку админу %s", admin_id)

    await update.message.reply_text(
        f"✅ Видео отправлено на проверку.\n\n"
        f"Номер заявки: #{submission_id}\n"
        "Название заполнять не нужно — администратор при необходимости добавит его сам после проверки.",
        reply_markup=bottom_keyboard(),
    )


# =========================================================
# ADMIN — NAME APPROVED SUBMISSION
# =========================================================

async def start_submission_naming(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return

    await query.answer()

    submission_id = int(query.data.split(":")[1])
    context.user_data["waiting_admin_submission_title"] = submission_id

    await query.message.reply_text(
        f"✏️ Напиши название для заявки #{submission_id}.\n\n"
        "Это название будет сохранено за заявкой и отправлено пользователю.",
    )


async def receive_admin_submission_title(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    submission_id = context.user_data.get("waiting_admin_submission_title")

    if not submission_id or update.effective_user.id not in ADMIN_IDS:
        return

    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("Название не может быть пустым.")
        return

    title = title[:200]
    context.user_data.pop("waiting_admin_submission_title", None)

    connection = get_db()
    submission = connection.execute(
        """
        SELECT user_id, status
        FROM submissions
        WHERE id = ?
        """,
        (submission_id,),
    ).fetchone()

    if not submission:
        connection.close()
        await update.message.reply_text("Заявка не найдена.")
        return

    connection.execute(
        """
        UPDATE submissions
        SET title = ?
        WHERE id = ?
        """,
        (title, submission_id),
    )
    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"✅ Название для заявки #{submission_id} сохранено:\n🎬 {title}"
    )

    try:
        await context.bot.send_message(
            chat_id=submission["user_id"],
            text=(
                f"✏️ Для твоего одобренного видео #{submission_id} "
                f"администратор установил название:\n\n🎬 {title}"
            ),
            reply_markup=bottom_keyboard(),
        )
    except TelegramError:
        logger.exception("Не удалось отправить пользователю название заявки %s", submission_id)


# =========================================================
# MODERATION
# =========================================================

async def moderate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return

    await query.answer()

    action, submission_id = query.data.split(":")
    submission_id = int(submission_id)

    connection = get_db()
    submission = connection.execute(
        """
        SELECT user_id, title, status
        FROM submissions
        WHERE id = ?
        """,
        (submission_id,),
    ).fetchone()

    if not submission:
        connection.close()
        await query.edit_message_caption(caption="Заявка не найдена.")
        return

    if submission["status"] != "pending":
        connection.close()
        await query.edit_message_caption(caption="Заявка уже обработана.")
        return

    user_id = submission["user_id"]
    title = submission["title"] or "Без названия"

    if action == "reject":
        connection.execute(
            """
            UPDATE submissions
            SET status = 'rejected'
            WHERE id = ?
            """,
            (submission_id,),
        )
        connection.commit()
        connection.close()

        await query.edit_message_caption(
            caption=f"❌ Заявка #{submission_id} отклонена.\n🎬 {title}"
        )
        await context.bot.send_message(
            user_id,
            f"❌ Твоё видео #{submission_id} было отклонено.\n"
            f"🎬 {title}",
            reply_markup=bottom_keyboard(),
        )
        return

    ensure_user(user_id)

    connection.execute(
        """
        UPDATE submissions
        SET status = 'approved'
        WHERE id = ?
        """,
        (submission_id,),
    )

    connection.execute(
        """
        UPDATE users
        SET approved_count = approved_count + 1,
            approved_total = approved_total + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    user = connection.execute(
        """
        SELECT approved_count
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    reward = False

    if user["approved_count"] >= 5:
        connection.execute(
            """
            UPDATE users
            SET approved_count = approved_count - 5,
                free_attempts = free_attempts + 1
            WHERE user_id = ?
            """,
            (user_id,),
        )
        reward = True

    connection.commit()
    connection.close()

    name_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✏️ Дать название",
            callback_data=f"name_submission:{submission_id}",
        )
    ]])

    await query.edit_message_caption(
        caption=(
            f"✅ Заявка #{submission_id} одобрена.\n"
            "🎬 Название: пока не задано"
        ),
        reply_markup=name_keyboard,
    )

    if reward:
        await context.bot.send_message(
            user_id,
            "🎁 Поздравляем!\n\n"
            "Твои 5 видео одобрены.\n"
            "Ты получил 1 бесплатную попытку просмотра!",
            reply_markup=bottom_keyboard(),
        )
    else:
        await context.bot.send_message(
            user_id,
            f"✅ Твоё видео #{submission_id} одобрено.\n"
            f"🎬 {title}",
            reply_markup=bottom_keyboard(),
        )


# =========================================================
# FREE ACCESS
# =========================================================

async def show_free(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    ensure_user(user_id)

    connection = get_db()
    user = connection.execute(
        """
        SELECT approved_count, free_attempts
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    prize = connection.execute(
        """
        SELECT id, name
        FROM content
        WHERE is_free_prize = 1
          AND active = 1
        LIMIT 1
        """
    ).fetchone()
    connection.close()

    text = (
        "🎁 Бесплатный доступ\n\n"
        f"📊 До следующей бесплатной попытки: {user['approved_count']}/5 одобренных видео\n"
        f"🎁 Бесплатных просмотров доступно: {user['free_attempts']}\n\n"
    )

    keyboard = []

    if prize:
        if user["free_attempts"] > 0:
            text += f"🎬 Твой бесплатный материал: {prize['name']}"
            keyboard.append([
                InlineKeyboardButton(
                    "🎁 Получить бесплатно",
                    callback_data=f"claim:{prize['id']}",
                )
            ])
        else:
            text += f"🎬 Материал: {prize['name']}\n\nУ тебя пока нет бесплатных попыток."
    else:
        text += "Бесплатный материал пока не настроен."

    keyboard.append([
        InlineKeyboardButton("📢 Просмотр за пиар", callback_data="promo")
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="home")
    ])

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def promo_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "📢 Просмотр за пиар\n\n"
        "Чтобы получить просмотр, вступи в канал/сообщество по ссылке ниже.\n\n"
        "После этого вернись в бота.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📢 Перейти за пиар",
                    url="https://t.me/+LeuzcxjiFOM1ZDY0",
                )
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="free")],
        ]),
    )


async def claim_free(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    prize_id = int(query.data.split(":")[1])

    connection = get_db()

    cursor = connection.execute(
        """
        UPDATE users
        SET free_attempts = free_attempts - 1
        WHERE user_id = ?
          AND free_attempts > 0
        """,
        (user_id,),
    )

    if cursor.rowcount != 1:
        connection.close()
        await query.message.reply_text(
            "У тебя нет бесплатных попыток.",
            reply_markup=bottom_keyboard(),
        )
        return

    prize = connection.execute(
        """
        SELECT name, file_id, media_type
        FROM content
        WHERE id = ?
          AND is_free_prize = 1
          AND active = 1
        """,
        (prize_id,),
    ).fetchone()

    if not prize:
        connection.execute(
            """
            UPDATE users
            SET free_attempts = free_attempts + 1
            WHERE user_id = ?
            """,
            (user_id,),
        )
        connection.commit()
        connection.close()

        await query.message.reply_text(
            "Бесплатный материал сейчас недоступен.",
            reply_markup=bottom_keyboard(),
        )
        return

    connection.commit()
    connection.close()

    try:
        await send_catalog_media(
            context.bot, user_id, prize["media_type"], prize["file_id"],
            caption=f"🎁 Бесплатный просмотр: {prize['name']}",
        )
    except TelegramError:
        connection = get_db()
        connection.execute(
            "UPDATE users SET free_attempts = free_attempts + 1 WHERE user_id = ?",
            (user_id,),
        )
        connection.commit()
        connection.close()
        await query.message.reply_text(
            "Не удалось отправить материал. Попытка возвращена.",
            reply_markup=bottom_keyboard(),
        )
        return

    connection = get_db()
    connection.execute(
        "UPDATE users SET viewed_count = viewed_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    connection.commit()
    connection.close()


# =========================================================
# PROFILE
# =========================================================

async def show_profile_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    connection = get_db()
    user = connection.execute(
        """
        SELECT submitted_count,
               approved_count,
               approved_total,
               viewed_count,
               search_count,
               free_attempts
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    connection.close()

    await update.message.reply_text(
        "👤 Твой профиль\n\n"
        f"📤 Отправлено видео: {user['submitted_count']}\n"
        f"✅ Одобрено всего: {user['approved_total']}\n"
        f"🎯 До следующей награды: {user['approved_count']} / 5\n"
        f"👀 Просмотрено материалов: {user['viewed_count']}\n"
        f"🔎 Поисков: {user['search_count']}\n"
        f"🎁 Бесплатных просмотров: {user['free_attempts']}",
        reply_markup=bottom_keyboard(),
    )


# =========================================================
# SEARCH
# =========================================================

async def start_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_search"] = True
    await update.message.reply_text(
        "🔎 Поиск по каталогу\n\n"
        "Напиши название или часть названия материала.",
        reply_markup=bottom_keyboard(),
    )


async def search_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_search"):
        return

    query_text = (update.message.text or "").strip()
    if not query_text:
        return

    context.user_data.pop("waiting_search", None)
    user_id = update.effective_user.id
    ensure_user(user_id)

    connection = get_db()
    results = connection.execute(
        """
        SELECT id, name, price
        FROM content
        WHERE active = 1
          AND name LIKE ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (f"%{query_text}%",),
    ).fetchall()

    connection.execute(
        "UPDATE users SET search_count = search_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    connection.commit()
    connection.close()

    if not results:
        await update.message.reply_text(
            f"Ничего не найдено по запросу: {query_text}",
            reply_markup=bottom_keyboard(),
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"{row['name']} — {row['price']} ⭐",
                callback_data=f"content:{row['id']}",
            )
        ]
        for row in results
    ]
    keyboard.append([
        InlineKeyboardButton("⬅️ Главное меню", callback_data="home")
    ])

    await update.message.reply_text(
        f"🔎 Результаты поиска: {query_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# ADMIN — ADD VIDEO
# =========================================================

async def add_video_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    await update.message.reply_text("📹 Отправь видео или 🖼 фото.")
    return ADD_VIDEO


async def add_video_receive(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message.video:
        context.user_data["file_id"] = update.message.video.file_id
        context.user_data["media_type"] = "video"
        media_label = "видео"
    elif update.message.photo:
        context.user_data["file_id"] = update.message.photo[-1].file_id
        context.user_data["media_type"] = "photo"
        media_label = "фото"
    else:
        await update.message.reply_text("Нужно отправить видео или фото.")
        return ADD_VIDEO

    context.user_data["media_label"] = media_label
    await update.message.reply_text(f"✏️ Напиши название {media_label}.")
    return ADD_NAME


async def add_video_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("Название не может быть пустым.")
        return ADD_NAME

    context.user_data["name"] = name

    await update.message.reply_text(
        "💰 Напиши цену:\n\n"
        "15\n"
        "25\n"
        "50"
    )
    return ADD_PRICE


async def add_video_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        price = int(update.message.text.strip())
    except ValueError:
        price = 0

    if price not in (15, 25, 50):
        await update.message.reply_text("Цена должна быть 15, 25 или 50.")
        return ADD_PRICE

    context.user_data["price"] = price

    keyboard = [[
        InlineKeyboardButton("🎁 Да, сделать призом", callback_data="free_yes"),
        InlineKeyboardButton("❌ Нет", callback_data="free_no"),
    ]]

    await update.message.reply_text(
        "Сделать это видео бесплатным призом?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_FREE


async def finish_add_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return ADD_FREE

    await query.answer()

    is_free = 1 if query.data == "free_yes" else 0

    name = context.user_data["name"]
    price = context.user_data["price"]
    file_id = context.user_data["file_id"]
    media_type = context.user_data.get("media_type", "video")

    connection = get_db()

    if is_free:
        connection.execute(
            """
            UPDATE content
            SET is_free_prize = 0
            WHERE is_free_prize = 1
            """
        )

    cursor = connection.execute(
        """
        INSERT INTO content(name, price, file_id, media_type, is_free_prize)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, price, file_id, media_type, is_free),
    )
    content_id = cursor.lastrowid

    connection.commit()
    connection.close()

    context.user_data.clear()

    await query.edit_message_text(
        f"✅ Видео добавлено!\n\n"
        f"ID: #{content_id}\n"
        f"Название: {name}\n"
        f"Цена: {price} ⭐\n"
        f"Бесплатный приз: {'Да' if is_free else 'Нет'}"
    )

    return ConversationHandler.END


# =========================================================
# ADMIN — BULK ADD
# =========================================================

async def bulk_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    admin_id = update.effective_user.id
    connection = get_db()
    existing = connection.execute(
        """
        SELECT b.batch_id, COUNT(q.id) AS total
        FROM bulk_batches b
        JOIN bulk_queue q ON q.batch_id = b.batch_id
        WHERE b.admin_id = ? AND q.status = 'queued'
        GROUP BY b.batch_id
        ORDER BY b.created_at DESC
        LIMIT 1
        """,
        (admin_id,),
    ).fetchone()
    connection.close()

    if existing:
        await update.message.reply_text(
            f"📦 У тебя уже есть незавершённая загрузка: {existing['total']} видео.\n\n"
            "Используй /bulk_resume, чтобы продолжить её.\n"
            "Или /bulk_cancel, если хочешь удалить эту очередь."
        )
        return ConversationHandler.END

    batch_id = uuid.uuid4().hex
    connection = get_db()
    connection.execute(
        "INSERT INTO bulk_batches(batch_id, admin_id, status) VALUES (?, ?, 'uploading')",
        (batch_id, admin_id),
    )
    connection.commit()
    connection.close()

    context.user_data["bulk_batch_id"] = batch_id

    await update.message.reply_text(
        "📦 Массовая загрузка включена.\n\n"
        "Отправляй сюда сколько угодно видео или фото — хоть 100 за раз.\n"
        "Я буду складывать их в очередь.\n\n"
        "Когда закончишь отправку, напиши /bulk_done.\n"
        "После этого бот начнёт спрашивать название и цену для каждого материала.\n\n"
        "Можно отправлять видео/фото альбомами или по одному."
    )
    return BULK_UPLOAD


async def bulk_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return BULK_UPLOAD

    batch_id = context.user_data.get("bulk_batch_id")
    if not batch_id or (not update.message.video and not update.message.photo):
        return BULK_UPLOAD

    if update.message.video:
        file_id = update.message.video.file_id
        media_type = "video"
        media_label = "видео"
    else:
        file_id = update.message.photo[-1].file_id
        media_type = "photo"
        media_label = "фото"

    connection = get_db()
    batch = connection.execute(
        "SELECT batch_id FROM bulk_batches WHERE batch_id = ? AND admin_id = ? AND status = 'uploading'",
        (batch_id, update.effective_user.id),
    ).fetchone()

    if not batch:
        connection.close()
        await update.message.reply_text("Очередь загрузки не найдена. Запусти /bulk_add заново.")
        return ConversationHandler.END

    connection.execute(
        """
        INSERT INTO bulk_queue(batch_id, admin_id, file_id, media_type)
        VALUES (?, ?, ?, ?)
        """,
        (batch_id, update.effective_user.id, file_id, media_type),
    )
    count = connection.execute(
        "SELECT COUNT(*) AS total FROM bulk_queue WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()["total"]
    connection.commit()
    connection.close()

    # Не отвечаем на каждое видео, чтобы при загрузке 100 файлов чат не заспамился.
    if count in (1, 10, 25, 50, 75, 100) or count % 100 == 0:
        await update.message.reply_text(
            f"📥 Получено материалов: {count}\n\n"
            "Продолжай загрузку. Когда закончишь — /bulk_done"
        )

    return BULK_UPLOAD


async def bulk_finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    batch_id = context.user_data.get("bulk_batch_id")
    if not batch_id:
        await update.message.reply_text("Активная массовая загрузка не найдена.")
        return ConversationHandler.END

    connection = get_db()
    count = connection.execute(
        "SELECT COUNT(*) AS total FROM bulk_queue WHERE batch_id = ? AND status = 'queued'",
        (batch_id,),
    ).fetchone()["total"]

    if count == 0:
        connection.close()
        await update.message.reply_text("Ты ещё не отправил ни одного видео.")
        return BULK_UPLOAD

    connection.execute(
        "UPDATE bulk_batches SET status = 'ready' WHERE batch_id = ?",
        (batch_id,),
    )
    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"✅ Загрузка закончена. В очереди {count} видео.\n\n"
        "Теперь будем по очереди задавать название и цену."
    )
    return await bulk_ask_name(update, context)


async def bulk_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    admin_id = update.effective_user.id
    connection = get_db()
    batch = connection.execute(
        """
        SELECT b.batch_id, b.status, COUNT(q.id) AS total
        FROM bulk_batches b
        JOIN bulk_queue q ON q.batch_id = b.batch_id
        WHERE b.admin_id = ? AND q.status = 'queued'
        GROUP BY b.batch_id
        ORDER BY b.created_at DESC
        LIMIT 1
        """,
        (admin_id,),
    ).fetchone()
    connection.close()

    if not batch:
        await update.message.reply_text("Незавершённых массовых загрузок нет. Используй /bulk_add.")
        return ConversationHandler.END

    context.user_data["bulk_batch_id"] = batch["batch_id"]

    if batch["status"] == "uploading":
        await update.message.reply_text(
            f"📦 В очереди уже {batch['total']} видео.\n\n"
            "Можешь продолжить отправлять видео или фото.\n"
            "Когда закончишь — /bulk_done"
        )
        return BULK_UPLOAD

    await update.message.reply_text(
        f"▶️ Продолжаем обработку. Осталось видео: {batch['total']}"
    )
    return await bulk_ask_name(update, context)


async def bulk_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_id = context.user_data.get("bulk_batch_id")
    if not batch_id:
        return ConversationHandler.END

    connection = get_db()
    row = connection.execute(
        """
        SELECT id, file_id, media_type,
               (SELECT COUNT(*) FROM bulk_queue WHERE batch_id = ? AND status = 'queued') AS remaining
        FROM bulk_queue
        WHERE batch_id = ? AND status = 'queued'
        ORDER BY id
        LIMIT 1
        """,
        (batch_id, batch_id),
    ).fetchone()
    connection.close()

    if not row:
        connection = get_db()
        connection.execute("UPDATE bulk_batches SET status = 'done' WHERE batch_id = ?", (batch_id,))
        connection.commit()
        connection.close()
        context.user_data.pop("bulk_batch_id", None)
        await update.message.reply_text("🎉 Всё готово! Все видео добавлены в каталог.")
        return ConversationHandler.END

    # Показываем текущее видео админу, чтобы было понятно, для какого файла вводится название.
    try:
        await send_catalog_media(
            context.bot, update.effective_user.id, row["media_type"], row["file_id"],
            caption=f"📦 Материал в очереди\nОсталось: {row['remaining']}\n\n✏️ Напиши название этого материала."
        )
    except TelegramError:
        logger.exception("Не удалось показать видео из bulk-очереди %s", row["id"])
        await update.message.reply_text(
            f"✏️ Напиши название видео #{row['id']}. Осталось: {row['remaining']}"
        )

    context.user_data["bulk_item_id"] = row["id"]
    return BULK_NAME


async def bulk_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return BULK_NAME

    item_id = context.user_data.get("bulk_item_id")
    if not item_id:
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым.")
        return BULK_NAME

    name = name[:200]
    connection = get_db()
    item = connection.execute(
        "SELECT id FROM bulk_queue WHERE id = ? AND admin_id = ? AND status = 'queued'",
        (item_id, update.effective_user.id),
    ).fetchone()
    connection.close()

    if not item:
        await update.message.reply_text("Видео из очереди не найдено. Используй /bulk_resume.")
        return ConversationHandler.END

    connection = get_db()
    connection.execute("UPDATE bulk_queue SET name = ? WHERE id = ?", (name, item_id))
    connection.commit()
    connection.close()

    keyboard = [[
        InlineKeyboardButton("🎁 15 ⭐", callback_data=f"bulk_price:{item_id}:15"),
        InlineKeyboardButton("🎁 25 ⭐", callback_data=f"bulk_price:{item_id}:25"),
        InlineKeyboardButton("🎁 50 ⭐", callback_data=f"bulk_price:{item_id}:50"),
    ]]

    await update.message.reply_text(
        f"🎬 {name}\n\nВыбери ценовую категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BULK_PRICE


async def bulk_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return BULK_PRICE

    await query.answer()

    _, item_id_text, price_text = query.data.split(":")
    item_id = int(item_id_text)
    price = int(price_text)

    connection = get_db()
    item = connection.execute(
        """
        SELECT id, batch_id, name, file_id, media_type
        FROM bulk_queue
        WHERE id = ? AND admin_id = ? AND status = 'queued'
        """,
        (item_id, query.from_user.id),
    ).fetchone()

    if not item or not item["name"]:
        connection.close()
        await query.edit_message_text("Не удалось найти это видео в очереди. Используй /bulk_resume.")
        return ConversationHandler.END

    cursor = connection.execute(
        """
        INSERT INTO content(name, price, file_id, media_type, is_free_prize)
        VALUES (?, ?, ?, ?, 0)
        """,
        (item["name"], price, item["file_id"], item["media_type"]),
    )
    content_id = cursor.lastrowid

    connection.execute(
        """
        UPDATE bulk_queue
        SET price = ?, status = 'done'
        WHERE id = ?
        """,
        (price, item_id),
    )
    connection.commit()

    remaining = connection.execute(
        "SELECT COUNT(*) AS total FROM bulk_queue WHERE batch_id = ? AND status = 'queued'",
        (item["batch_id"],),
    ).fetchone()["total"]

    if remaining == 0:
        connection.execute(
            "UPDATE bulk_batches SET status = 'done' WHERE batch_id = ?",
            (item["batch_id"],),
        )
        connection.commit()

    connection.close()

    await query.edit_message_text(
        f"✅ Добавлено в каталог: #{content_id}\n"
        f"🎬 {item['name']}\n"
        f"⭐ {price}\n\n"
        f"Осталось: {remaining}"
    )

    if remaining == 0:
        context.user_data.pop("bulk_batch_id", None)
        context.user_data.pop("bulk_item_id", None)
        await query.message.reply_text("🎉 Готово! Все видео из этой массовой загрузки добавлены.")
        return ConversationHandler.END

    context.user_data["bulk_item_id"] = None
    # CallbackQuery не является Message, поэтому передаём объект update дальше
    # и функция сама отправит следующее видео через context.bot.
    return await bulk_ask_next_from_callback(query, context, item["batch_id"])


async def bulk_ask_next_from_callback(query, context, batch_id):
    connection = get_db()
    row = connection.execute(
        """
        SELECT id, file_id, media_type,
               (SELECT COUNT(*) FROM bulk_queue WHERE batch_id = ? AND status = 'queued') AS remaining
        FROM bulk_queue
        WHERE batch_id = ? AND status = 'queued'
        ORDER BY id
        LIMIT 1
        """,
        (batch_id, batch_id),
    ).fetchone()
    connection.close()

    if not row:
        return ConversationHandler.END

    context.user_data["bulk_item_id"] = row["id"]
    try:
        await send_catalog_media(
            context.bot, query.from_user.id, row["media_type"], row["file_id"],
            caption=f"📦 Следующий материал\nОсталось: {row['remaining']}\n\n✏️ Напиши название этого материала."
        )
    except TelegramError:
        logger.exception("Не удалось показать следующее bulk-видео %s", row["id"])
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"✏️ Напиши название видео #{row['id']}. Осталось: {row['remaining']}"
        )
    return BULK_NAME


async def bulk_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    batch_id = context.user_data.get("bulk_batch_id")

    if not batch_id:
        connection = get_db()
        row = connection.execute(
            """
            SELECT b.batch_id
            FROM bulk_batches b
            JOIN bulk_queue q ON q.batch_id = b.batch_id
            WHERE b.admin_id = ? AND q.status = 'queued'
            ORDER BY b.created_at DESC
            LIMIT 1
            """,
            (update.effective_user.id,),
        ).fetchone()
        connection.close()
        batch_id = row["batch_id"] if row else None

    if batch_id:
        connection = get_db()
        connection.execute("DELETE FROM bulk_queue WHERE batch_id = ?", (batch_id,))
        connection.execute("DELETE FROM bulk_batches WHERE batch_id = ?", (batch_id,))
        connection.commit()
        connection.close()

    context.user_data.pop("bulk_batch_id", None)
    context.user_data.pop("bulk_item_id", None)
    await update.message.reply_text("❌ Массовая загрузка отменена. Очередь очищена.")
    return ConversationHandler.END


# =========================================================
# ADMIN — CATALOG
# =========================================================

async def admin_videos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id not in ADMIN_IDS:
        return

    connection = get_db()
    videos = connection.execute(
        """
        SELECT id, name, price, is_free_prize, active
        FROM content
        ORDER BY id DESC
        """
    ).fetchall()
    connection.close()

    if not videos:
        await update.message.reply_text("Каталог пока пуст.")
        return

    lines = ["📚 Каталог:\n"]
    for video in videos:
        status = "🟢" if video["active"] else "🔴"
        prize = " 🎁 ПРИЗ" if video["is_free_prize"] else ""
        lines.append(
            f"{status} ID: {video['id']} — {video['name']} — "
            f"🎁 {video['price']} ⭐{prize}"
        )

    await update.message.reply_text("\n".join(lines))


# =========================================================
# ADMIN — FIND VIDEO BY NAME
# =========================================================

async def find_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    query_text = " ".join(context.args).strip()
    if not query_text:
        await update.message.reply_text(
            "Использование:\n/find_video название\n\nНапример:\n/find_video funny cats"
        )
        return

    connection = get_db()
    videos = connection.execute(
        "SELECT id, name, price, active, is_free_prize FROM content WHERE name LIKE ? ORDER BY id DESC LIMIT 30",
        (f"%{query_text}%",),
    ).fetchall()
    connection.close()

    if not videos:
        await update.message.reply_text(f"🔎 Ничего не найдено по запросу: {query_text}")
        return

    lines = [f"🔎 Найдено по запросу «{query_text}»:\n"]
    for video in videos:
        status = "🟢 активно" if video["active"] else "🔴 скрыто"
        prize = " 🎁 ПРИЗ" if video["is_free_prize"] else ""
        lines.append(
            f"🆔 ID: {video['id']}\n"
            f"🎬 {video['name']}\n"
            f"🎁 {video['price']} ⭐ — {status}{prize}\n"
        )

    await update.message.reply_text("\n".join(lines))


# =========================================================
# ADMIN — DELETE
# =========================================================

async def delete_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/delete_video ID\n\n"
            "Например:\n"
            "/delete_video 3"
        )
        return

    try:
        content_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    connection = get_db()
    cursor = connection.execute(
        """
        UPDATE content
        SET active = 0,
            is_free_prize = 0
        WHERE id = ?
        """,
        (content_id,),
    )
    connection.commit()
    connection.close()

    if cursor.rowcount == 0:
        await update.message.reply_text("Видео с таким ID не найдено.")
        return

    await update.message.reply_text(
        f"✅ Видео #{content_id} убрано из каталога."
    )


# =========================================================
# ADMIN — GIVE FREE ATTEMPTS
# =========================================================

async def give_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args or len(context.args) > 2:
        await update.message.reply_text(
            "Использование:\n"
            "/give_free USER_ID [КОЛИЧЕСТВО]\n\n"
            "Примеры:\n"
            "/give_free 123456789\n"
            "/give_free 123456789 3"
        )
        return

    try:
        user_id = int(context.args[0])
        amount = int(context.args[1]) if len(context.args) == 2 else 1
    except ValueError:
        await update.message.reply_text("USER_ID и количество должны быть числами.")
        return

    if amount < 1 or amount > 100:
        await update.message.reply_text("Количество попыток должно быть от 1 до 100.")
        return

    ensure_user(user_id)
    connection = get_db()
    connection.execute(
        "UPDATE users SET free_attempts = free_attempts + ? WHERE user_id = ?",
        (amount, user_id),
    )
    row = connection.execute(
        "SELECT free_attempts FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    connection.commit()
    connection.close()

    total = row["free_attempts"]
    await update.message.reply_text(
        "✅ Бесплатные попытки выданы.\n\n"
        f"👤 User ID: {user_id}\n"
        f"🎁 Выдано: {amount}\n"
        f"🎟 Теперь доступно: {total}"
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎁 Тебе выдана бесплатная попытка!\n\n"
                f"Добавлено попыток: {amount}\n"
                f"Всего доступно: {total}\n\n"
                "Открой раздел «Бесплатная попытка», чтобы получить бесплатный материал."
            ),
        )
    except TelegramError:
        logger.exception("Не удалось уведомить пользователя %s о попытках", user_id)


# =========================================================
# ADMIN — PANEL / STATS / USERS / SUBMISSIONS / PAYMENTS
# =========================================================

def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Каталог", callback_data="admin:videos"),
         InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton("📥 Заявки", callback_data="admin:submissions")],
        [InlineKeyboardButton("💰 Оплаты", callback_data="admin:payments"),
         InlineKeyboardButton("🎁 Подарки", callback_data="admin:gifts")],
        [InlineKeyboardButton("⬅️ Закрыть", callback_data="home")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "👑 <b>Панель администратора</b>\n\n"
        "Основные команды доступны также напрямую:\n"
        "/videos — каталог и ID\n"
        "/find_video название — поиск\n"
        "/add_video — добавить материал\n"
        "/delete_video ID — скрыть материал\n"
        "/give_free USER_ID [КОЛИЧЕСТВО] — выдать попытки\n"
        "/bulk_add — массовая загрузка\n"
        "/bulk_done — закончить загрузку\n"
        "/bulk_resume — продолжить очередь\n"
        "/bulk_cancel — отменить очередь\n"
        "/backup_db — резервная копия базы\n"
        "/gift_status — статус Business-подарков",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db=get_db()
    users=db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    active=db.execute("SELECT COUNT(*) c FROM content WHERE active=1").fetchone()["c"]
    total=db.execute("SELECT COUNT(*) c FROM content").fetchone()["c"]
    submissions=db.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
    pending=db.execute("SELECT COUNT(*) c FROM submissions WHERE status='pending'").fetchone()["c"]
    donations=db.execute("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM donations").fetchone()
    gifts=db.execute("SELECT COUNT(*) c, COALESCE(SUM(gift_star_count),0) s FROM gift_orders WHERE status='completed'").fetchone()
    db.close()
    await update.message.reply_text(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"📚 Материалов всего: {total}\n"
        f"🟢 Активных: {active}\n"
        f"📥 Заявок: {submissions}\n"
        f"⏳ На модерации: {pending}\n"
        f"⭐ Донатов: {donations['c']} на {donations['s']} Stars\n"
        f"🎁 Оплаченных подарков: {gifts['c']} на {gifts['s']} Stars",
        parse_mode="HTML",
    )


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db=get_db()
    rows=db.execute("SELECT user_id, free_attempts, approved_count, viewed_count, search_count FROM users ORDER BY user_id DESC LIMIT 50").fetchall()
    db.close()
    if not rows:
        await update.message.reply_text("Пользователей пока нет.")
        return
    lines=["👥 Последние пользователи:",""]
    for r in rows:
        lines.append(f"ID {r['user_id']} — 🎟 {r['free_attempts']} | 👁 {r['viewed_count']} | 🔎 {r['search_count']} | ✅ {r['approved_count']}")
    await update.message.reply_text("\n".join(lines))


async def admin_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db=get_db()
    rows=db.execute("SELECT id, user_id, title, status FROM submissions ORDER BY id DESC LIMIT 50").fetchall()
    db.close()
    if not rows:
        await update.message.reply_text("Заявок пока нет.")
        return
    lines=["📥 Последние заявки:",""]
    for r in rows:
        title=r['title'] or 'без названия'
        lines.append(f"#{r['id']} — {r['status']} — User {r['user_id']} — {title}")
    await update.message.reply_text("\n".join(lines))


async def admin_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db=get_db()
    rows=db.execute("SELECT charge_id, user_id, content_id, amount FROM payments ORDER BY rowid DESC LIMIT 50").fetchall()
    gifts=db.execute("SELECT order_id, user_id, content_id, expected_price, status FROM gift_orders ORDER BY order_id DESC LIMIT 50").fetchall()
    db.close()
    lines=["💰 Последние оплаты:",""]
    if rows:
        lines += [f"⭐ Stars: {r['charge_id']} — User {r['user_id']} — материал #{r['content_id']} — {r['amount']} ⭐" for r in rows]
    else:
        lines.append("Stars-оплат пока нет.")
    if gifts:
        lines += [f"🎁 Gift #{r['order_id']} — User {r['user_id']} — материал #{r['content_id']} — {r['expected_price']} ⭐ — {r['status']}" for r in gifts]
    await update.message.reply_text("\n".join(lines[:102]))


async def admin_donations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db=get_db()
    rows=db.execute("SELECT charge_id, user_id, amount FROM donations ORDER BY rowid DESC LIMIT 50").fetchall()
    db.close()
    if not rows:
        await update.message.reply_text("Донатов пока нет.")
        return
    await update.message.reply_text("💙 Последние донаты:\n\n" + "\n".join(f"{r['amount']} ⭐ — User {r['user_id']} — {r['charge_id']}" for r in rows))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    key=query.data.split(":",1)[1]
    # Reuse the command functions with a lightweight fake-free response where possible.
    if key == "videos":
        db=get_db(); rows=db.execute("SELECT id,name,price,active,is_free_prize,media_type FROM content ORDER BY id DESC LIMIT 100").fetchall(); db.close()
        text="📚 Каталог:\n\n" + ("\n".join(f"{'🟢' if r['active'] else '🔴'} #{r['id']} — {r['name']} — {r['price']} ⭐ — {'🖼' if r['media_type']=='photo' else '🎬'}" for r in rows) if rows else "Каталог пуст.")
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
    elif key == "stats":
        db=get_db(); u=db.execute("SELECT COUNT(*) c FROM users").fetchone()['c']; a=db.execute("SELECT COUNT(*) c FROM content WHERE active=1").fetchone()['c']; p=db.execute("SELECT COUNT(*) c FROM submissions WHERE status='pending'").fetchone()['c']; d=db.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()['s']; g=db.execute("SELECT COALESCE(SUM(gift_star_count),0) s FROM gift_orders WHERE status='completed'").fetchone()['s']; db.close()
        await query.edit_message_text(f"📊 Статистика\n\n👥 Пользователи: {u}\n🟢 Активные материалы: {a}\n⏳ Заявки: {p}\n⭐ Донаты: {d}\n🎁 Подарки: {g}", reply_markup=admin_panel_keyboard())
    elif key == "users":
        db=get_db(); rows=db.execute("SELECT user_id,free_attempts FROM users ORDER BY user_id DESC LIMIT 30").fetchall(); db.close()
        await query.edit_message_text("👥 Пользователи:\n\n" + ("\n".join(f"{r['user_id']} — 🎟 {r['free_attempts']}" for r in rows) if rows else "Пусто."), reply_markup=admin_panel_keyboard())
    elif key == "submissions":
        db=get_db(); rows=db.execute("SELECT id,user_id,title,status FROM submissions ORDER BY id DESC LIMIT 30").fetchall(); db.close()
        await query.edit_message_text("📥 Заявки:\n\n" + ("\n".join(f"#{r['id']} — {r['status']} — {r['user_id']} — {r['title'] or 'без названия'}" for r in rows) if rows else "Пусто."), reply_markup=admin_panel_keyboard())
    elif key == "payments":
        db=get_db(); rows=db.execute("SELECT order_id,user_id,content_id,expected_price,status FROM gift_orders ORDER BY order_id DESC LIMIT 30").fetchall(); db.close()
        await query.edit_message_text("💰 Подарочные оплаты:\n\n" + ("\n".join(f"#{r['order_id']} — User {r['user_id']} — #{r['content_id']} — {r['expected_price']} ⭐ — {r['status']}" for r in rows) if rows else "Пусто."), reply_markup=admin_panel_keyboard())
    elif key == "gifts":
        await query.edit_message_text("🎁 Для полной проверки Business-подарков используй /gift_status.", reply_markup=admin_panel_keyboard())


# =========================================================
# HOME / BOTTOM BUTTONS
# =========================================================

async def home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Главное меню:",
        reply_markup=main_menu(),
    )


async def menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=main_menu(),
    )


async def profile_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_profile_message(update, context)



# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=bottom_keyboard(),
    )
    return ConversationHandler.END


# =========================================================
# MAIN
# =========================================================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует в .env")

    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS отсутствует в .env")

    init_db()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    bulk_add_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("bulk_add", bulk_add_start),
            CommandHandler("bulk_resume", bulk_resume),
        ],
        states={
            BULK_UPLOAD: [
                MessageHandler(filters.VIDEO | filters.PHOTO, bulk_receive_video),
                CommandHandler("bulk_done", bulk_finish_upload),
            ],
            BULK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_receive_name),
            ],
            BULK_PRICE: [
                CallbackQueryHandler(bulk_set_price, pattern=r"^bulk_price:\d+:(15|25|50)$"),
            ],
        },
        fallbacks=[
            CommandHandler("bulk_done", bulk_finish_upload),
            CommandHandler("bulk_cancel", bulk_cancel),
        ],
        allow_reentry=True,
    )

    add_video_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add_video", add_video_start)
        ],
        states={
            ADD_VIDEO: [
                MessageHandler(filters.VIDEO | filters.PHOTO, add_video_receive)
            ],
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_video_name)
            ],
            ADD_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_video_price)
            ],
            ADD_FREE: [
                CallbackQueryHandler(
                    finish_add_video,
                    pattern=r"^free_(yes|no)$",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        try:
            with open(DB, "rb") as db_file:
                await update.message.reply_document(
                    document=db_file,
                    caption=(
                        "🗄 Резервная копия базы бота.\n"
                        "В ней хранятся каталог, цены, заказы и настройки.\n"
                        "Telegram-файлы сам бот хранит у Telegram по file_id."
                    ),
                )
        except (OSError, TelegramError):
            logger.exception("Не удалось сделать резервную копию базы")
            await update.message.reply_text("❌ Не удалось создать резервную копию базы.")


# Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("terms", terms))
    application.add_handler(CommandHandler("paysupport", pay_support))
    application.add_handler(CommandHandler("donate", donate_command))
    application.add_handler(PreCheckoutQueryHandler(donation_pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, donation_successful_payment))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler("users", admin_users))
    application.add_handler(CommandHandler("submissions", admin_submissions))
    application.add_handler(CommandHandler("payments", admin_payments))
    application.add_handler(CommandHandler("donations", admin_donations))
    application.add_handler(CommandHandler("videos", admin_videos))
    application.add_handler(CommandHandler("backup_db", backup_db))
    application.add_handler(CommandHandler("delete_video", delete_video))
    application.add_handler(CommandHandler("find_video", find_video))
    application.add_handler(CommandHandler("give_free", give_free))
    application.add_handler(bulk_add_conversation)
    application.add_handler(add_video_conversation)
    application.add_handler(CommandHandler("bulk_cancel", bulk_cancel))

    # Inline buttons
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:(videos|stats|users|submissions|payments|gifts)$"))
    application.add_handler(CallbackQueryHandler(show_catalog, pattern=r"^catalog$"))
    application.add_handler(
        CallbackQueryHandler(donate_menu, pattern=r"^donate$")
    )
    application.add_handler(
        CallbackQueryHandler(donate, pattern=r"^donate:(15|25|50|100|250)$")
    )
    application.add_handler(
        CallbackQueryHandler(show_price, pattern=r"^price:(15|25|50)$")
    )
    application.add_handler(
        CallbackQueryHandler(show_content, pattern=r"^content:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(buy_content, pattern=r"^buy:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(start_submission, pattern=r"^submit$")
    )
    application.add_handler(
        CallbackQueryHandler(show_free, pattern=r"^free$")
    )
    application.add_handler(
        CallbackQueryHandler(promo_access, pattern=r"^promo$")
    )
    application.add_handler(
        CallbackQueryHandler(claim_free, pattern=r"^claim:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(home, pattern=r"^home$")
    )
    application.add_handler(
        CallbackQueryHandler(moderate, pattern=r"^(approve|reject):\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            start_submission_naming,
            pattern=r"^name_submission:\d+$",
        )
    )

    # Telegram Business / Gifts
    application.add_handler(BusinessConnectionHandler(handle_business_connection))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.GIFT, business_gift_received)
    )
    application.add_handler(CommandHandler("gift_status", gift_status))

    # Bottom keyboard
    application.add_handler(
        MessageHandler(filters.Regex(r"^☰ Меню$"), menu_message)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🔎 Поиск$"), start_search_message)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^👤 Профиль$"), profile_message)
    )

    # User search / submission title
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_admin_submission_title,
        ),
        group=2,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            search_content,
        ),
        group=3,
    )

    # User videos
    application.add_handler(
        MessageHandler(filters.VIDEO, receive_submission)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
