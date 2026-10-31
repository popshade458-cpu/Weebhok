import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}
DATABASE_PATH = os.getenv("DATABASE_PATH", "library.db")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", 8443))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # مثال: https://your-app.onrender.com

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("lecture_library")

CATALOG = {
    "فلسفة الأخلاق": [],
    "أسس الفلسفة": [],
    "فكر شرقي قديم — د. جيهان": [],
    "فكر شرقي قديم — د. نشوى": [],
    "الفلسفة اليونانية": [],
    "نصوص ومصطلحات فلسفية — د. إسلام عمارة": [],
    "نصوص ومصطلحات فلسفية — د. دينا": [],
    "اللغة الأوروبية": [],
    "مناهج البحث الفلسفي": [],
}
FILE_TYPES = ["PDF", "تسجيل صوتي", "رسالة صوتية", "صور", "فيديو", "ملف آخر"]

SUBJECT, LECTURE_NO, FILE_TYPE, FILE_UPLOAD = range(4)


def db_connect():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    parent = os.path.dirname(os.path.abspath(DATABASE_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                teacher TEXT NOT NULL,
                lecture_no INTEGER NOT NULL,
                file_type TEXT NOT NULL,
                telegram_file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_name TEXT,
                media_kind TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_lookup ON files(subject, teacher, lecture_no, file_type)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_name TEXT,
                media_kind TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                registered INTEGER NOT NULL DEFAULT 0
            )
        """)
        course_renames = {
            ("فكر شرقي قديم", "د. جيهان"): "فكر شرقي قديم — د. جيهان",
            ("فكر شرقي قديم", "د. نشوى"): "فكر شرقي قديم — د. نشوى",
            ("نصوص ومصطلحات فلسفية", "د. إسلام عمارة"): "نصوص ومصطلحات فلسفية — د. إسلام عمارة",
            ("نصوص ومصطلحات فلسفية", "د. اسلام عمارة"): "نصوص ومصطلحات فلسفية — د. إسلام عمارة",
            ("نصوص ومصطلحات فلسفية", "د. دينا"): "نصوص ومصطلحات فلسفية — د. دينا",
        }
        for (old_subject, old_teacher), new_subject in course_renames.items():
            conn.execute(
                "UPDATE files SET subject=?, teacher='' WHERE subject=? AND teacher=?",
                (new_subject, old_subject, old_teacher),
            )
        conn.execute(
            "UPDATE files SET teacher='' WHERE subject IN (?, ?, ?, ?, ?)",
            (
                "فلسفة الأخلاق",
                "أسس الفلسفة",
                "الفلسفة اليونانية",
                "اللغة الأوروبية",
                "مناهج البحث الفلسفي",
            ),
        )


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def catalog_keyboard(prefix: str = "subject") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"{prefix}|{i}")]
        for i, name in enumerate(CATALOG.keys())
    ]
    return InlineKeyboardMarkup(rows)


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="home")]]
    )


def get_subjects_with_files():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT subject FROM files ORDER BY subject"
        ).fetchall()
    return [r["subject"] for r in rows]


def get_lecture_numbers(subject: str):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT lecture_no FROM files WHERE subject=? ORDER BY lecture_no",
            (subject,),
        ).fetchall()
    return [r["lecture_no"] for r in rows]


def get_file_types(subject: str, lecture_no: int):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT file_type FROM files WHERE subject=? AND lecture_no=? ORDER BY file_type",
            (subject, lecture_no),
        ).fetchall()
    return [r["file_type"] for r in rows]


def get_files(subject: str, lecture_no: int, file_type: str):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM files WHERE subject=? AND lecture_no=? AND file_type=? ORDER BY id DESC",
            (subject, lecture_no, file_type),
        ).fetchall()


def main_menu_markup(user_id: Optional[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📚 تصفح المحاضرات", callback_data="browse")]]
    if is_admin(user_id):
        rows.append(
            [InlineKeyboardButton("➕ إضافة محاضرة", callback_data="admin_add")]
        )
    rows.append([InlineKeyboardButton("ℹ️ طريقة الاستخدام", callback_data="help")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        "أهلًا بيك في مكتبة محاضرات قسم الفلسفة 📚\n\n"
        "اختار تصفح المحاضرات، وبعدها المادة ورقم المحاضرة ونوع الملف."
    )
    if is_admin(user.id if user else None):
        text += "\n\nبصفتك أدمن، تقدر تضيف ملف من زر الإضافة أو باستخدام الأمر /add، وتحذف تسجيلًا من المكتبة باستخدام /delete."
    await update.effective_message.reply_text(
        text, reply_markup=main_menu_markup(user.id if user else None)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "طريقة الاستخدام:\n"
        "1) اختار المادة.\n2) اختار رقم المحاضرة.\n3) اختار نوع الملف.\n\n"
        "لو الملف مش موجود، ارجع للأدمن عشان يضيفه.\n"
        "إضافة الملفات متاحة للأدمن فقط."
    )


async def edit_or_send_menu(query, text: str, reply_markup: InlineKeyboardMarkup):
    message = query.message
    if message and message.text is not None:
        await query.edit_message_text(text, reply_markup=reply_markup)
    elif message:
        await message.reply_text(text, reply_markup=reply_markup)


async def browse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "home":
        for key in (
            "browse_subject",
            "browse_lecture",
            "browse_types",
            "browse_subjects",
        ):
            context.user_data.pop(key, None)
        await edit_or_send_menu(
            query, "القائمة الرئيسية 📚", main_menu_markup(query.from_user.id)
        )
        return

    if data == "help":
        await query.edit_message_text(
            "طريقة الاستخدام:\nاختار المادة ثم رقم المحاضرة ونوع الملف.\n\n"
            "لو مش لاقي ملف، غالبًا لسه ما اترفعش للمكتبة.\n"
            "إضافة الملفات متاحة للأدمن فقط.",
            reply_markup=back_home_keyboard(),
        )
        return

    if data == "browse":
        subjects = get_subjects_with_files()
        if not subjects:
            await query.edit_message_text(
                "لسه مفيش محاضرات مضافة للمكتبة. جرّب تاني بعد ما الأدمن يرفع أول ملف.",
                reply_markup=back_home_keyboard(),
            )
            return
        rows = [
            [InlineKeyboardButton(s, callback_data=f"bsub|{i}")]
            for i, s in enumerate(subjects)
        ]
        rows.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="home")])
        context.user_data["browse_subjects"] = subjects
        await query.edit_message_text(
            "اختار المادة:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("bsub|"):
        subjects = context.user_data.get("browse_subjects") or get_subjects_with_files()
        try:
            subject = subjects[int(data.split("|", 1)[1])]
        except (ValueError, IndexError):
            await query.edit_message_text(
                "القائمة انتهت صلاحيتها. ابدأ من جديد.",
                reply_markup=back_home_keyboard(),
            )
            return
        context.user_data["browse_subject"] = subject
        lectures = get_lecture_numbers(subject)
        rows = [
            [InlineKeyboardButton(f"المحاضرة {n}", callback_data=f"blec|{n}")]
            for n in lectures
        ]
        rows.append([InlineKeyboardButton("⬅️ المواد", callback_data="browse")])
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")])
        await query.edit_message_text(
            f"{subject}\nاختار رقم المحاضرة:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data == "bbacklectures":
        subject = context.user_data.get("browse_subject")
        if not subject:
            await query.edit_message_text(
                "ابدأ من القائمة الرئيسية.", reply_markup=back_home_keyboard()
            )
            return
        lectures = get_lecture_numbers(subject)
        rows = [
            [InlineKeyboardButton(f"المحاضرة {n}", callback_data=f"blec|{n}")]
            for n in lectures
        ]
        rows.append([InlineKeyboardButton("⬅️ المواد", callback_data="browse")])
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")])
        await query.edit_message_text(
            f"{subject}\nاختار رقم المحاضرة:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data == "bbacktypes":
        subject = context.user_data.get("browse_subject")
        lecture_no = context.user_data.get("browse_lecture")
        if not subject or not lecture_no:
            await query.edit_message_text(
                "ابدأ من القائمة الرئيسية.", reply_markup=back_home_keyboard()
            )
            return
        types = get_file_types(subject, lecture_no)
        context.user_data["browse_types"] = types
        rows = [
            [InlineKeyboardButton(t, callback_data=f"btype|{i}")]
            for i, t in enumerate(types)
        ]
        rows.append(
            [InlineKeyboardButton("⬅️ المحاضرات", callback_data="bbacklectures")]
        )
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")])
        await edit_or_send_menu(
            query,
            f"{subject}\nالمحاضرة {lecture_no}\nاختار نوع الملف:",
            InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("blec|"):
        try:
            lecture_no = int(data.split("|", 1)[1])
        except ValueError:
            return
        subject = context.user_data.get("browse_subject")
        if not subject:
            await query.edit_message_text(
                "ابدأ من القائمة الرئيسية.", reply_markup=back_home_keyboard()
            )
            return
        types = get_file_types(subject, lecture_no)
        context.user_data["browse_lecture"] = lecture_no
        rows = [
            [InlineKeyboardButton(t, callback_data=f"btype|{i}")]
            for i, t in enumerate(types)
        ]
        rows.append(
            [InlineKeyboardButton("⬅️ المحاضرات", callback_data="bbacklectures")]
        )
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")])
        context.user_data["browse_types"] = types
        await query.edit_message_text(
            f"{subject}\nالمحاضرة {lecture_no}\nاختار نوع الملف:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("btype|"):
        types = context.user_data.get("browse_types", [])
        try:
            file_type = types[int(data.split("|", 1)[1])]
        except (ValueError, IndexError):
            await query.edit_message_text(
                "القائمة انتهت صلاحيتها. ابدأ من جديد.",
                reply_markup=back_home_keyboard(),
            )
            return
        subject = context.user_data.get("browse_subject")
        lecture_no = context.user_data.get("browse_lecture")
        files = get_files(subject, lecture_no, file_type)
        if not files:
            await query.edit_message_text(
                "الملف مش موجود حاليًا.", reply_markup=back_home_keyboard()
            )
            return
        await query.edit_message_text("تمام، هبعتلك الملف دلوقتي 📎")
        for row in files:
            await send_stored_file(context, query.message.chat_id, row)
        return

    if data == "admin_add":
        if not is_admin(query.from_user.id):
            await query.answer("الإضافة متاحة للأدمن فقط.", show_alert=True)
            return
        await query.message.reply_text("لإضافة ملف، اكتب /add واتبع الخطوات.")
        return


async def send_stored_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, row):
    caption = f"{row['subject']}\nالمحاضرة {row['lecture_no']} | {row['file_type']}"
    navigation = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ رجوع لأنواع الملفات", callback_data="bbacktypes")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")],
        ]
    )
    file_id = row["telegram_file_id"]
    kind = row["media_kind"]
    if kind == "audio":
        await context.bot.send_audio(
            chat_id=chat_id, audio=file_id, caption=caption, reply_markup=navigation
        )
    elif kind == "voice":
        await context.bot.send_voice(
            chat_id=chat_id, voice=file_id, caption=caption, reply_markup=navigation
        )
    elif kind == "video":
        await context.bot.send_video(
            chat_id=chat_id, video=file_id, caption=caption, reply_markup=navigation
        )
    elif kind == "photo":
        await context.bot.send_photo(
            chat_id=chat_id, photo=file_id, caption=caption, reply_markup=navigation
        )
    else:
        await context.bot.send_document(
            chat_id=chat_id, document=file_id, caption=caption, reply_markup=navigation
        )


def admin_subject_keyboard():
    return ReplyKeyboardMarkup(
        [[s] for s in CATALOG.keys()] + [["إلغاء"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    chat = update.effective_chat
    if chat:
        await update.effective_message.reply_text(f"Chat ID: {chat.id}")


def get_pending_files():
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM pending_files WHERE registered=0 ORDER BY id DESC LIMIT 20"
        ).fetchall()


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.effective_message.reply_text(
            "عذرًا، إضافة الملفات متاحة للأدمن فقط."
        )
        return ConversationHandler.END
    if not STORAGE_CHAT_ID:
        await update.effective_message.reply_text(
            "إعداد جروب التخزين لسه ناقص. ابعت /chatid داخل جروب التخزين، وبعدها ضيف STORAGE_CHAT_ID في إعدادات تشغيل البوت وأعد تشغيله."
        )
        return ConversationHandler.END
    pending = get_pending_files()
    if not pending:
        await update.effective_message.reply_text(
            "مفيش ملفات جديدة متسجلة من جروب التخزين لحد دلوقتي.\n\n"
            "1) اتأكد إن STORAGE_CHAT_ID مضبوط.\n"
            "2) ابعت الملف في جروب التخزين بعد تشغيل النسخة المعدلة.\n"
            "3) ارجع هنا واكتب /add."
        )
        return ConversationHandler.END
    if context.args:
        try:
            chosen_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                "اكتب رقم الملف بعد الأمر، مثال: /add 12"
            )
            return ConversationHandler.END
        chosen = next((row for row in pending if row["id"] == chosen_id), None)
        if not chosen:
            await update.effective_message.reply_text(
                "رقم الملف مش موجود ضمن الملفات الجديدة. اكتب /add من غير رقم لعرض القائمة."
            )
            return ConversationHandler.END
    else:
        lines = ["الملفات الجديدة في جروب التخزين (الأحدث أولًا):"]
        for row in pending:
            label = row["file_name"] or row["media_kind"]
            lines.append(f"{row['id']} — {label}")
        lines.append("\nاختار الملف بكتابة /add ورقم الملف. مثال: /add 12")
        await update.effective_message.reply_text("\n".join(lines))
        return ConversationHandler.END
    context.user_data["new_file"] = {
        "pending_id": chosen["id"],
        "telegram_file_id": chosen["telegram_file_id"],
        "file_unique_id": chosen["file_unique_id"],
        "file_name": chosen["file_name"],
        "media_kind": chosen["media_kind"],
    }
    await update.effective_message.reply_text(
        "اختار المادة:", reply_markup=admin_subject_keyboard()
    )
    return SUBJECT


async def admin_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text == "إلغاء":
        return await cancel(update, context)
    if text not in CATALOG:
        await update.effective_message.reply_text("اختار مادة من الأزرار الموجودة تحت.")
        return SUBJECT
    context.user_data["new_file"]["subject"] = text
    context.user_data["new_file"]["teacher"] = ""
    await update.effective_message.reply_text(
        "اكتب رقم المحاضرة بالأرقام، مثال: 3", reply_markup=ReplyKeyboardRemove()
    )
    return LECTURE_NO


async def admin_lecture_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text == "إلغاء":
        return await cancel(update, context)
    try:
        number = int(text)
        if number < 1 or number > 999:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "اكتب رقم محاضرة صحيح، مثال: 1 أو 2 أو 3."
        )
        return LECTURE_NO
    context.user_data["new_file"]["lecture_no"] = number
    keyboard = ReplyKeyboardMarkup(
        [[t] for t in FILE_TYPES] + [["إلغاء"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.effective_message.reply_text("اختار نوع الملف:", reply_markup=keyboard)
    return FILE_TYPE


async def admin_file_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text == "إلغاء":
        return await cancel(update, context)
    if text not in FILE_TYPES:
        await update.effective_message.reply_text(
            "اختار نوع الملف من الأزرار الموجودة تحت."
        )
        return FILE_TYPE
    context.user_data["new_file"]["file_type"] = text
    await save_selected_pending_file(update, context)
    return ConversationHandler.END


async def save_selected_pending_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    details = context.user_data.get("new_file", {})
    required = [
        "subject",
        "lecture_no",
        "file_type",
        "pending_id",
        "telegram_file_id",
        "media_kind",
    ]
    if not user or not all(k in details for k in required):
        await update.effective_message.reply_text(
            "بيانات الملف ناقصة. ابدأ من جديد باستخدام /add.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("new_file", None)
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO files(subject, teacher, lecture_no, file_type, telegram_file_id, file_unique_id, file_name, media_kind, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                details["subject"],
                "",
                details["lecture_no"],
                details["file_type"],
                details["telegram_file_id"],
                details.get("file_unique_id"),
                details.get("file_name"),
                details["media_kind"],
                user.id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            "UPDATE pending_files SET registered=1 WHERE id=?", (details["pending_id"],)
        )
    await update.effective_message.reply_text(
        "✅ اتضاف الملف للمكتبة بنجاح!\n\n"
        f"المادة: {details['subject']}\nالمحاضرة: {details['lecture_no']}\nالنوع: {details['file_type']}\n\n"
        "تقدر تضيف ملف تاني باستخدام /add.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.pop("new_file", None)


async def storage_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not chat or str(chat.id) != STORAGE_CHAT_ID:
        return
    file_id = unique_id = file_name = media_kind = None
    if message.document:
        file_id, unique_id, file_name, media_kind = (
            message.document.file_id,
            message.document.file_unique_id,
            message.document.file_name or "document",
            "document",
        )
    elif message.audio:
        file_id, unique_id, file_name, media_kind = (
            message.audio.file_id,
            message.audio.file_unique_id,
            message.audio.file_name or message.audio.title or "audio",
            "audio",
        )
    elif message.voice:
        file_id, unique_id, file_name, media_kind = (
            message.voice.file_id,
            message.voice.file_unique_id,
            "voice_message",
            "voice",
        )
    elif message.video:
        file_id, unique_id, file_name, media_kind = (
            message.video.file_id,
            message.video.file_unique_id,
            message.video.file_name or "video",
            "video",
        )
    elif message.photo:
        photo = message.photo[-1]
        file_id, unique_id, file_name, media_kind = (
            photo.file_id,
            photo.file_unique_id,
            "photo",
            "photo",
        )
    if not file_id:
        return
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT id FROM pending_files WHERE file_unique_id=? AND source_chat_id=? AND source_message_id=?",
            (unique_id, chat.id, message.message_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO pending_files(telegram_file_id, file_unique_id, file_name, media_kind, source_message_id, source_chat_id, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    unique_id,
                    file_name,
                    media_kind,
                    message.message_id,
                    chat.id,
                    user.id if user else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    if user and is_admin(user.id):
        await message.reply_text(
            "اتسجل الملف كمادة غير مضافة للمكتبة. افتح البوت في الخاص واكتب /add لاختيار الملف وتسجيل بياناته."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_file", None)
    await update.effective_message.reply_text(
        "تم إلغاء إضافة المحاضرة.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("الأمر ده متاح للأدمن فقط.")
        return
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        subjects = conn.execute(
            "SELECT COUNT(DISTINCT subject) AS n FROM files"
        ).fetchone()["n"]
    await update.effective_message.reply_text(
        f"📊 إحصائيات المكتبة\nعدد الملفات: {total}\nعدد المواد التي بها ملفات: {subjects}"
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.effective_message.reply_text("الأمر ده متاح للأدمن فقط.")
        return
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, subject, lecture_no, file_type, file_name FROM files ORDER BY id DESC LIMIT 40"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("مفيش ملفات مسجلة في المكتبة حاليًا.")
        return
    buttons = []
    for row in rows:
        label = f"#{row['id']} {row['subject']} — محاضرة {row['lecture_no']} ({row['file_type']})"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"del|{row['id']}")])
    await update.effective_message.reply_text(
        "اختار التسجيل اللي عايز تحذفه من المكتبة.\nملاحظة: ده هيحذف التسجيل من المكتبة فقط، مش الملف الأصلي في جروب التخزين.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id if user else None):
        await query.answer("الأمر ده متاح للأدمن فقط.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("del|"):
        try:
            file_id = int(data.split("|", 1)[1])
        except ValueError:
            await query.edit_message_text("اختيار غير صحيح. اكتب /delete من جديد.")
            return
        with db_connect() as conn:
            row = conn.execute(
                "SELECT id, subject, lecture_no, file_type, file_name FROM files WHERE id=?",
                (file_id,),
            ).fetchone()
        if row is None:
            await query.edit_message_text("التسجيل ده مش موجود في المكتبة دلوقتي.")
            return
        details = (
            f"هل أنت متأكد إنك عايز تحذف التسجيل ده من المكتبة؟\n\n"
            f"المادة: {row['subject']}\nالمحاضرة: {row['lecture_no']}\n"
            f"النوع: {row['file_type']}\nالملف: {row['file_name'] or 'بدون اسم'}\n\n"
            "الملف الأصلي في جروب التخزين مش هيتحذف."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑️ تأكيد الحذف", callback_data=f"delconfirm|{file_id}"
                    ),
                    InlineKeyboardButton("إلغاء", callback_data="delcancel"),
                ]
            ]
        )
        await query.edit_message_text(details, reply_markup=keyboard)
        return
    if data.startswith("delconfirm|"):
        try:
            file_id = int(data.split("|", 1)[1])
        except ValueError:
            await query.edit_message_text("اختيار غير صحيح. اكتب /delete من جديد.")
            return
        with db_connect() as conn:
            cur = conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            deleted = cur.rowcount
        if deleted:
            await query.edit_message_text(
                "✅ تم حذف التسجيل من المكتبة. الملف الأصلي في جروب التخزين زي ما هو."
            )
        else:
            await query.edit_message_text("التسجيل ده مش موجود في المكتبة دلوقتي.")
        return
    if data == "delcancel":
        await query.edit_message_text("تم إلغاء الحذف. مفيش حاجة اتحذفت.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Unhandled exception while processing update", exc_info=context.error
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable.")
    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty: no users can upload files until it is configured."
        )
    if not WEBHOOK_URL:
        raise SystemExit("Missing WEBHOOK_URL environment variable.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    add_conversation = ConversationHandler(
        entry_points=[CommandHandler("add", add_command)],
        states={
            SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_subject)],
            LECTURE_NO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_lecture_no)
            ],
            FILE_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_file_type)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(add_conversation)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(
        MessageHandler(
            (
                filters.Document.ALL
                | filters.AUDIO
                | filters.VOICE
                | filters.VIDEO
                | filters.PHOTO
            )
            & ~filters.COMMAND,
            storage_media_handler,
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            delete_callback, pattern=r"^(del\||delconfirm\||delcancel$)"
        )
    )
    app.add_handler(CallbackQueryHandler(browse_callback))
    app.add_error_handler(error_handler)

    logger.info("Lecture library bot is running with webhook")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
    )


if __name__ == "__main__":
    main()