import asyncio
import io
import logging
import os
import re
import time
from collections import deque
from typing import Dict, List, Optional, Deque

import img2pdf
from PIL import Image, ImageOps, UnidentifiedImageError
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Optional HEIC support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---- Environment variables ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set. Please set it in .env file or environment.")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip())
    except ValueError:
        logger.error("ADMIN_IDS contains invalid integers. Ignoring.")

# Hard limit for output PDF size (bytes)
MAX_PDF_SIZE = 1 * 1024 * 1024  # 1 MB

# Default compression settings
DEFAULT_SETTINGS = {
    "quality": 60,
    "max_width": 1600,
    "dpi": 150,
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB input limit

# Storage
user_settings: Dict[int, Dict] = {}
album_photos: Dict[str, List[str]] = {}
album_tasks: Dict[str, asyncio.Task] = {}
album_chat_id: Dict[str, int] = {}
album_user_id: Dict[str, int] = {}

# Pending image sets waiting for filename
pending_images: Dict[int, Dict] = {}  # user_id -> {"file_ids": list, "chat_id": int, "timestamp": float}
PENDING_TIMEOUT = 300  # 5 minutes

# Recent errors for admin
recent_errors: Deque[str] = deque(maxlen=10)

# Semaphore to limit concurrent image processing (if needed)
PROCESS_SEMAPHORE = asyncio.Semaphore(4)


def get_user_settings(user_id: int) -> Dict:
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]


async def download_file(file_id: str, bot) -> bytes:
    tg_file = await bot.get_file(file_id)
    return await tg_file.download_as_bytearray()


# ----------------------------------------------------------------------
# Command handlers
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    heic_note = "✅ HEIC/HEIF support enabled." if HEIC_SUPPORT else \
                "⚠️ HEIC/HEIF not supported. Install `pillow-heif`."
    await update.message.reply_text(
        "👋 Hi! I convert images to a compressed PDF.\n\n"
        "📌 The output PDF will always be **≤ 1 MB**.\n"
        "• Single image → PDF\n"
        "• Multiple images in an album → combined PDF\n"
        "• Use /settings to adjust compression (1 MB limit always enforced)\n"
        "• Use /cancel to abort a pending album\n"
        "• After sending images, you will be asked for a filename\n\n"
        f"{heic_note}\n"
        "Just send the images!"
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    s = get_user_settings(user_id)
    keyboard = [
        [
            InlineKeyboardButton(f"Quality: {s['quality']}", callback_data="set_quality"),
            InlineKeyboardButton(f"Max width: {s['max_width']}", callback_data="set_max_width"),
        ],
        [
            InlineKeyboardButton(f"DPI: {s['dpi']}", callback_data="set_dpi"),
            InlineKeyboardButton("Reset defaults", callback_data="reset_settings"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🔧 Adjust your compression settings:", reply_markup=reply_markup)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    cancelled = False
    # Cancel album collection
    for mg_id, chat in list(album_chat_id.items()):
        if chat == chat_id:
            if mg_id in album_tasks:
                album_tasks[mg_id].cancel()
                album_tasks.pop(mg_id, None)
            album_photos.pop(mg_id, None)
            album_chat_id.pop(mg_id, None)
            album_user_id.pop(mg_id, None)
            cancelled = True
    # Cancel pending image set waiting for filename
    if user_id in pending_images:
        pending_images.pop(user_id, None)
        cancelled = True
    if cancelled:
        await update.message.reply_text("✅ Cancelled pending operations.")
    else:
        await update.message.reply_text("No pending operations to cancel.")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    active_album_tasks = len(album_tasks)
    pending_filename_users = len(pending_images)
    total_users_with_settings = len(user_settings)
    recent_errors_list = list(recent_errors)
    msg = (
        "📊 **Bot Status**\n\n"
        f"Active album tasks: {active_album_tasks}\n"
        f"Pending filename requests: {pending_filename_users}\n"
        f"Users with custom settings: {total_users_with_settings}\n"
        f"Recent errors: {len(recent_errors_list)}\n"
    )
    if recent_errors_list:
        msg += "\n**Last errors:**\n"
        for err in recent_errors_list[-5:]:
            msg += f"- {err}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ----------------------------------------------------------------------
# Inline button handler for settings
# ----------------------------------------------------------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    s = get_user_settings(user_id)
    data = query.data
    if data == "reset_settings":
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
        await query.edit_message_text("Settings reset to defaults.")
        return
    if data == "set_quality":
        current = s["quality"]
        new_quality = 80 if current >= 80 else (60 if current >= 60 else 40)
        s["quality"] = new_quality
    elif data == "set_max_width":
        current = s["max_width"]
        new_width = 2000 if current >= 2000 else (1600 if current >= 1600 else 1200)
        s["max_width"] = new_width
    elif data == "set_dpi":
        current = s["dpi"]
        new_dpi = 200 if current >= 200 else (150 if current >= 150 else 100)
        s["dpi"] = new_dpi
    keyboard = [
        [
            InlineKeyboardButton(f"Quality: {s['quality']}", callback_data="set_quality"),
            InlineKeyboardButton(f"Max width: {s['max_width']}", callback_data="set_max_width"),
        ],
        [
            InlineKeyboardButton(f"DPI: {s['dpi']}", callback_data="set_dpi"),
            InlineKeyboardButton("Reset defaults", callback_data="reset_settings"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("🔧 Adjust your compression settings:", reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Media handlers
# ----------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.photo:
        return
    photo_file = message.photo[-1]
    await _handle_media(update, context, photo_file.file_id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.document:
        return
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await message.reply_text(f"❌ File too large. Maximum input size is {MAX_FILE_SIZE // (1024*1024)} MB.")
        return
    await _handle_media(update, context, doc.file_id)


async def _handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # If user already has a pending image set, inform them
    if user_id in pending_images:
        await update.message.reply_text("ℹ️ You already have a pending image set. Please provide a filename first or /cancel.")

    message = update.message
    media_group_id = message.media_group_id

    if media_group_id:
        # Part of an album: collect files, then after delay ask for filename
        if media_group_id not in album_photos:
            album_photos[media_group_id] = []
            album_chat_id[media_group_id] = chat_id
            album_user_id[media_group_id] = user_id

        album_photos[media_group_id].append(file_id)

        if media_group_id in album_tasks:
            album_tasks[media_group_id].cancel()

        task = asyncio.create_task(_process_album_after_delay(update, context, media_group_id))
        album_tasks[media_group_id] = task
    else:
        # Single image: ask for filename immediately
        await _ask_for_filename_for_single(update, context, [file_id])


async def _process_album_after_delay(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     media_group_id: str) -> None:
    """Wait 3 seconds to collect all album images, then ask for filename."""
    await asyncio.sleep(3)
    chat_id = album_chat_id.get(media_group_id)
    if chat_id is None:
        return
    file_ids = album_photos.get(media_group_id, [])
    user_id = album_user_id.get(media_group_id)
    # Clean up album storage
    album_photos.pop(media_group_id, None)
    album_chat_id.pop(media_group_id, None)
    album_user_id.pop(media_group_id, None)
    album_tasks.pop(media_group_id, None)

    if file_ids:
        await _ask_for_filename(chat_id, user_id, file_ids, context.bot)
    else:
        await context.bot.send_message(chat_id=chat_id, text="No images received.")


async def _ask_for_filename_for_single(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       file_ids: List[str]) -> None:
    """Ask for filename immediately for a single image."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await _ask_for_filename(chat_id, user_id, file_ids, context.bot)


async def _ask_for_filename(chat_id: int, user_id: int, file_ids: List[str], bot) -> None:
    """Store file IDs and ask user for a filename."""
    pending_images[user_id] = {
        "file_ids": file_ids,
        "chat_id": chat_id,
        "timestamp": time.time(),
    }
    await bot.send_message(
        chat_id=chat_id,
        text="📝 Please send the desired filename (or /skip to use default)."
    )


# ----------------------------------------------------------------------
# Filename handling
# ----------------------------------------------------------------------
async def handle_text_for_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user text messages that are responses to filename prompt."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id not in pending_images:
        await update.message.reply_text("Send me an image to convert, or use /start for help.")
        return

    pending = pending_images.pop(user_id)
    if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
        await update.message.reply_text("⏰ Filename request expired. Please send the images again.")
        return

    filename = update.message.text.strip()
    # Sanitize filename
    filename = re.sub(r'[^\w\s.-]', '', filename, flags=re.UNICODE)
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = ' '.join(filename.split())
    if not filename:
        filename = "converted"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    if len(filename) > 100:
        filename = filename[:100]

    # Process images and send PDF
    await process_and_send_pdf(chat_id, user_id, pending["file_ids"], filename, context)


async def skip_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command to skip filename and use default."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id not in pending_images:
        await update.message.reply_text("No pending images to process.")
        return

    pending = pending_images.pop(user_id)
    if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
        await update.message.reply_text("⏰ Filename request expired. Please send the images again.")
        return

    await process_and_send_pdf(chat_id, user_id, pending["file_ids"], "converted.pdf", context)


async def process_and_send_pdf(chat_id: int, user_id: int, file_ids: List[str], filename: str, context) -> None:
    """Download, compress, create PDF, and send."""
    settings = get_user_settings(user_id)
    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Processing images...")
    try:
        start_time = time.time()
        # Download all files concurrently
        photo_bytes_list = await asyncio.gather(*(download_file(fid, context.bot) for fid in file_ids))
        # Process each image
        processed_images = []
        for i, img_bytes in enumerate(photo_bytes_list):
            await status_msg.edit_text(f"⏳ Processing image {i+1}/{len(file_ids)}...")
            img = await asyncio.to_thread(process_image_bytes, img_bytes, settings)
            if img is not None:
                processed_images.append(img)
        if not processed_images:
            await status_msg.edit_text("❌ No valid images found.")
            return
        # Create PDF with 1 MB limit
        pdf_bytes = await asyncio.to_thread(create_pdf_with_limit, processed_images, settings)
        await status_msg.delete()
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption="✅ Your PDF is ready! (≤ 1 MB)"
        )
        logger.info(f"Processed {len(processed_images)} images in {time.time()-start_time:.2f}s")
    except Exception as e:
        logger.error(f"Error processing images: {e}")
        await status_msg.edit_text("❌ Sorry, an error occurred while processing your images.")


# ----------------------------------------------------------------------
# Image processing functions (unchanged)
# ----------------------------------------------------------------------
def process_image_bytes(img_bytes: bytes, settings: Dict) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        if getattr(img, "is_animated", False):
            img.seek(0)
            img = img.convert("RGB")
        if img.width > settings["max_width"]:
            ratio = settings["max_width"] / img.width
            new_height = int(img.height * ratio)
            img = img.resize((settings["max_width"], new_height), Image.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=settings["quality"], optimize=True)
        return out_buf.getvalue()
    except Exception:
        return None


def create_pdf_with_limit(image_jpeg_list: List[bytes], settings: Dict) -> bytes:
    pdf_bytes = img2pdf.convert(image_jpeg_list, dpi=settings["dpi"])
    if len(pdf_bytes) <= MAX_PDF_SIZE:
        return pdf_bytes
    quality = settings["quality"]
    max_width = settings["max_width"]
    current_dpi = settings["dpi"]
    for _ in range(5):
        quality = max(20, int(quality * 0.8))
        max_width = max(800, int(max_width * 0.8))
        current_dpi = max(72, int(current_dpi * 0.8))
        re_processed = []
        for img_bytes in image_jpeg_list:
            img = Image.open(io.BytesIO(img_bytes))
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            if img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), Image.LANCZOS)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=quality, optimize=True)
            re_processed.append(out_buf.getvalue())
        pdf_bytes = img2pdf.convert(re_processed, dpi=current_dpi)
        if len(pdf_bytes) <= MAX_PDF_SIZE:
            return pdf_bytes
    raise ValueError("Cannot compress images enough to fit 1 MB limit.")


# ----------------------------------------------------------------------
# Error handler
# ----------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    error_str = str(context.error)
    recent_errors.append(error_str[:200])
    if update and isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(update.effective_chat.id, "❌ An unexpected error occurred. Please try again.")
        except:
            pass


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
application = None

def main() -> None:
    global application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("skip", skip_filename))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_for_filename))
    application.add_error_handler(error_handler)
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()