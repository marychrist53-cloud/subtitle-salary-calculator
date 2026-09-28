#!/usr/bin/env python3
"""Ultimate Salary Calculator — Telegram bot.

You choose the destination ONCE per chat with two commands:

  /sheet  → NAS, PP, or LK   (which Google Sheet file files go to)
  /month  → e.g. 8.2026      (which monthly tab files go to)

These choices are sticky: every file submitted afterwards is logged to the same
sheet + month until you change them with the commands again.

Flow per file:
  1. Download + decode.
  2. Reject untranslated files (English-only / Chinese-only, no Burmese) and
     reply why; attempt to delete the message where Telegram allows it.
  3. Count lines (.srt / .ass / .txt).
  4. Read the rate tag: 20ks=20 and 25ks=25 for NAS; (v)=20, (p)=18,
     (mm)=22 and (old)=15 for PP.
  5. +1500 Ks review fee for p1/part1 of a movie or of a series' first episode
     (bookkept on NAS tables, which have the Review column).
  6. Log to the chosen sheet under the chosen month tab. A batch summary is
     sent a few seconds after the last file of a batch.

If a file arrives before /sheet or /month was ever chosen, the bot queues it
and asks with inline buttons.

Runs either with long polling (BOT_MODE=poll, no n8n needed) or as a webhook
endpoint that n8n forwards Telegram updates to (BOT_MODE=webhook).
"""

import asyncio
import logging
import os
from types import SimpleNamespace
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from logic import SUPPORTED_EXTENSIONS, analyze_file, decode_bytes, review_fee_for
from storage import read_json, write_json
from pending_store import load_pending_files, pending_chat_ids, save_pending_files
from sheets import SOURCE_RATES, SalarySheets, normalize_month

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def config_path(name, default):
    value = Path(os.environ.get(name) or default).expanduser()
    return value if value.is_absolute() else BASE_DIR / value


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SERVICE_ACCOUNT_PATH = str(config_path("GOOGLE_SERVICE_ACCOUNT_PATH", "sheets-service-account.json"))
IDS_FILE = str(config_path("SHEET_IDS_FILE", "sheet_ids.json"))
RECORD_METADATA_FILE = config_path("RECORD_METADATA_FILE", "record_metadata.json")
BOT_MODE = os.environ.get("BOT_MODE", "poll").lower()          # poll | webhook
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")                # e.g. https://abc.trycloudflare.com
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BATCH_QUIET_SECONDS = int(os.environ.get("BATCH_QUIET_SECONDS", "5"))

SETTINGS_FILE = config_path("SETTINGS_FILE", "bot_settings.json")
PENDING_FILES_FILE = config_path("PENDING_FILES_FILE", "pending_files.json")

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_PENDING_FILES = int(os.environ.get("MAX_PENDING_FILES", "100"))

BOT_COMMANDS = [
    BotCommand("start", "Start the bot & instructions"),
    BotCommand("help", "How the calculator works"),
    BotCommand("sheet", "Choose the sheet to log to (NAS, PP or LK)"),
    BotCommand("month", "Choose the month tab (e.g. 8.2026)"),
    BotCommand("status", "Show this chat's current sheet and month"),
    BotCommand("payslit", "Show totals for the selected sheet and month"),
    BotCommand("link", "Links to the NAS / PP / LK sheets"),
    BotCommand("reset", "Forget sheet & month for this chat"),
    BotCommand("resetmonth", "Alias for /reset"),
    BotCommand("resetyear", "Choose a different year for this month"),
    BotCommand("pending", "Show files waiting to be recorded"),
    BotCommand("retry", "Retry files waiting to be recorded"),
    BotCommand("undo", "Preview or confirm with /undo confirm"),
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("salary-bot")

sheets = SalarySheets(SERVICE_ACCOUNT_PATH, ids_file=IDS_FILE,
                      backup_dir=config_path("BACKUP_DIR", "backups"),
                      metadata_file=RECORD_METADATA_FILE)


# ─── Persistent per-chat settings (sheet + month) ───
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return read_json(SETTINGS_FILE)
    # Migrate the old months.json (month-only) if it exists.
    old = BASE_DIR / "months.json"
    if old.exists():
        migrated = {
            chat_id: {"month": month} for chat_id, month in
            read_json(old).items()
        }
        write_json(SETTINGS_FILE, migrated)
        return migrated
    return {}


def save_setting(chat_id: int, key: str, value):
    settings = load_settings()
    settings.setdefault(str(chat_id), {})[key] = value
    write_json(SETTINGS_FILE, settings)


def get_setting(chat_id: int, key: str):
    return load_settings().get(str(chat_id), {}).get(key)


def format_ks(n: int) -> str:
    return f"{n:,}"


async def send_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    """Send a reply, retrying once — a dropped send must never mean a
    silent file (one reply per submitted file, always)."""
    try:
        await context.bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        logger.warning("Send failed (%s), retrying once", exc)
        await asyncio.sleep(3)
        try:
            await context.bot.send_message(chat_id, text, **kwargs)
        except Exception as exc2:
            logger.error("Send retry also failed: %s", exc2)


def month_options() -> list[str]:
    """Previous, current and next month as "M.YYYY" strings."""
    today = date.today()
    options = []
    for d in (today.month - 1, today.month, today.month + 1):
        month = ((d - 1) % 12) + 1
        year = today.year + (d - 1) // 12
        options.append(f"{month}.{year}")
    return options


def year_options() -> list[str]:
    """Previous, current and next calendar year for the year chooser."""
    year = date.today().year
    return [str(value) for value in (year - 1, year, year + 1) if value > 0]


def normalize_year(raw: str) -> str | None:
    raw = raw.strip()
    if len(raw) != 4 or not raw.isascii() or not raw.isdigit() or int(raw) == 0:
        return None
    return raw


# ─── Choosers (/sheet and /month) ───
async def ask_sheet(update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("NAS (20ks / 25ks)", callback_data="sheet:NAS"),
            InlineKeyboardButton("PP ((v) / (p) / (mm) / (old))", callback_data="sheet:PP"),
        ],
        [InlineKeyboardButton("LK (filename and line count)", callback_data="sheet:LK")],
    ]
    text = "🗂 Which sheet should the files be logged to?"
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.send_message(
            chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def ask_month(update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    keyboard = [
        [InlineKeyboardButton(m, callback_data=f"month:{m}") for m in month_options()],
        [InlineKeyboardButton("✍️ Other…", callback_data="month:other")],
    ]
    text = (
        "📅 Which month should the salary be logged to?\n"
        "Each month gets its own tab in the sheet (e.g. 8.2026)."
    )
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.send_message(
            chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def ask_year(update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, month: str):
    keyboard = [
        [InlineKeyboardButton(year, callback_data=f"year:{year}") for year in year_options()],
        [InlineKeyboardButton("✍️ Other…", callback_data="year:other")],
    ]
    month_number = int(month.split(".", 1)[0])
    text = (
        f"📅 Choose the year for month {month_number}. The selected month and sheet will stay the same."
    )
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.send_message(
            chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def sheet_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, sheet: str):
    chat_id = update.effective_chat.id
    save_setting(chat_id, "sheet", sheet)
    await context.bot.send_message(
        chat_id,
        f"🗂 Sheet: {sheet}. All following files will be logged there "
        f"(change with /sheet).",
    )
    await continue_pending(context, chat_id)


async def month_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, month: str):
    chat_id = update.effective_chat.id
    save_setting(chat_id, "month", month)
    context.chat_data.pop("awaiting_month_user", None)
    await context.bot.send_message(
        chat_id,
        f"📅 Month: {month}. All following files will be logged there "
        f"(change with /month).",
    )
    await continue_pending(context, chat_id)


async def continue_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Attempt each saved item once, preserving failures and their original destination."""
    pending = load_pending_files(PENDING_FILES_FILE, chat_id)
    sheet, month = get_setting(chat_id, "sheet"), get_setting(chat_id, "month")
    for item in list(pending):
        if not item.get("sheet") or not item.get("month"):
            if not (sheet and month):
                continue
            item["sheet"], item["month"] = sheet, month
        item.setdefault("record_id", uuid4().hex)
        item.setdefault("submitted_at", datetime.now(timezone.utc).isoformat())
        item.setdefault("chat_id", str(chat_id))
        save_pending_files(PENDING_FILES_FILE, chat_id, pending)
        try:
            entry = analyze_file(item["text"], item["file_name"], destination=item["sheet"])
            entry["file_name"] = item["file_name"]
            for key in ("record_id", "submitted_at", "submitted_by", "user_id", "chat_id"):
                if key in item:
                    entry[key] = item[key]
            apply_destination(entry, item["sheet"])
            completed = await report_result(context, chat_id, entry, item["month"])
        except Exception:
            logger.exception("Queued file processing failed")
            completed = False
            await send_reply(context, chat_id, f"Could not record {item.get('file_name', 'file')}. Saved for /retry.")
        if completed:
            pending.remove(item)
        else:
            item["attempts"] = item.get("attempts", 0) + 1
        save_pending_files(PENDING_FILES_FILE, chat_id, pending)
    if any(not item.get("sheet") or not item.get("month") for item in pending):
        if not sheet:
            await ask_sheet(None, context, chat_id)
        elif not month:
            await ask_month(None, context, chat_id)


def apply_destination(entry: dict, sheet: str):
    """Enforce that filename rate tags are logged to their designated sheet."""
    if entry["status"] != "ok":
        return
    if entry["source"] != sheet:
        entry["status"] = "error"
        entry["reason"] = (
            f"The {entry['tag']} rate tag belongs to {entry['source']}, but this chat is set to {sheet}. "
            f"Use /sheet to choose {entry['source']} and resend the file."
        )
        return
    entry["review_fee"] = review_fee_for(entry["file_name"]) if entry["source"] == "NAS" else 0
    entry["total_fee"] = entry["base_fee"] + entry["review_fee"]


# ─── Result reporting ───
async def report_result(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    entry: dict,
    month: str | None,
    deleted_note: str = "",
):
    previous_job = context.chat_data.pop("summary_job", None)
    if previous_job:
        try:
            previous_job.schedule_removal()
        except Exception:
            pass
    batch = context.chat_data.get("batch")
    if not batch:
        batch = {"logged": 0, "removed": 0, "errors": 0, "dupes": 0,
                 "nas": 0, "pp": 0, "lk_lines": 0, "total": 0}
        context.chat_data["batch"] = batch
    batch["total"] += 1

    if entry["status"] == "rejected":
        batch["removed"] += 1
        await send_reply(
            context,
            chat_id,
            f"🗑 Removed: {entry.get('file_name', 'file')}\n"
            f"Reason: {entry['reason']}\n"
            f"Nothing was logged.{deleted_note}",
        )
        schedule_batch_summary(context, chat_id)
        return True

    if entry["status"] == "error":
        batch["errors"] += 1
        await send_reply(
            context, chat_id,
            f"❌ Skipped: {entry.get('file_name', 'file')}\nReason: {entry['reason']}",
        )
        schedule_batch_summary(context, chat_id)
        return True

    # status == "ok"
    try:
        result = await asyncio.to_thread(sheets.record, entry, month)
    except Exception:
        logger.exception("Google Sheets record failed")
        batch["errors"] += 1
        await send_reply(context, chat_id, f"⚠️ {entry['file_name']} could not be recorded. It is saved for /retry.")
        schedule_batch_summary(context, chat_id)
        return False
    if result.get("duplicate"):
        batch["dupes"] += 1
        await send_reply(
            context,
            chat_id,
            f"⚠️ Duplicate: {entry['file_name']}\n"
            f"This exact file name is already logged in Salary {entry['source']} → "
            f"tab {month}. Nothing was added again.",
        )
        schedule_batch_summary(context, chat_id)
        return True

    batch["logged"] += 1
    if entry["source"] == "LK":
        batch["lk_lines"] = batch.get("lk_lines", 0) + entry["lines"]
        await send_reply(context, chat_id,
                         f"✅ {entry['file_name']}\n"
                         f"📏 Lines: {format_ks(entry['lines'])}\n"
                         f"📅 Logged to Salary LK → tab {month}")
        schedule_batch_summary(context, chat_id)
        return True
    batch[entry["source"].lower()] += entry["total_fee"]

    lines = [
        f"✅ {entry['file_name']}",
        f"📏 Lines: {format_ks(entry['lines'])}",
        f"🏷 Tag: {entry['tag']} ({entry['rate']} Ks/line)",
        f"🗂 Sheet: {entry['source']}",
        f"🧮 Base: {format_ks(entry['base_fee'])} Ks",
    ]
    if entry["review_fee"]:
        lines.append(f"⭐ Review: +{format_ks(entry['review_fee'])} Ks")
    lines += [
        f"💵 Total: {format_ks(entry['total_fee'])} Ks",
        f"📅 Logged to Salary {entry['source']} → tab {month}",
    ]
    await send_reply(context, chat_id, "\n".join(lines))
    schedule_batch_summary(context, chat_id)
    return True


def schedule_batch_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Send one summary a few seconds after the last file of a batch."""
    existing = context.chat_data.get("summary_job")
    if existing:
        try:
            existing.schedule_removal()
        except Exception:
            # The job already ran and was removed by the scheduler — fine.
            pass
    context.chat_data["summary_job"] = context.job_queue.run_once(
        batch_summary_job, BATCH_QUIET_SECONDS, chat_id=chat_id, name="batch_summary"
    )


async def batch_summary_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    batch = context.chat_data.get("batch") or {}
    if not batch or batch.get("total", 0) == 0:
        return

    parts = [f"📦 Batch done — {batch['total']} file(s)"]
    details = []
    if batch.get("logged"):
        details.append(f"✅ Logged: {batch['logged']}")
    if batch.get("removed"):
        details.append(f"🗑 Removed: {batch['removed']}")
    if batch.get("errors"):
        details.append(f"❌ Errors: {batch['errors']}")
    if batch.get("dupes"):
        details.append(f"🔁 Duplicates: {batch['dupes']}")
    parts.append(" | ".join(details))

    totals = []
    if batch.get("nas"):
        totals.append(f"NAS {format_ks(batch['nas'])} Ks")
    if batch.get("pp"):
        totals.append(f"PP {format_ks(batch['pp'])} Ks")
    if totals:
        parts.append("💰 " + " | ".join(totals))
    if batch.get("lk_lines"):
        parts.append(f"📏 LK: {format_ks(batch['lk_lines'])} lines")

    context.chat_data["batch"] = None
    await send_reply(context, chat_id, "\n".join(parts))


# ─── Commands ───
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet = get_setting(update.effective_chat.id, "sheet")
    month = get_setting(update.effective_chat.id, "month")
    state = f"Current: sheet {sheet or '—'}, month {month or '—'}"
    await update.message.reply_text(
        "👋 Send me subtitle files (.srt, .ass, .txt) — one at a time or a batch.\n\n"
        "First set where files go:\n"
        "• /sheet — pick NAS, PP, or LK\n"
        "• /month — pick the month tab (e.g. 8.2026)\n\n"
        "Use /resetyear to choose a different year while keeping the month and sheet.\n\n"
        "Use /payslit to see the current month's totals.\n\n"
        "Then every file is logged to that same sheet + month until you change it.\n\n"
        "For each file I will:\n"
        "1. Remove untranslated files (English-only / Chinese-only) and tell you why\n"
        "2. Count the lines\n"
        "3. Read the rate tag at the end of the filename:\n"
        "      NAS: 20ks=20 • 25ks=25; PP: (v)=20 • (p)=18 • (mm)=22 • (old)=15 (Ks per line)\n"
        "4. NAS only: add +1500 Ks review fee for p1/part1 of a movie or a series' first episode\n"
        "LK records filenames and line counts only; no rate tag is needed.\n\n"
        f"{state}\n"
        "Use /help for details."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Destination (sticky until changed):\n"
        "• /sheet — NAS, PP, or LK\n"
        "• /month — month tab, e.g. 8.2026\n"
        "• /status — show the current sheet and month\n"
        "• /payslit — show salary, review fees, lines, and file count\n"
        "• /resetyear — choose a different year, keeping the selected month and sheet\n"
        "• /reset — forget both; the bot asks again on the next file\n\n"
        "Rate tags determine which sheet accepts a file:\n"
        "NAS: 20ks → 20 Ks/line; 25ks → 25 Ks/line\n"
        "PP: (v) → 20 Ks/line; (p) → 18 Ks/line; (mm) → 22 Ks/line; (old) → 15 Ks/line\n"
        "Choose the matching sheet with /sheet.\n\n"
        "LK accepts filenames without a rate tag and records only filenames and line counts. "
        "LK filenames are sorted alphabetically. Names containing fix/fixed/check/checked "
        "are highlighted in yellow, ignoring case.\n\n"
        "Review fee: +1500 Ks when the filename has an explicit p1/part1 marker for a "
        "movie, or for a series' first episode (E1/E01/EP1/EP01, S02E01, episode 1, "
        "1-1 — any season; spaces in episode markers are allowed). EP1 alone is not a p1 marker.\n"
        "Review fees are tracked on the NAS sheet only; PP has no review fee.\n\n"
        "Untranslated files (English-only or Chinese-only, no Burmese) are removed and not logged.\n"
        "Files with the exact same name already logged in the same sheet + month are rejected as duplicates.\n\n"
        "Commands:\n"
        "/sheet — choose NAS, PP, or LK\n"
        "/month — choose the month\n"
        "/status — show the current sheet and month\n"
        "/payslit — show current totals\n"
        "/resetyear — choose a different year for the selected month\n"
        "/reset — forget sheet & month\n"
        "/pending — list saved files\n"
        "/retry — retry saved files\n"
        "/undo — preview removing your latest record"
    )


async def sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_sheet(update, context, update.effective_chat.id)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sheet = get_setting(chat_id, "sheet")
    month = get_setting(chat_id, "month")
    await update.effective_message.reply_text(
        f"Current settings for this chat:\n"
        f"Sheet: {sheet or 'not set'}\n"
        f"Month: {month or 'not set'}"
    )


async def payslit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    source = get_setting(chat_id, "sheet")
    month = get_setting(chat_id, "month")
    if source not in SOURCE_RATES or not month:
        await update.effective_message.reply_text(
            "Set your destination with /sheet and month with /month first."
        )
        return
    try:
        summary = await asyncio.to_thread(sheets.month_summary, source, month)
    except Exception:
        logger.exception("Failed reading monthly payslip summary for %s %s", source, month)
        await update.effective_message.reply_text(
            f"Could not read Salary {source} for {month}. Check sheet access and try again."
        )
        return
    if source == "LK":
        await update.effective_message.reply_text(
            f"📊 Salary LK — {month}\n"
            f"Total lines: {format_ks(summary['line_count'])}\n"
            f"File count: {format_ks(summary['file_count'])}"
        )
        return
    await update.effective_message.reply_text(
        f"📊 Salary {source} — {month}\n"
        f"Overall total: {format_ks(summary['overall_total'])} Ks\n"
        f"Review files: {summary['review_count']}\n"
        f"Review total: {format_ks(summary['review_total'])} Ks\n"
        f"Total lines: {format_ks(summary['line_count'])}\n"
        f"File count: {format_ks(summary['file_count'])}"
    )


async def month_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_month(update, context, update.effective_chat.id)


async def resetyear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    month = normalize_month(get_setting(chat_id, "month") or "")
    if month is None:
        await update.effective_message.reply_text(
            "Set a month with /month first. Then /resetyear can change its year while keeping the month and sheet."
        )
        return
    context.chat_data.pop("awaiting_month_user", None)
    await ask_year(update, context, chat_id, month)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("awaiting_month_user", None)
    context.chat_data.pop("awaiting_year_user", None)
    settings = load_settings()
    settings.pop(str(update.effective_chat.id), None)
    write_json(SETTINGS_FILE, settings)
    await update.message.reply_text(
        "🧹 Sheet & month cleared. I'll ask for both on the next file."
    )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create (if needed) and link all three spreadsheets."""
    parts = ["🔗 Salary sheets:"]
    for source in SOURCE_RATES:
        try:
            url = await asyncio.to_thread(sheets.sheet_url, source)
            parts.append(f"• Salary {source}: {url}")
        except Exception as exc:
            logger.error("/link failed for %s: %s", source, exc)
            parts.append(
                f"• Salary {source}: ❌ {exc}\n"
                f"  If this is a quota error: create the sheet yourself in your "
                f"Drive folder, share it (Editor) with the service-account email, "
                f"and put its ID in .env as "
                f"{source}_SHEET_ID."
            )
    await update.message.reply_text("\n".join(parts), disable_web_page_preview=True)


# ─── Callback & text handlers ───
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = load_pending_files(PENDING_FILES_FILE, update.effective_chat.id)
    lines = [f"Saved files: {len(pending)}"]
    for item in pending[:20]:
        target = f"{item.get('sheet') or '?'} / {item.get('month') or '?'}"
        lines.append(f"• {item['file_name'][:100]} → {target} (failed attempts: {item.get('attempts', 0)})")
    if len(pending) > 20:
        lines.append(f"…and {len(pending) - 20} more.")
    lines.append("Use /retry to try again. Set /sheet and /month for files with no destination.")
    await update.message.reply_text("\n".join(lines))


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not load_pending_files(PENDING_FILES_FILE, chat_id):
        await update.message.reply_text("No saved files are waiting.")
        return
    await continue_pending(context, chat_id)


async def year_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, year: str):
    chat_id = update.effective_chat.id
    month = normalize_month(get_setting(chat_id, "month") or "")
    if month is None:
        context.chat_data.pop("awaiting_year_user", None)
        await context.bot.send_message(chat_id, "Set a month with /month before changing its year.")
        return
    month_number = int(month.split(".", 1)[0])
    new_month = f"{month_number}.{year}"
    save_setting(chat_id, "month", new_month)
    context.chat_data.pop("awaiting_year_user", None)
    context.chat_data.pop("awaiting_month_user", None)
    await context.bot.send_message(
        chat_id,
        f"📅 Year changed to {year}. Selected period is now {new_month}; sheet unchanged.",
    )
    await continue_pending(context, chat_id)


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    key = f"undo:{chat_id}"
    if context.args and context.args != ["confirm"]:
        await update.message.reply_text("Use /undo to preview, then /undo confirm within two minutes.")
        return
    if context.args == ["confirm"]:
        target = context.user_data.get(key)
        if not target or datetime.now(timezone.utc).timestamp() > target["expires"]:
            await update.message.reply_text("Use /undo again to preview the record first.")
            return
        if any(item.get("record_id") == target["record_id"] for item in load_pending_files(PENDING_FILES_FILE, chat_id)):
            await update.message.reply_text("Use /retry first to reconcile the saved queue, then /undo again.")
            return
        try:
            removed = await asyncio.to_thread(sheets.undo_record, target["sheet"], target["month"],
                                             target["record_id"], user_id, chat_id)
        except Exception:
            logger.exception("Undo failed")
            await update.message.reply_text("Could not confirm the correction. Try /undo confirm again.")
            return
        context.user_data.pop(key, None)
        await update.message.reply_text("Record removed and totals updated." if removed else "That record is already absent.")
        return
    sheet, month = get_setting(chat_id, "sheet"), get_setting(chat_id, "month")
    if not (sheet and month):
        await update.message.reply_text("Choose /sheet and /month first.")
        return
    try:
        row = await asyncio.to_thread(sheets.latest_record, sheet, month, user_id, chat_id)
    except Exception:
        logger.exception("Undo preview failed")
        await update.message.reply_text("Could not read the sheet. Try /undo later.")
        return
    if not row:
        await update.message.reply_text("No recorded uploads with your user ID were found in this sheet and month.")
        return
    context.user_data[key] = {"record_id": row["record_id"], "sheet": sheet, "month": month,
                              "expires": datetime.now(timezone.utc).timestamp() + 120}
    detail = (f"Lines: {format_ks(row['lines'])}.\n" if sheet == "LK"
              else f"Amount: {format_ks(row['total'] + row['review'])} Ks.\n")
    await update.message.reply_text(
        f"Remove {row['file_name']} from {sheet} / {month}?\n"
        + detail +
        "Send /undo confirm within two minutes to apply this correction.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "month:other":
        context.chat_data["awaiting_month_user"] = update.effective_user.id
        await query.edit_message_text(
            "✍️ Send the month as M.YYYY (for example 8.2026)."
        )
        return

    if data.startswith("month:"):
        month = normalize_month(data.removeprefix("month:"))
        if month is None:
            await ask_month(update, context, update.effective_chat.id)
            return
        await query.edit_message_text(f"📅 Month: {month}")
        await month_chosen(update, context, month)
        return

    if data == "year:other":
        context.chat_data["awaiting_year_user"] = update.effective_user.id
        await query.edit_message_text("✍️ Send the four-digit year to use (for example 2027).")
        return

    if data.startswith("year:"):
        year = normalize_year(data.removeprefix("year:"))
        month = normalize_month(get_setting(update.effective_chat.id, "month") or "")
        if year is None or month is None:
            if month is None:
                await query.edit_message_text("Set a month with /month before changing its year.")
            else:
                await ask_year(update, context, update.effective_chat.id, month)
            return
        await query.edit_message_text(f"📅 Changing the selected month to year {year}.")
        await year_chosen(update, context, year)
        return

    if data.startswith("sheet:"):
        sheet = data.removeprefix("sheet:")
        if sheet not in SOURCE_RATES:
            await ask_sheet(update, context, update.effective_chat.id)
            return
        await query.edit_message_text(f"🗂 Sheet: {sheet}")
        await sheet_chosen(update, context, sheet)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.chat_data.get("awaiting_year_user") == update.effective_user.id:
        year = normalize_year(update.message.text or "")
        if year is None:
            await update.message.reply_text("❌ Send a valid four-digit year, e.g. 2027.")
            return
        await year_chosen(update, context, year)
        return

    if context.chat_data.get("awaiting_month_user") != update.effective_user.id:
        await start_command(update, context)
        return

    month = normalize_month(update.message.text or "")
    if month is None:
        await update.message.reply_text("❌ That doesn't look like a month. Send it as M.YYYY, e.g. 8.2026.")
        return

    await update.message.reply_text(f"📅 Month: {month}")
    await month_chosen(update, context, month)


# ─── Document handler ───
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document:
        return

    document = message.document
    chat_id = message.chat.id
    file_name = document.file_name or "unknown_file"

    # Catch-all: every submitted file gets a reply, even if processing crashes.
    try:
        await _process_document(update, context, message, chat_id, document, file_name)
    except Exception:
        logger.exception("Processing failed for %s", file_name)
        await send_reply(
            context,
            chat_id,
            f"❌ Could not process: {file_name}\nCheck the file encoding and size, then resend. Use /pending to check saved uploads.",
        )
        batch = context.chat_data.get("batch")
        if batch:
            batch["errors"] += 1
        schedule_batch_summary(context, chat_id)


async def _process_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message,
    chat_id: int,
    document,
    file_name: str,
):
    if Path(file_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        entry = analyze_file("", file_name)
        entry["file_name"] = file_name
        await report_result(context, chat_id, entry, month=None)
        return

    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        await report_result(context, chat_id, {"status": "error", "file_name": file_name,
            "reason": f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."}, None)
        return
    if len(load_pending_files(PENDING_FILES_FILE, chat_id)) >= MAX_PENDING_FILES:
        await send_reply(context, chat_id, "The saved queue is full. Use /pending and /retry first.")
        return
    new_file = await context.bot.get_file(document.file_id)
    file_bytes = await new_file.download_as_bytearray()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the upload size limit")
    text = decode_bytes(file_bytes)

    sheet = get_setting(chat_id, "sheet")
    month = get_setting(chat_id, "month")
    # With no destination yet, save untagged files so the user can choose LK.
    entry = analyze_file(text, file_name, destination=sheet, require_rate=sheet is not None)
    entry.setdefault("file_name", file_name)

    # Try to remove untranslated files from the chat (works in groups where the
    # bot is admin; in private chats Telegram doesn't allow deleting user messages).
    deleted_note = ""
    if entry["status"] == "rejected" and message.chat.type != "private":
        try:
            await message.delete()
            deleted_note = "\n(Message deleted from this chat.)"
        except Exception:
            pass

    # Rejected / no-rate-tag files are reported immediately — no sheet needed.
    if entry["status"] != "ok":
        await report_result(context, chat_id, entry, month=None, deleted_note=deleted_note)
        return

    pending = load_pending_files(PENDING_FILES_FILE, chat_id)
    message_id = str(message.message_id)
    if not any(item.get("message_id") == message_id for item in pending):
        user = update.effective_user
        pending.append({
            "text": text, "file_name": file_name, "sheet": sheet, "month": month,
            "record_id": f"{chat_id}:{message_id}", "message_id": message_id,
            "chat_id": str(chat_id), "user_id": str(user.id) if user else "",
            "submitted_by": user.full_name if user else "Unknown",
            "submitted_at": message.date.astimezone(timezone.utc).isoformat(),
        })
        save_pending_files(PENDING_FILES_FILE, chat_id, pending)
    await continue_pending(context, chat_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log unhandled errors instead of dumping raw tracebacks."""
    logger.error("Unhandled error while processing update: %s", context.error)


# ─── Main ───
async def post_init(app: Application):
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Command menu registration failed; continuing startup")
    try:
        chat_ids = pending_chat_ids(PENDING_FILES_FILE)
    except Exception:
        logger.exception("Cannot read pending store; restore its backup before retrying")
        return
    for chat_id in chat_ids:
        context = SimpleNamespace(bot=app.bot, job_queue=app.job_queue, chat_data=app.chat_data[chat_id])
        try:
            await continue_pending(context, chat_id)
        except Exception:
            logger.exception("Queue recovery failed for chat %s; continuing startup", chat_id)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in ultimate-bot/.env")
    if BOT_MODE not in {"poll", "webhook", "bridge"}:
        raise SystemExit("BOT_MODE must be poll, webhook, or bridge")
    if MAX_UPLOAD_BYTES <= 0 or MAX_PENDING_FILES <= 0 or BATCH_QUIET_SECONDS < 1:
        raise SystemExit("Upload, queue, and batch limits must be positive")
    if BOT_MODE in {"webhook", "bridge"}:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
            raise SystemExit("Set WEBHOOK_SECRET to 16–256 letters, digits, underscores or hyphens")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("sheet", sheet_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("payslit", payslit_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("resetmonth", reset_command))  # old name
    app.add_handler(CommandHandler("resetyear", resetyear_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(month|year|sheet):"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    if BOT_MODE == "bridge":
        from bridge import run_bridge
        try:
            asyncio.run(run_bridge(app, WEBHOOK_PORT, WEBHOOK_SECRET, post_init))
        except KeyboardInterrupt:
            pass
    elif BOT_MODE == "webhook":
        if not WEBHOOK_URL:
            raise SystemExit("BOT_MODE=webhook requires WEBHOOK_URL in .env")
        url_path = WEBHOOK_SECRET.lstrip("/")
        print(f"Webhook mode — listening on port {WEBHOOK_PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=url_path,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{url_path}",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        )
    else:
        print("🤖 Polling mode — bot started")
        app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
