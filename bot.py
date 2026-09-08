import os
import re
import sqlite3
import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

from telegram import Update, ReactionTypeEmoji
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("admin-bot")

# ---------------------------------------------------------------------------
# Config (all via environment variables — see README.md)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
MILEAGE_THREAD_ID = int(os.environ["MILEAGE_THREAD_ID"])
JY_THREAD_ID = int(os.environ["JY_THREAD_ID"])
HYX_THREAD_ID = int(os.environ["HYX_THREAD_ID"])

TZ = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Singapore"))
DB_PATH = os.environ.get("DB_PATH", "/data/admin_bot.db")

MILEAGE_REMINDER_TIME = dtime(17, 30, tzinfo=TZ)   # 5:30pm daily from the 3rd
TRAVEL_CHECK_TIME = dtime(8, 0, tzinfo=TZ)          # when to check "is it 1st Saturday"
TRAVEL_REMINDER_TIME = dtime(9, 0, tzinfo=TZ)       # daily "claim your transport" nag

CLAIM_KEYWORD_RE = re.compile(r"\bclaim(ed)?\b", re.IGNORECASE)

TRAVEL_GROUPS = {
    "JY": JY_THREAD_ID,
    "HYX": HYX_THREAD_ID,
}

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS mileage (
        year_month TEXT PRIMARY KEY,
        text TEXT,
        submitted_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS travel_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grp TEXT,
        year_month TEXT,
        day INTEGER,
        text TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS travel_status (
        grp TEXT,
        year_month TEXT,
        summary_sent INTEGER DEFAULT 0,
        claimed INTEGER DEFAULT 0,
        summary_text TEXT,
        PRIMARY KEY (grp, year_month)
    )""")
    return conn


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def is_mileage_submitted(d: date) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM mileage WHERE year_month=?", (ym(d),)).fetchone()
    conn.close()
    return row is not None


def record_mileage(d: date, text: str):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO mileage (year_month, text, submitted_at) VALUES (?, ?, ?)",
        (ym(d), text, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def add_travel_entry(grp: str, d: date, text: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO travel_log (grp, year_month, day, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (grp, ym(d), d.day, text, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def get_travel_status(grp: str, year_month: str):
    conn = get_db()
    row = conn.execute(
        "SELECT summary_sent, claimed, summary_text FROM travel_status WHERE grp=? AND year_month=?",
        (grp, year_month),
    ).fetchone()
    conn.close()
    return row


def set_travel_summary_sent(grp: str, year_month: str, summary_text: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO travel_status (grp, year_month, summary_sent, claimed, summary_text)
           VALUES (?, ?, 1, 0, ?)
           ON CONFLICT(grp, year_month) DO UPDATE SET summary_sent=1, summary_text=excluded.summary_text""",
        (grp, year_month, summary_text),
    )
    conn.commit()
    conn.close()


def set_travel_claimed(grp: str, year_month: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO travel_status (grp, year_month, summary_sent, claimed)
           VALUES (?, ?, 1, 1)
           ON CONFLICT(grp, year_month) DO UPDATE SET claimed=1""",
        (grp, year_month),
    )
    conn.commit()
    conn.close()


def get_travel_entries(grp: str, year_month: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT day, text FROM travel_log WHERE grp=? AND year_month=? ORDER BY day, id",
        (grp, year_month),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_travel_summary(grp: str, year_month: str):
    entries = get_travel_entries(grp, year_month)
    if not entries:
        return None
    y, m = year_month.split("-")
    month_name = MONTH_NAMES[int(m) - 1]
    lines = ["CLAIM YO TRANSPORT!!", "", month_name]
    for day, text in entries:
        lines.append(f"{day:02d} {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def first_saturday(year: int, month: int) -> date:
    d = date(year, month, 1)
    offset = (5 - d.weekday()) % 7  # Monday=0 ... Saturday=5
    return date(year, month, 1 + offset)


def prev_month(d: date):
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1


# ---------------------------------------------------------------------------
# Incoming messages
# ---------------------------------------------------------------------------

async def react_or_reply(context: ContextTypes.DEFAULT_TYPE, msg, fallback_text: str):
    try:
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji="\U0001F44D")],
        )
    except Exception:
        try:
            await msg.reply_text(fallback_text)
        except Exception:
            log.exception("Failed to ack message")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID or not msg.text:
        return

    thread_id = msg.message_thread_id
    text = msg.text.strip()
    today = datetime.now(TZ).date()

    # --- Mileage topic: any message here IS this month's mileage figure ---
    if thread_id == MILEAGE_THREAD_ID:
        record_mileage(today, text)
        await react_or_reply(context, msg, "Mileage recorded for this month, thanks!")
        return

    # --- Travel topics ---
    for grp, tid in TRAVEL_GROUPS.items():
        if thread_id == tid:
            if CLAIM_KEYWORD_RE.search(text):
                y, m = prev_month(today)
                set_travel_claimed(grp, f"{y:04d}-{m:02d}")
                await react_or_reply(context, msg, f"Marked {grp} transport as claimed.")
            else:
                add_travel_entry(grp, today, text)
                await react_or_reply(context, msg, "Logged.")
            return


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_mileage_reminder(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    if today.day < 3:
        return
    if is_mileage_submitted(today):
        return
    await context.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=MILEAGE_THREAD_ID,
        text="UPLOAD YOUR MILEAGE!!",
    )


async def job_travel_summary_check(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    if today != first_saturday(today.year, today.month):
        return
    py, pm = prev_month(today)
    year_month = f"{py:04d}-{pm:02d}"
    for grp in TRAVEL_GROUPS:
        status = get_travel_status(grp, year_month)
        if status and status[0]:
            continue  # already sent
        summary = format_travel_summary(grp, year_month)
        if not summary:
            continue
        await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=TRAVEL_GROUPS[grp],
            text=summary,
        )
        set_travel_summary_sent(grp, year_month, summary)


async def job_travel_claim_reminder(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    py, pm = prev_month(today)
    year_month = f"{py:04d}-{pm:02d}"
    for grp in TRAVEL_GROUPS:
        status = get_travel_status(grp, year_month)
        if not status:
            continue
        summary_sent, claimed, summary_text = status
        if not summary_sent or claimed:
            continue
        text = summary_text or format_travel_summary(grp, year_month) or "CLAIM YO TRANSPORT!!"
        await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=TRAVEL_GROUPS[grp],
            text=text,
        )


# ---------------------------------------------------------------------------
# Setup / query commands
# ---------------------------------------------------------------------------

async def cmd_topicid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(
        f"chat_id: {msg.chat_id}\nmessage_thread_id: {msg.message_thread_id}"
    )


def mileage_summary_text(d: date) -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT text FROM mileage WHERE year_month=?", (ym(d),)
    ).fetchone()
    conn.close()
    if row:
        return f"Mileage recorded this month: {row[0]}"
    return "No mileage recorded yet this month."


def travel_summary_text(grp: str, d: date) -> str:
    entries = get_travel_entries(grp, ym(d))
    if not entries:
        return f"{grp}: no trips logged yet this month."
    lines = [f"{grp} trips logged so far this month:"]
    for day, text in entries:
        lines.append(f"{day:02d} {text}")
    return "\n".join(lines)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return
    thread_id = msg.message_thread_id
    today = datetime.now(TZ).date()

    if thread_id == MILEAGE_THREAD_ID:
        text = mileage_summary_text(today)
    else:
        grp = next((g for g, tid in TRAVEL_GROUPS.items() if tid == thread_id), None)
        if grp:
            text = travel_summary_text(grp, today)
        else:
            # asked outside a recognised topic — give everything
            parts = [mileage_summary_text(today)]
            parts.extend(travel_summary_text(g, today) for g in TRAVEL_GROUPS)
            text = "\n\n".join(parts)

    await msg.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Admin bot is running.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topicid", cmd_topicid))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    jq = app.job_queue
    jq.run_daily(job_mileage_reminder, time=MILEAGE_REMINDER_TIME, name="mileage_reminder")
    jq.run_daily(job_travel_summary_check, time=TRAVEL_CHECK_TIME, name="travel_summary_check")
    jq.run_daily(job_travel_claim_reminder, time=TRAVEL_REMINDER_TIME, name="travel_claim_reminder")

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
