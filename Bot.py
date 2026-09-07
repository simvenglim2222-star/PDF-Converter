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

# Optional HEIC support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

# PDF rendering (PyMuPDF)
try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

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

# Hard limit for output file size (bytes)
MAX_OUTPUT_SIZE = 1 * 1024 * 1024  # 1 MB

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

# Pending images waiting for output format choice
pending_format: Dict[int, Dict] = {}
PENDING_TIMEOUT = 300  # 5 minutes

# Pending PDFs waiting for filename (after PDF format chosen)
pending_pdfs: Dict[int, Dict] = {}

# Pending PDF compression requests (waiting for filename)
pending_pdf_compress: Dict[int, Dict] = {}

# Recent errors for admin
recent_errors: Deque[str] = deque(maxlen=10)

# Semaphore to limit concurrent image processing (optional)
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
    """Short and clear welcome message."""
    user = update.effective_user
    first_name = user.first_name if user.first_name else "there"

    heic_note = "✅ HEIC/HEIF support enabled." if HEIC_SUPPORT else \
                "⚠️ HEIC/HEIF not supported. Install `pillow-heif`."
    pdf_note = "✅ PDF compression enabled." if PDF_SUPPORT else \
               "⚠️ PDF compression not available. Install `PyMuPDF`."

    welcome_text = (
        f"Hello, {first_name}!\n\n"
        "Welcome to AMT Scholarship Document Converter.\n\n"
        "• Send images → convert to PDF or JPEG (≤1 MB)\n"
        "• Send a PDF → compress it (≤1 MB)\n\n"
        "How to use:\n"
        "1. Send image(s) or PDF.\n"
        "2. Choose format if images (PDF/JPEG).\n"
        "3. Provide a filename if needed.\n\n"
        "Commands:\n"
        "/settings – adjust compression\n"
        "/cancel – cancel pending operation\n\n"
        f"{heic_note}\n"
        f"{pdf_note}\n"
        "Send your file to begin."
    )
    await update.message.reply_text(welcome_text)


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
    await update.message.reply_text(
        "🔧 Adjust your compression settings:",
        reply_markup=reply_markup
    )


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

    # Cancel pending format choice
    if user_id in pending_format:
        pending_format.pop(user_id, None)
        cancelled = True

    # Cancel pending filename (PDF from images)
    if user_id in pending_pdfs:
        pending_pdfs.pop(user_id, None)
        cancelled = True

    # Cancel pending PDF compression
    if user_id in pending_pdf_compress:
        pending_pdf_compress.pop(user_id, None)
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
    pending_format_users = len(pending_format)
    pending_filename_users = len(pending_pdfs)
    pending_pdf_compress_users = len(pending_pdf_compress)
    total_users_with_settings = len(user_settings)
    recent_errors_list = list(recent_errors)

    msg = (
        "📊 **AMT Scholarship Converter Status**\n\n"
        f"Active album tasks: {active_album_tasks}\n"
        f"Pending format choices: {pending_format_users}\n"
        f"Pending filename requests (PDF from images): {pending_filename_users}\n"
        f"Pending PDF compression requests: {pending_pdf_compress_users}\n"
        f"Users with custom settings: {total_users_with_settings}\n"
        f"Recent errors: {len(recent_errors_list)}\n"
    )
    if recent_errors_list:
        msg += "\n**Last errors:**\n"
        for err in recent_errors_list[-5:]:
            msg += f"- {err}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ----------------------------------------------------------------------
# Inline button handler (settings + format selection)
# ----------------------------------------------------------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # --- Format selection callbacks ---
    if data == "format_pdf" or data == "format_jpeg":
        if user_id not in pending_format:
            await query.edit_message_text("❌ No pending images to process.")
            return

        pending = pending_format.pop(user_id)
        if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
            await query.edit_message_text("⏰ Request expired. Please send the images again.")
            return

        file_ids = pending["file_ids"]
        chat_id = pending["chat_id"]

        if data == "format_pdf":
            await query.edit_message_text("✅ You chose PDF. Now processing...")
            await _ask_for_filename(chat_id, user_id, file_ids, context.bot)
        else:  # format_jpeg
            await query.edit_message_text("✅ You chose JPEG. Processing...")
            await process_jpeg_output(chat_id, user_id, file_ids, context)
        return

    # --- Settings callbacks ---
    s = get_user_settings(user_id)
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
    await query.edit_message_text(
        "🔧 Adjust your compression settings:",
        reply_markup=reply_markup
    )


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

    # Check if it's a PDF
    file_name = doc.file_name or ""
    mime_type = doc.mime_type or ""
    if file_name.lower().endswith(".pdf") or mime_type == "application/pdf":
        await _ask_pdf_compress_filename(update, context, doc)
    else:
        await _handle_media(update, context, doc.file_id)


async def _ask_pdf_compress_filename(update: Update, context: ContextTypes.DEFAULT_TYPE, doc) -> None:
    """Ask user for filename for the compressed PDF."""
    if not PDF_SUPPORT:
        await update.message.reply_text("❌ PDF compression is not available. Please install PyMuPDF.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    original_filename = doc.file_name or "document.pdf"

    pending_pdf_compress[user_id] = {
        "file_id": doc.file_id,
        "chat_id": chat_id,
        "timestamp": time.time(),
        "original_filename": original_filename,
    }

    await update.message.reply_text(
        "📝 Please provide a filename for the compressed PDF (or use /skip for default)."
    )


async def _handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in pending_format:
        await update.message.reply_text("ℹ️ You already have a pending operation. Please choose a format or /cancel.")

    message = update.message
    media_group_id = message.media_group_id

    if media_group_id:
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
        await _ask_for_format(chat_id, user_id, [file_id], context.bot)


async def _process_album_after_delay(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     media_group_id: str) -> None:
    await asyncio.sleep(3)
    chat_id = album_chat_id.get(media_group_id)
    if chat_id is None:
        return
    file_ids = album_photos.get(media_group_id, [])
    user_id = album_user_id.get(media_group_id)

    album_photos.pop(media_group_id, None)
    album_chat_id.pop(media_group_id, None)
    album_user_id.pop(media_group_id, None)
    album_tasks.pop(media_group_id, None)

    if file_ids:
        await _ask_for_format(chat_id, user_id, file_ids, context.bot)
    else:
        await context.bot.send_message(chat_id=chat_id, text="No images received.")


async def _ask_for_format(chat_id: int, user_id: int, file_ids: List[str], bot) -> None:
    pending_format[user_id] = {
        "file_ids": file_ids,
        "chat_id": chat_id,
        "timestamp": time.time(),
    }

    keyboard = [
        [
            InlineKeyboardButton("📄 PDF", callback_data="format_pdf"),
            InlineKeyboardButton("🖼️ JPEG", callback_data="format_jpeg"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(
        chat_id=chat_id,
        text="Please choose the required output format:",
        reply_markup=reply_markup
    )


# ----------------------------------------------------------------------
# Filename handling (for PDF from images)
# ----------------------------------------------------------------------
async def _ask_for_filename(chat_id: int, user_id: int, file_ids: List[str], bot) -> None:
    pending_pdfs[user_id] = {
        "file_ids": file_ids,
        "chat_id": chat_id,
        "timestamp": time.time(),
    }
    await bot.send_message(
        chat_id=chat_id,
        text="📝 Please provide a filename for the PDF (or use /skip for default)."
    )


async def handle_text_for_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check for PDF compression pending
    if user_id in pending_pdf_compress:
        pending = pending_pdf_compress.pop(user_id)
        if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
            await update.message.reply_text("⏰ Filename request expired. Please send the PDF again.")
            return

        filename = sanitize_filename(update.message.text.strip())
        await process_pdf_compression(
            chat_id, user_id, pending["file_id"],
            pending["original_filename"], filename, context
        )
        return

    # Check for image-to-PDF filename pending
    if user_id in pending_pdfs:
        pending = pending_pdfs.pop(user_id)
        if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
            await update.message.reply_text("⏰ Filename request expired. Please send the images again.")
            return

        filename = sanitize_filename(update.message.text.strip())
        await process_pdf_output(chat_id, user_id, pending["file_ids"], filename, context)
        return

    # If no pending filename request
    await update.message.reply_text("Send me an image to convert, or use /start for help.")


async def skip_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # PDF compression skip
    if user_id in pending_pdf_compress:
        pending = pending_pdf_compress.pop(user_id)
        if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
            await update.message.reply_text("⏰ Filename request expired. Please send the PDF again.")
            return
        base = os.path.splitext(pending["original_filename"])[0]
        filename = f"{base}_compressed.pdf"
        await process_pdf_compression(
            chat_id, user_id, pending["file_id"],
            pending["original_filename"], filename, context
        )
        return

    # Image-to-PDF skip
    if user_id in pending_pdfs:
        pending = pending_pdfs.pop(user_id)
        if time.time() - pending["timestamp"] > PENDING_TIMEOUT:
            await update.message.reply_text("⏰ Filename request expired. Please send the images again.")
            return
        await process_pdf_output(chat_id, user_id, pending["file_ids"], "converted.pdf", context)
        return

    await update.message.reply_text("No pending PDF to send.")


def sanitize_filename(text: str) -> str:
    """Clean user-provided filename."""
    filename = text.strip()
    filename = re.sub(r'[^\w\s.-]', '', filename, flags=re.UNICODE)
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = ' '.join(filename.split())
    if not filename:
        filename = "document"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    if len(filename) > 100:
        filename = filename[:100]
    return filename


# ----------------------------------------------------------------------
# Output processing
# ----------------------------------------------------------------------
async def process_pdf_output(chat_id: int, user_id: int, file_ids: List[str], filename: str, context) -> None:
    """Process images and create a PDF."""
    settings = get_user_settings(user_id)
    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Processing PDF...")
    try:
        start_time = time.time()
        photo_bytes_list = await asyncio.gather(*(download_file(fid, context.bot) for fid in file_ids))
        processed_images = []
        for i, img_bytes in enumerate(photo_bytes_list):
            await status_msg.edit_text(f"⏳ Processing image {i+1}/{len(file_ids)}...")
            img = await asyncio.to_thread(process_image_bytes, img_bytes, settings)
            if img is not None:
                processed_images.append(img)
        if not processed_images:
            await status_msg.edit_text("❌ No valid images found.")
            return
        pdf_bytes = await asyncio.to_thread(create_pdf_with_limit, processed_images, settings)
        await status_msg.delete()
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption="Thank you for using the AMT Scholarship Document Converter. Your PDF has been successfully prepared."
        )
        logger.info(f"PDF processed in {time.time()-start_time:.2f}s, {len(processed_images)} images")
    except Exception as e:
        logger.error(f"Error creating PDF: {e}")
        await status_msg.edit_text("❌ Sorry, an error occurred while processing your PDF.")


async def process_jpeg_output(chat_id: int, user_id: int, file_ids: List[str], context) -> None:
    """Process images and send as JPEG."""
    settings = get_user_settings(user_id)
    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Processing JPEG...")
    try:
        start_time = time.time()
        photo_bytes_list = await asyncio.gather(*(download_file(fid, context.bot) for fid in file_ids))
        for i, img_bytes in enumerate(photo_bytes_list):
            await status_msg.edit_text(f"⏳ Processing image {i+1}/{len(file_ids)}...")
            jpeg_bytes = await asyncio.to_thread(process_image_bytes, img_bytes, settings)
            if jpeg_bytes is None:
                continue
            jpeg_bytes = await asyncio.to_thread(ensure_jpeg_under_limit, jpeg_bytes, settings)
            filename = f"image_{i+1}.jpg"
            await context.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(jpeg_bytes),
                filename=filename,
                caption="Thank you for using the AMT Scholarship Document Converter. Your JPEG has been successfully prepared."
            )
        await status_msg.delete()
        logger.info(f"JPEG processed in {time.time()-start_time:.2f}s, {len(file_ids)} images")
    except Exception as e:
        logger.error(f"Error creating JPEG: {e}")
        await status_msg.edit_text("❌ Sorry, an error occurred while processing your images.")


async def process_pdf_compression(chat_id: int, user_id: int, file_id: str,
                                  original_filename: str, output_filename: str,
                                  context) -> None:
    """Download PDF, render pages to images, compress, and send as new PDF."""
    settings = get_user_settings(user_id)
    status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Compressing PDF...")
    try:
        start_time = time.time()
        pdf_bytes = await download_file(file_id, context.bot)

        # Render PDF pages to JPEG images
        jpeg_list = await asyncio.to_thread(render_pdf_to_jpegs, pdf_bytes, settings)

        if not jpeg_list:
            await status_msg.edit_text("❌ Could not process the PDF.")
            return

        # Create compressed PDF
        new_pdf_bytes = await asyncio.to_thread(create_pdf_with_limit, jpeg_list, settings)

        await status_msg.delete()
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(new_pdf_bytes),
            filename=output_filename,
            caption="Thank you for using the AMT Scholarship Document Converter. Your PDF has been compressed successfully."
        )
        logger.info(f"PDF compression completed in {time.time()-start_time:.2f}s")
    except Exception as e:
        logger.error(f"Error compressing PDF: {e}")
        await status_msg.edit_text("❌ Sorry, an error occurred while compressing the PDF.")


def render_pdf_to_jpegs(pdf_bytes: bytes, settings: Dict) -> List[bytes]:
    """Render each page of a PDF to a JPEG image (bytes)."""
    jpeg_list = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            dpi = settings.get("dpi", 150)
            zoom = dpi / 72
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=settings["quality"], optimize=True)
            jpeg_list.append(out_buf.getvalue())
        doc.close()
    except Exception as e:
        logger.error(f"Error rendering PDF: {e}")
    return jpeg_list


# ----------------------------------------------------------------------
# Image processing functions
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


def ensure_jpeg_under_limit(jpeg_bytes: bytes, settings: Dict) -> bytes:
    if len(jpeg_bytes) <= MAX_OUTPUT_SIZE:
        return jpeg_bytes
    quality = settings["quality"]
    max_width = settings["max_width"]
    for _ in range(5):
        quality = max(20, int(quality * 0.8))
        max_width = max(800, int(max_width * 0.8))
        img = Image.open(io.BytesIO(jpeg_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=quality, optimize=True)
        jpeg_bytes = out_buf.getvalue()
        if len(jpeg_bytes) <= MAX_OUTPUT_SIZE:
            return jpeg_bytes
    return jpeg_bytes


def create_pdf_with_limit(image_jpeg_list: List[bytes], settings: Dict) -> bytes:
    pdf_bytes = img2pdf.convert(image_jpeg_list, dpi=settings["dpi"])
    if len(pdf_bytes) <= MAX_OUTPUT_SIZE:
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
        if len(pdf_bytes) <= MAX_OUTPUT_SIZE:
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

    print("AMT Scholarship Document Converter is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
