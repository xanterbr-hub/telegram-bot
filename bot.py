import os
import sqlite3
import asyncio
import uuid
import logging
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

DB = "bot.db"

ADD_VIDEO, ADD_NAME, ADD_PRICE, ADD_FREE = range(4)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PROMO_LINK = "https://t.me/+LeuzcxjiFOM1ZDY0"


# =========================================================
# DATABASE
# =========================================================

def get_db():
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def add_column_if_missing(connection, table, column, definition):
    columns = {
        row["name"]
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
            free_attempts INTEGER NOT NULL DEFAULT 0,
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
            is_free_prize INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            amount INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promo_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            batch_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
    """)

    # Миграции для уже существующего bot.db.
    add_column_if_missing(connection, "users", "viewed_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "search_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "donation_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "donated_stars", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "users", "promo_approved_photos", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "submissions", "title", "TEXT")
    add_column_if_missing(connection, "submissions", "reviewed_at", "TEXT")
    add_column_if_missing(connection, "submissions", "reviewed_by", "INTEGER")
    add_column_if_missing(connection, "promo_submissions", "batch_id", "TEXT")

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


# =========================================================
# COMMON UI
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 15 звёзд", callback_data="price:15"),
            InlineKeyboardButton("⭐ 25 звёзд", callback_data="price:25"),
        ],
        [
            InlineKeyboardButton("⭐ 50 звёзд", callback_data="price:50"),
        ],
        [
            InlineKeyboardButton("📤 Отправить видео", callback_data="submit"),
        ],
        [
            InlineKeyboardButton("🎁 Бесплатный просмотр", callback_data="free"),
        ],
        [
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            InlineKeyboardButton("🔎 Поиск", callback_data="search"),
        ],
        [
            InlineKeyboardButton("⭐ Поддержать проект", callback_data="donate"),
        ],
    ])


def back_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
    ])


def user_display(user):
    if not user:
        return "неизвестный пользователь"
    username = f"@{user.username}" if user.username else "без username"
    name = user.full_name or "Без имени"
    return f"{name} ({username})"


# =========================================================
# START / HELP
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)

    await update.message.reply_text(
        "🎬 Добро пожаловать!\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — главное меню\n"
        "/terms — условия\n"
        "/paysupport — помощь с оплатой\n"
        "/cancel — отменить действие\n\n"
        "Админские команды доступны только администратору."
    )


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 Условия использования\n\n"
        "Покупая цифровой контент через бота, пользователь "
        "подтверждает ознакомление с условиями покупки.\n\n"
        "Контент предоставляется в цифровом виде после "
        "подтверждения оплаты."
    )


async def pay_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 Поддержка по оплате\n\n"
        "Если Stars были списаны, но контент не пришёл, "
        "обратись к администратору."
    )


# =========================================================
# PROFILE
# =========================================================

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    ensure_user(user_id)

    connection = get_db()

    user = connection.execute(
        """
        SELECT approved_count, free_attempts, viewed_count,
               search_count, donation_count, donated_stars
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    submitted = connection.execute(
        "SELECT COUNT(*) AS count FROM submissions WHERE user_id = ?",
        (user_id,),
    ).fetchone()["count"]

    pending = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM submissions
        WHERE user_id = ? AND status = 'pending'
        """,
        (user_id,),
    ).fetchone()["count"]

    approved_total = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM submissions
        WHERE user_id = ? AND status = 'approved'
        """,
        (user_id,),
    ).fetchone()["count"]

    promo_total = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM promo_submissions
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()["count"]

    connection.close()

    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"📤 Отправлено видео: <b>{submitted}</b>\n"
        f"✅ Одобрено видео: <b>{approved_total}</b>\n"
        f"⏳ На проверке: <b>{pending}</b>\n"
        f"🎁 Прогресс: <b>{user['approved_count']}/5</b>\n"
        f"🎟 Бесплатных попыток: <b>{user['free_attempts']}</b>\n"
        f"👀 Просмотрено материалов: <b>{user['viewed_count']}</b>\n"
        f"🔎 Поисков: <b>{user['search_count']}</b>\n"
        f"📢 Отправлено пиар-проверок: <b>{promo_total}</b>\n"
        f"⭐ Донатов: <b>{user['donation_count']}</b>\n"
        f"💫 Всего поддержано: <b>{user['donated_stars']} Stars</b>"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=back_home_keyboard(),
    )


# =========================================================
# SEARCH
# =========================================================

async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_search"] = True

    await query.message.reply_text(
        "🔎 <b>Поиск по каталогу</b>\n\n"
        "Напиши название или часть названия видео.",
        parse_mode="HTML",
    )


async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_search"):
        return

    context.user_data.pop("waiting_search", None)

    user_id = update.effective_user.id
    search = update.message.text.strip()

    if not search:
        await update.message.reply_text("Напиши текст для поиска.")
        return

    ensure_user(user_id)

    connection = get_db()
    connection.execute(
        """
        UPDATE users
        SET search_count = search_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    videos = connection.execute(
        """
        SELECT id, name, price
        FROM content
        WHERE active = 1
          AND name LIKE ?
        ORDER BY id DESC
        LIMIT 30
        """,
        (f"%{search}%",),
    ).fetchall()

    connection.commit()
    connection.close()

    keyboard = []

    for video in videos:
        keyboard.append([
            InlineKeyboardButton(
                f"{video['name']} — {video['price']} ⭐",
                callback_data=f"content:{video['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="home")
    ])

    if not videos:
        text = f"🔎 По запросу «{search}» ничего не найдено."
    else:
        text = f"🔎 Результаты поиска: «{search}»"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# PRICE CATEGORIES
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
        text = f"Пока нет контента за {price} ⭐."
    else:
        text = f"🎬 Контент за {price} ⭐:"

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
                f"⭐ Оплатить {video['price']} Stars",
                callback_data=f"buy:{video['id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"price:{video['price']}",
            )
        ],
    ]

    await query.edit_message_text(
        f"🎬 {video['name']}\n\n"
        f"Цена: {video['price']} ⭐",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# TELEGRAM STARS PAYMENT
# =========================================================

async def buy_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    content_id = int(query.data.split(":")[1])

    connection = get_db()

    video = connection.execute(
        """
        SELECT name, price
        FROM content
        WHERE id = ?
          AND active = 1
        """,
        (content_id,),
    ).fetchone()

    connection.close()

    if not video:
        await query.message.reply_text("Контент больше недоступен.")
        return

    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=video["name"][:32],
        description=f"Доступ к {video['name']}"[:255],
        payload=f"content:{content_id}",
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=video["name"][:32],
                amount=video["price"],
            )
        ],
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    if not query.invoice_payload.startswith("content:"):
        await query.answer(
            ok=False,
            error_message="Неизвестный товар.",
        )
        return

    try:
        content_id = int(query.invoice_payload.split(":")[1])
    except ValueError:
        await query.answer(
            ok=False,
            error_message="Некорректный товар.",
        )
        return

    connection = get_db()

    content = connection.execute(
        """
        SELECT id, price
        FROM content
        WHERE id = ?
          AND active = 1
        """,
        (content_id,),
    ).fetchone()

    connection.close()

    if not content:
        await query.answer(
            ok=False,
            error_message="Контент больше недоступен.",
        )
        return

    if query.currency != "XTR" or query.total_amount != content["price"]:
        await query.answer(
            ok=False,
            error_message="Сумма платежа не совпадает с ценой товара.",
        )
        return

    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment

    if not payment or payment.currency != "XTR":
        return

    try:
        content_id = int(payment.invoice_payload.split(":")[1])
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Оплата получена, но товар не найден."
        )
        return

    user_id = update.effective_user.id

    connection = get_db()

    existing = connection.execute(
        """
        SELECT charge_id
        FROM payments
        WHERE charge_id = ?
        """,
        (payment.telegram_payment_charge_id,),
    ).fetchone()

    if existing:
        connection.close()
        return

    content = connection.execute(
        """
        SELECT name, file_id, price
        FROM content
        WHERE id = ?
          AND active = 1
        """,
        (content_id,),
    ).fetchone()

    if not content:
        connection.close()
        await update.message.reply_text(
            "Оплата получена, но контент сейчас недоступен. "
            "Обратись к администратору."
        )
        return

    if payment.total_amount != content["price"]:
        connection.close()
        await update.message.reply_text(
            "Оплата получена, но сумма не совпала с ценой товара. "
            "Обратись к администратору."
        )
        return

    connection.execute(
        """
        INSERT INTO payments(charge_id, user_id, content_id, amount)
        VALUES (?, ?, ?, ?)
        """,
        (
            payment.telegram_payment_charge_id,
            user_id,
            content_id,
            payment.total_amount,
        ),
    )

    ensure_user(user_id)

    connection.execute(
        """
        UPDATE users
        SET viewed_count = viewed_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    connection.commit()
    connection.close()

    try:
        await context.bot.send_video(
            chat_id=user_id,
            video=content["file_id"],
            caption=f"🎬 {content['name']}",
        )
    except Exception:
        logger.exception("Не удалось отправить оплаченный контент пользователю %s", user_id)
        await update.message.reply_text(
            "⭐ Оплата прошла успешно, но видео не удалось отправить автоматически.\n"
            "Обратись к администратору и укажи, что оплата уже прошла."
        )


# =========================================================
# USER VIDEO SUBMISSIONS
# =========================================================

async def start_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_submission"] = True

    await query.message.reply_text(
        "📤 <b>Отправка видео</b>\n\n"
        "Просто отправь мне видео — больше ничего заполнять не нужно.\n\n"
        "После отправки видео попадёт админу на проверку.\n\n"
        "🎁 За каждые <b>5 одобренных видео</b> ты получаешь "
        "<b>1 бесплатную попытку</b> на просмотр материала.",
        parse_mode="HTML",
    )


async def receive_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_submission"):
        return

    if not update.message.video:
        await update.message.reply_text(
            "❗ Просто отправь видео. Название заполнять не нужно."
        )
        return

    context.user_data.pop("waiting_submission", None)

    user_id = update.effective_user.id
    file_id = update.message.video.file_id

    ensure_user(user_id)

    connection = get_db()

    cursor = connection.execute(
        """
        INSERT INTO submissions(user_id, file_id, status)
        VALUES (?, ?, 'pending')
        """,
        (user_id, file_id),
    )

    submission_id = cursor.lastrowid

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

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_video(
                chat_id=admin_id,
                video=file_id,
                caption=(
                    f"📥 <b>Новая заявка #{submission_id}</b>\n\n"
                    f"👤 {user_display(update.effective_user)}\n"
                    f"🆔 User ID: <code>{user_id}</code>\n"
                    f"🎬 Название: <i>пока не задано</i>\n\n"
                    "После одобрения можно отдельно задать название."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception:
            logger.exception("Не удалось отправить заявку админу %s", admin_id)

    await update.message.reply_text(
        f"✅ Видео отправлено на проверку.\n\n"
        f"Номер заявки: <b>#{submission_id}</b>\n"
        "Тебе ничего больше заполнять не нужно.",
        parse_mode="HTML",
    )


# =========================================================
# VIDEO MODERATION + ADMIN NAMING
# =========================================================

async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        SELECT user_id, status, title
        FROM submissions
        WHERE id = ?
        """,
        (submission_id,),
    ).fetchone()

    if not submission:
        connection.close()
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    if submission["status"] != "pending":
        connection.close()
        await query.answer("Заявка уже обработана.", show_alert=True)
        return

    user_id = submission["user_id"]

    if action == "reject":
        connection.execute(
            """
            UPDATE submissions
            SET status = 'rejected',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewed_by = ?
            WHERE id = ?
            """,
            (query.from_user.id, submission_id),
        )
        connection.commit()
        connection.close()

        try:
            await query.edit_message_caption(
                caption=f"❌ Заявка #{submission_id} отклонена.",
            )
        except Exception:
            pass

        await context.bot.send_message(
            user_id,
            f"❌ Твоё видео (заявка #{submission_id}) было отклонено.",
        )
        return

    ensure_user(user_id)

    connection.execute(
        """
        UPDATE submissions
        SET status = 'approved',
            reviewed_at = CURRENT_TIMESTAMP,
            reviewed_by = ?
        WHERE id = ?
        """,
        (query.from_user.id, submission_id),
    )

    connection.execute(
        """
        UPDATE users
        SET approved_count = approved_count + 1
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

    keyboard = [[
        InlineKeyboardButton(
            "✏️ Дать название",
            callback_data=f"name_submission:{submission_id}",
        ),
    ]]

    try:
        await query.edit_message_caption(
            caption=(
                f"✅ <b>Заявка #{submission_id} одобрена.</b>\n"
                "Название пока не задано."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        pass

    message = f"✅ Твоё видео (заявка #{submission_id}) одобрено."

    if reward:
        message += (
            "\n\n🎁 Поздравляем!\n"
            "Ты получил 1 бесплатную попытку за 5 одобренных видео."
        )

    await context.bot.send_message(user_id, message)


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

    connection = get_db()
    submission = connection.execute(
        """
        SELECT user_id, status, title
        FROM submissions
        WHERE id = ?
        """,
        (submission_id,),
    ).fetchone()
    connection.close()

    if not submission:
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    if submission["status"] != "approved":
        await query.answer(
            "Сначала нужно одобрить заявку.",
            show_alert=True,
        )
        return

    context.user_data["naming_submission_id"] = submission_id

    await query.message.reply_text(
        f"✏️ Напиши название для заявки <b>#{submission_id}</b>.\n\n"
        "После отправки название сохранится.",
        parse_mode="HTML",
    )


async def save_submission_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    submission_id = context.user_data.get("naming_submission_id")

    if not submission_id:
        return

    if update.effective_user.id not in ADMIN_IDS:
        return

    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("Название не может быть пустым.")
        return

    context.user_data.pop("naming_submission_id", None)

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
        (name, submission_id),
    )

    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"✅ Название для заявки #{submission_id} сохранено:\n"
        f"🎬 {name}"
    )

    try:
        await context.bot.send_message(
            submission["user_id"],
            f"🎬 Администратор назначил твоему видео название:\n"
            f"<b>{name}</b>",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Не удалось уведомить пользователя о названии")


# =========================================================
# FREE ACCESS
# =========================================================

async def show_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "🎁 <b>Бесплатный просмотр</b>\n\n"
        f"📊 Одобрено: <b>{user['approved_count']}/5</b>\n"
        f"🎟 Бесплатных попыток: <b>{user['free_attempts']}</b>\n\n"
    )

    keyboard = []

    if prize and user["free_attempts"] > 0:
        text += f"🎬 Бесплатный материал: <b>{prize['name']}</b>\n\n"
        keyboard.append([
            InlineKeyboardButton(
                "🎁 Получить бесплатно",
                callback_data=f"claim:{prize['id']}",
            )
        ])
    elif not prize:
        text += "🎬 Бесплатный материал пока не настроен.\n\n"
    else:
        text += (
            "🎬 Бесплатный материал доступен, "
            "но бесплатных попыток пока нет.\n\n"
        )

    text += (
        "📢 <b>Просмотр за пиар</b>\n"
        "Перейди по ссылке, выполни пиар и отправь сюда "
        "скриншот. Администратор проверит его вручную.\n\n"
        "🎁 Каждые 5 одобренных скриншотов дают 1 бесплатную попытку "
        "(10 = 2 попытки, 15 = 3 и т.д.)."
    )

    keyboard.extend([
        [
            InlineKeyboardButton(
                "📢 Перейти на канал",
                url=PROMO_LINK,
            )
        ],
        [
            InlineKeyboardButton(
                "📸 Проверить пиар",
                callback_data="promo_check",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="home",
            )
        ],
    ])

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


PROMO_ALBUM_DELAY = 1.5
PROMO_ALBUM_MAX = 10
promo_album_buffers = {}


async def _flush_promo_album(application, user, key):
    """After Telegram finishes sending an album, create one moderation item per photo."""
    await asyncio.sleep(PROMO_ALBUM_DELAY)

    batch = promo_album_buffers.pop(key, None)
    if not batch:
        return

    user_id = user.id
    file_ids = batch["file_ids"][:PROMO_ALBUM_MAX]
    batch_id = uuid.uuid4().hex[:16]

    ensure_user(user_id)
    connection = get_db()
    promo_ids = []

    for file_id in file_ids:
        cursor = connection.execute(
            """
            INSERT INTO promo_submissions(user_id, file_id, status, batch_id)
            VALUES (?, ?, 'pending', ?)
            """,
            (user_id, file_id, batch_id),
        )
        promo_ids.append(cursor.lastrowid)

    connection.commit()
    connection.close()

    # Each photo gets its own moderation card, so the admin can approve/reject
    # them independently (e.g. 4 approve + 1 reject).
    for index, (promo_id, file_id) in enumerate(zip(promo_ids, file_ids), 1):
        keyboard = [[
            InlineKeyboardButton(
                f"✅ Одобрить #{index}",
                callback_data=f"promo_approve:{promo_id}",
            ),
            InlineKeyboardButton(
                f"❌ Отклонить #{index}",
                callback_data=f"promo_reject:{promo_id}",
            ),
        ]]

        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=(
                        f"📢 <b>Проверка пиара</b>\n\n"
                        f"📦 Пакет: <code>{batch_id}</code>\n"
                        f"📸 Фото: <b>{index}/{len(file_ids)}</b>\n"
                        f"🔢 Проверка: <b>#{promo_id}</b>\n\n"
                        f"👤 {user_display(user)}\n"
                        f"🆔 User ID: <code>{user_id}</code>\n\n"
                        "Проверь именно это фото. Каждое фото проверяется отдельно."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception:
                logger.exception("Не удалось отправить пиар-проверку админу %s", admin_id)

    if len(file_ids) > 1:
        await application.bot.send_message(
            user_id,
            f"✅ Получил <b>{len(file_ids)}</b> скриншотов.\n\n"
            "Администратор проверит каждый скриншот отдельно.\n"
            "Одобренные скриншоты накапливаются: каждые 5 одобренных "
            "скриншотов = 1 бесплатная попытка. Отклонённые нужно заменить.",
            parse_mode="HTML",
        )
    else:
        await application.bot.send_message(
            user_id,
            f"✅ Скриншот отправлен на проверку.\n"
            f"Номер проверки: <b>#{promo_ids[0]}</b>\n\n"
            "Дождись решения администратора.",
            parse_mode="HTML",
        )


async def start_promo_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_promo"] = True

    await query.message.reply_text(
        "📸 <b>Проверка пиара</b>\n\n"
        "1. Перейди по кнопке «Перейти на канал».\n"
        "2. Выполни нужный пиар.\n"
        "3. Сделай скриншоты, на которых видно выполнение пиара.\n"
        "4. Можешь отправить <b>от 1 до 10 фото за раз</b>.\n\n"
        "Если отправишь несколько фото одним альбомом, администратор "
        "проверит <b>каждое фото отдельно</b>. Например, из 5 скриншотов "
        "4 могут быть одобрены, а 1 отклонён. Тогда 4 засчитаются, "
        "а заменить нужно будет только 1.\n\n"
        "❗ Отправляй именно фотографии/скриншоты.",
        parse_mode="HTML",
    )


async def receive_promo_screenshot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.user_data.get("waiting_promo"):
        return

    if not update.message.photo:
        await update.message.reply_text(
            "❗ Для проверки пиара нужен именно скриншот/фотография."
        )
        return

    user = update.effective_user
    file_id = update.message.photo[-1].file_id
    media_group_id = update.message.media_group_id

    # Album: keep collecting photos for a short moment so Telegram's separate
    # media-group updates become one logical submission batch.
    if media_group_id:
        key = (user.id, media_group_id)
        batch = promo_album_buffers.setdefault(
            key,
            {"file_ids": [], "task": None},
        )

        if len(batch["file_ids"]) < PROMO_ALBUM_MAX:
            batch["file_ids"].append(file_id)

        if batch["task"] is None:
            batch["task"] = asyncio.create_task(
                _flush_promo_album(context.application, user, key)
            )
        return

    # Single photo: treat it as a one-photo batch and use the exact same
    # moderation/result logic as albums.
    key = (user.id, f"single-{uuid.uuid4().hex}")
    promo_album_buffers[key] = {"file_ids": [file_id], "task": None}
    promo_album_buffers[key]["task"] = asyncio.create_task(
        _flush_promo_album(context.application, user, key)
    )


async def moderate_promo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return

    await query.answer()

    action, promo_id_text = query.data.split(":")
    promo_id = int(promo_id_text)

    connection = get_db()

    promo = connection.execute(
        """
        SELECT id, user_id, status, batch_id
        FROM promo_submissions
        WHERE id = ?
        """,
        (promo_id,),
    ).fetchone()

    if not promo:
        connection.close()
        await query.answer("Проверка не найдена.", show_alert=True)
        return

    if promo["status"] != "pending":
        connection.close()
        await query.answer("Это фото уже обработано.", show_alert=True)
        return

    user_id = promo["user_id"]
    batch_id = promo["batch_id"]

    if action == "promo_reject":
        new_status = "rejected"
    else:
        new_status = "approved"

    connection.execute(
        """
        UPDATE promo_submissions
        SET status = ?,
            reviewed_at = CURRENT_TIMESTAMP,
            reviewed_by = ?
        WHERE id = ? AND status = 'pending'
        """,
        (new_status, query.from_user.id, promo_id),
    )

    # Promo rewards are counted by APPROVED screenshots, not by photos.
    # Every 5 approved screenshots = 1 free attempt.
    # This is cumulative, so e.g. 4 approved + 1 approved replacement = 1 attempt,
    # and 10 approved screenshots = 2 attempts.
    attempts_awarded_now = 0
    if new_status == "approved":
        user_progress = connection.execute(
            """
            SELECT promo_approved_photos
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        old_approved_photos = int(user_progress["promo_approved_photos"] or 0)
        new_approved_photos = old_approved_photos + 1

        old_attempts_earned = old_approved_photos // 5
        new_attempts_earned = new_approved_photos // 5
        attempts_awarded_now = new_attempts_earned - old_attempts_earned

        connection.execute(
            """
            UPDATE users
            SET promo_approved_photos = ?,
                free_attempts = free_attempts + ?
            WHERE user_id = ?
            """,
            (new_approved_photos, attempts_awarded_now, user_id),
        )

    # Determine whether the whole album/batch has been reviewed.
    batch_rows = connection.execute(
        """
        SELECT status
        FROM promo_submissions
        WHERE user_id = ? AND batch_id = ?
        """,
        (user_id, batch_id),
    ).fetchall()

    pending_count = sum(row["status"] == "pending" for row in batch_rows)
    approved_count = sum(row["status"] == "approved" for row in batch_rows)
    rejected_count = sum(row["status"] == "rejected" for row in batch_rows)
    total_count = len(batch_rows)

    user = connection.execute(
        """
        SELECT free_attempts, promo_approved_photos
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    promo_remainder = int(user["promo_approved_photos"] or 0) % 5
    promo_to_next = 5 - promo_remainder if promo_remainder else 5

    connection.commit()
    connection.close()

    # Mark only this admin card as processed. Other cards remain available.
    try:
        status_text = "✅ ОДОБРЕНО" if new_status == "approved" else "❌ ОТКЛОНЕНО"
        await query.edit_message_caption(
            caption=(
                f"{status_text}\n\n"
                f"📸 Проверка #{promo_id}\n"
                f"📦 Пакет: <code>{batch_id}</code>\n\n"
                "Это фото обработано. Остальные фото пакета проверяются отдельно."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Don't spam the user after every click. Send the final summary only when
    # all photos from the same album have been reviewed.
    if pending_count == 0:
        if rejected_count:
            # Rejected screenshots must be replaced one-for-one, while
            # approved screenshots continue accumulating toward each group of 5.
            need = rejected_count
            reward_line = (
                f"🎁 Новых попыток за эту проверку: <b>{attempts_awarded_now}</b>\n"
                if attempts_awarded_now
                else "🎁 Новых попыток пока нет: нужно накопить 5 одобренных скриншотов.\n"
            )
            await context.bot.send_message(
                user_id,
                f"📢 <b>Проверка пиара завершена</b>\n\n"
                f"📸 Всего скриншотов: <b>{total_count}</b>\n"
                f"✅ Одобрено: <b>{approved_count}</b>\n"
                f"❌ Отклонено: <b>{rejected_count}</b>\n\n"
                f"{reward_line}"
                f"📊 До следующей бесплатной попытки: <b>{promo_to_next}</b> одобренных скриншот(а).\n"
                f"⚠️ Нужно сделать и отправить ещё <b>{need}</b> скриншот(а), "
                "чтобы заменить отклонённые.\n\n"
                f"🎟 Всего доступно попыток: <b>{user['free_attempts']}</b>",
                parse_mode="HTML",
            )
        else:
            reward_line = (
                f"🎉 Получена новая бесплатная попытка: <b>{attempts_awarded_now}</b>\n"
                if attempts_awarded_now
                else "⏳ Бесплатная попытка пока не начислена — нужно 5 одобренных скриншотов.\n"
            )
            await context.bot.send_message(
                user_id,
                f"📢 <b>Проверка пиара завершена!</b>\n\n"
                f"📸 Проверено скриншотов: <b>{total_count}</b>\n"
                f"✅ Одобрено: <b>{approved_count}</b>\n\n"
                f"{reward_line}"
                f"📊 До следующей бесплатной попытки: <b>{promo_to_next}</b> одобренных скриншот(а).\n"
                f"🎟 Всего доступно попыток: <b>{user['free_attempts']}</b>",
                parse_mode="HTML",
            )


async def claim_free(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    prize_id = int(query.data.split(":")[1])

    ensure_user(user_id)

    connection = get_db()

    # Сначала проверяем, что именно этот приз существует.
    prize = connection.execute(
        """
        SELECT name, file_id
        FROM content
        WHERE id = ?
          AND is_free_prize = 1
          AND active = 1
        """,
        (prize_id,),
    ).fetchone()

    if not prize:
        connection.close()
        await query.message.reply_text(
            "Бесплатный материал сейчас недоступен."
        )
        return

    # Атомарно списываем попытку.
    cursor = connection.execute(
        """
        UPDATE users
        SET free_attempts = free_attempts - 1,
            viewed_count = viewed_count + 1
        WHERE user_id = ?
          AND free_attempts > 0
        """,
        (user_id,),
    )

    if cursor.rowcount != 1:
        connection.close()
        await query.message.reply_text(
            "У тебя нет бесплатных попыток."
        )
        return

    connection.commit()
    connection.close()

    try:
        await context.bot.send_video(
            chat_id=user_id,
            video=prize["file_id"],
            caption=f"🎁 Бесплатный просмотр: {prize['name']}",
        )
    except Exception:
        # Возвращаем попытку, если Telegram не дал отправить видео.
        connection = get_db()
        connection.execute(
            """
            UPDATE users
            SET free_attempts = free_attempts + 1,
                viewed_count = CASE
                    WHEN viewed_count > 0 THEN viewed_count - 1
                    ELSE 0
                END
            WHERE user_id = ?
            """,
            (user_id,),
        )
        connection.commit()
        connection.close()

        await query.message.reply_text(
            "Не удалось отправить видео. Бесплатная попытка возвращена."
        )


# =========================================================
# DONATION
# =========================================================

async def show_donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "⭐ <b>Поддержать проект</b>\n\n"
        "Спасибо за поддержку! ❤️\n"
        "Введи количество Stars, которое хочешь отправить.\n\n"
        "Например: <code>50</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
        ]),
    )

    context.user_data["waiting_donation"] = True


async def receive_donation_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.user_data.get("waiting_donation"):
        return

    text = update.message.text.strip()

    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text(
            "❗ Введи целое число Stars, например: 50"
        )
        return

    if amount < 1 or amount > 10000:
        await update.message.reply_text(
            "❗ Сумма должна быть от 1 до 10000 Stars."
        )
        return

    context.user_data.pop("waiting_donation", None)

    await context.bot.send_invoice(
        chat_id=update.effective_user.id,
        title="Поддержка проекта",
        description=f"Добровольная поддержка проекта на {amount} Stars.",
        payload=f"donation:{amount}",
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label="Поддержка проекта",
                amount=amount,
            )
        ],
    )


# =========================================================
# ADMIN — DONATION PAYMENT
# =========================================================

async def pre_checkout_donation_or_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.pre_checkout_query
    payload = query.invoice_payload

    if payload.startswith("donation:"):
        try:
            amount = int(payload.split(":")[1])
        except (ValueError, IndexError):
            await query.answer(
                ok=False,
                error_message="Некорректная сумма.",
            )
            return

        if query.currency != "XTR" or query.total_amount != amount:
            await query.answer(
                ok=False,
                error_message="Сумма платежа не совпадает.",
            )
            return

        await query.answer(ok=True)
        return

    await pre_checkout(update, context)


async def successful_payment_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    payment = update.message.successful_payment

    if not payment or payment.currency != "XTR":
        return

    if payment.invoice_payload.startswith("donation:"):
        try:
            amount = int(payment.invoice_payload.split(":")[1])
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Платёж получен, но сумма не распознана. Обратись к администратору."
            )
            return

        if payment.total_amount != amount:
            await update.message.reply_text(
                "Платёж получен, но сумма не совпала. Обратись к администратору."
            )
            return

        ensure_user(update.effective_user.id)

        connection = get_db()
        connection.execute(
            """
            UPDATE users
            SET donation_count = donation_count + 1,
                donated_stars = donated_stars + ?
            WHERE user_id = ?
            """,
            (amount, update.effective_user.id),
        )
        connection.commit()
        connection.close()

        await update.message.reply_text(
            f"❤️ Спасибо за поддержку!\n"
            f"Ты отправил {amount} Stars."
        )
        return

    await successful_payment(update, context)



async def user_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes ordinary text according to the user's current state."""
    if context.user_data.get("naming_submission_id"):
        await save_submission_name(update, context)
        return

    if context.user_data.get("waiting_donation"):
        await receive_donation_amount(update, context)
        return

    if context.user_data.get("waiting_search"):
        await search_text(update, context)
        return

    await update.message.reply_text(
        "Используй кнопки меню или /start.",
        reply_markup=main_menu(),
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

    await update.message.reply_text("📹 Отправь видео.")
    return ADD_VIDEO


async def add_video_receive(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message.video:
        await update.message.reply_text("Нужно отправить именно видео.")
        return ADD_VIDEO

    context.user_data["file_id"] = update.message.video.file_id

    await update.message.reply_text("✏️ Напиши название видео.")
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
        await update.message.reply_text(
            "Цена должна быть 15, 25 или 50."
        )
        return ADD_PRICE

    context.user_data["price"] = price

    keyboard = [[
        InlineKeyboardButton(
            "🎁 Да, сделать бесплатным материалом",
            callback_data="free_yes",
        ),
        InlineKeyboardButton(
            "❌ Нет",
            callback_data="free_no",
        ),
    ]]

    await update.message.reply_text(
        "Сделать это видео бесплатным материалом?",
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

    connection = get_db()

    if is_free:
        connection.execute(
            """
            UPDATE content
            SET is_free_prize = 0
            WHERE is_free_prize = 1
            """
        )

    connection.execute(
        """
        INSERT INTO content(name, price, file_id, is_free_prize)
        VALUES (?, ?, ?, ?)
        """,
        (name, price, file_id, is_free),
    )

    connection.commit()
    connection.close()

    context.user_data.clear()

    await query.edit_message_text(
        f"✅ Видео добавлено!\n\n"
        f"Название: {name}\n"
        f"Цена: {price} ⭐\n"
        f"Бесплатный материал: {'Да' if is_free else 'Нет'}"
    )

    return ConversationHandler.END


# =========================================================
# ADMIN — CATALOG / DELETE
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
        prize = " 🎁 БЕСПЛАТНЫЙ" if video["is_free_prize"] else ""

        lines.append(
            f"{status} #{video['id']} — "
            f"{video['name']} — "
            f"{video['price']} ⭐{prize}"
        )

    await update.message.reply_text("\n".join(lines))


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
        await update.message.reply_text(
            "Видео с таким ID не найдено."
        )
        return

    await update.message.reply_text(
        f"✅ Видео #{content_id} убрано из каталога."
    )


# =========================================================
# ADMIN — GIVE FREE ATTEMPTS
# =========================================================

async def give_free(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/give_free USER_ID\n"
            "/give_free USER_ID КОЛИЧЕСТВО\n\n"
            "Пример:\n"
            "/give_free 123456789 1"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID должен быть числом.")
        return

    amount = 1

    if len(context.args) >= 2:
        try:
            amount = int(context.args[1])
        except ValueError:
            await update.message.reply_text(
                "Количество попыток должно быть числом."
            )
            return

    if amount < 1 or amount > 100:
        await update.message.reply_text(
            "Количество должно быть от 1 до 100."
        )
        return

    ensure_user(user_id)

    connection = get_db()

    connection.execute(
        """
        UPDATE users
        SET free_attempts = free_attempts + ?
        WHERE user_id = ?
        """,
        (amount, user_id),
    )

    user = connection.execute(
        """
        SELECT free_attempts
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"✅ Пользователю <code>{user_id}</code> выдано "
        f"<b>{amount}</b> бесплатных попыток.\n"
        f"🎟 Всего теперь: <b>{user['free_attempts']}</b>",
        parse_mode="HTML",
    )

    try:
        await context.bot.send_message(
            user_id,
            f"🎁 Тебе выдано <b>{amount}</b> бесплатных "
            f"попыток администраторами.\n\n"
            "Открой «🎁 Бесплатный просмотр», чтобы использовать их.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Не удалось уведомить пользователя %s", user_id)


# =========================================================
# HOME / CANCEL
# =========================================================

async def home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data.pop("waiting_search", None)
    context.user_data.pop("waiting_donation", None)
    context.user_data.pop("waiting_promo", None)

    await query.edit_message_text(
        "Главное меню:",
        reply_markup=main_menu(),
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=main_menu(),
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

    # Админское добавление видео.
    add_video_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add_video", add_video_start),
        ],
        states={
            ADD_VIDEO: [
                MessageHandler(filters.VIDEO, add_video_receive),
            ],
            ADD_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_video_name,
                ),
            ],
            ADD_PRICE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_video_price,
                ),
            ],
            ADD_FREE: [
                CallbackQueryHandler(
                    finish_add_video,
                    pattern=r"^free_(yes|no)$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
    )

    # Команды.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("terms", terms))
    application.add_handler(CommandHandler("paysupport", pay_support))
    application.add_handler(CommandHandler("videos", admin_videos))
    application.add_handler(CommandHandler("delete_video", delete_video))
    application.add_handler(CommandHandler("give_free", give_free))
    application.add_handler(add_video_conversation)

    # Кнопки каталога.
    application.add_handler(
        CallbackQueryHandler(
            show_price,
            pattern=r"^price:(15|25|50)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_content,
            pattern=r"^content:\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            buy_content,
            pattern=r"^buy:\d+$",
        )
    )

    # Пользовательское видео.
    application.add_handler(
        CallbackQueryHandler(
            start_submission,
            pattern=r"^submit$",
        )
    )

    # Бесплатный просмотр / пиар.
    application.add_handler(
        CallbackQueryHandler(
            show_free,
            pattern=r"^free$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            start_promo_check,
            pattern=r"^promo_check$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            claim_free,
            pattern=r"^claim:\d+$",
        )
    )

    # Профиль / поиск / донат.
    application.add_handler(
        CallbackQueryHandler(
            show_profile,
            pattern=r"^profile$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            start_search,
            pattern=r"^search$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_donate,
            pattern=r"^donate$",
        )
    )

    # Модерация видео.
    application.add_handler(
        CallbackQueryHandler(
            moderate,
            pattern=r"^(approve|reject):\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            start_submission_naming,
            pattern=r"^name_submission:\d+$",
        )
    )

    # Модерация пиара.
    application.add_handler(
        CallbackQueryHandler(
            moderate_promo,
            pattern=r"^promo_(approve|reject):\d+$",
        )
    )

    # Назад.
    application.add_handler(
        CallbackQueryHandler(
            home,
            pattern=r"^home$",
        )
    )

    # Платежи.
    application.add_handler(
        PreCheckoutQueryHandler(
            pre_checkout_donation_or_content
        )
    )
    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_router,
        )
    )

    # Текстовые состояния пользователя.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            user_text_router,
        )
    )

    # Фото для проверки пиара.
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_promo_screenshot,
        )
    )

    # Видео от пользователей.
    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            receive_submission,
        )
    )

    logger.info("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()
