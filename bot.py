import os
import re
import sqlite3
import logging
from datetime import datetime, date, time as dtime, timedelta
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
CYCLE_CHECK_TIME = dtime(9, 15, tzinfo=TZ)          # daily check for policy cycle renewal

CLAIM_KEYWORD_RE = re.compile(r"\bclaim(ed)?\b", re.IGNORECASE)

TRAVEL_GROUPS = {
    "JY": JY_THREAD_ID,
    "HYX": HYX_THREAD_ID,
}

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]

# Mileage rebate tiers: (avg km/day upper bound, daily rebate rate as a decimal)
# < 14 km/day -> 0.08%, < 22 km/day -> 0.06%, < 33 km/day -> 0.03%, else 0%
MILEAGE_REBATE_TIERS = [
    (14, 0.0008),
    (22, 0.0006),
    (33, 0.0003),
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
    conn.execute("""CREATE TABLE IF NOT EXISTS mileage_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        premium REAL,
        cycle_date TEXT,
        cycle_year_processed INTEGER,
        premium_confirmed INTEGER DEFAULT 1
    )""")
    # Migrate older DBs that don't have the numeric `value` column yet.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mileage)").fetchall()]
    if "value" not in cols:
        conn.execute("ALTER TABLE mileage ADD COLUMN value REAL")
    settings_cols = [r[1] for r in conn.execute("PRAGMA table_info(mileage_settings)").fetchall()]
    if "cycle_year_processed" not in settings_cols:
        conn.execute("ALTER TABLE mileage_settings ADD COLUMN cycle_year_processed INTEGER")
    if "premium_confirmed" not in settings_cols:
        conn.execute("ALTER TABLE mileage_settings ADD COLUMN premium_confirmed INTEGER DEFAULT 1")
    return conn


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def is_mileage_submitted(d: date) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM mileage WHERE year_month=?", (ym(d),)).fetchone()
    conn.close()
    return row is not None


# Mileage entries with an explicit backdated date, DD/MM/YY (or DD/MM/YYYY), e.g.
# "14/07/26 86987" or "86987 14/07/26"
_SLASH_DATE = r"(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{2}|\d{4})"
_SLASH_VALUE = r"(?P<value>\d+(?:\.\d+)?)"

_SLASH_DATE_FIRST_RE = re.compile(rf"^{_SLASH_DATE}\s+{_SLASH_VALUE}\s*(?:km)?\s*$", re.IGNORECASE)
_SLASH_VALUE_FIRST_RE = re.compile(rf"^{_SLASH_VALUE}\s*(?:km)?\s+{_SLASH_DATE}\s*$", re.IGNORECASE)


def _slash_date_to_date(day_str, month_str, year_str) -> date:
    day, month, year = int(day_str), int(month_str), int(year_str)
    if len(year_str) == 2:
        year += 2000
    return date(year, month, day)


def parse_mileage_entry(raw_text: str, sent_date: date):
    """Handles backdated mileage readings like "14/07/26 86987" or "86987 14/07/26".
    Returns (reading_date, text_to_store, value). `value` is the mileage number if we
    can identify it unambiguously from the dated pattern, else None (caller should
    fall back to generic number extraction on the raw text). Anything that doesn't
    match the dated pattern returns (sent_date, unchanged text, None)."""
    text = raw_text.strip()
    normalized = re.sub(r"(?<=\d),(?=\d)", "", text)  # allow "86,987"

    for rx in (_SLASH_DATE_FIRST_RE, _SLASH_VALUE_FIRST_RE):
        m = rx.match(normalized)
        if m:
            try:
                reading_date = _slash_date_to_date(m.group("day"), m.group("month"), m.group("year"))
                value = float(m.group("value"))
                return reading_date, text, value
            except ValueError:
                pass  # invalid date — fall through to default

    return sent_date, text, None


_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_mileage_value(text: str):
    m = _NUMBER_RE.search(text.replace(",", ""))
    return float(m.group(1)) if m else None


def record_mileage(d: date, text: str, value=None):
    if value is None:
        value = parse_mileage_value(text)
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO mileage (year_month, text, value, submitted_at) VALUES (?, ?, ?, ?)",
        (ym(d), text, value, d.isoformat()),
    )
    conn.commit()
    conn.close()


def get_previous_mileage(year_month: str):
    """Most recent mileage record strictly before the given month."""
    conn = get_db()
    row = conn.execute(
        "SELECT year_month, value, submitted_at FROM mileage "
        "WHERE year_month < ? AND value IS NOT NULL ORDER BY year_month DESC LIMIT 1",
        (year_month,),
    ).fetchone()
    conn.close()
    return row


def get_mileage_settings():
    conn = get_db()
    row = conn.execute("SELECT premium, cycle_date FROM mileage_settings WHERE id=1").fetchone()
    conn.close()
    if not row:
        return None, None
    return row[0], row[1]


def set_premium(value: float):
    conn = get_db()
    conn.execute(
        """INSERT INTO mileage_settings (id, premium, premium_confirmed) VALUES (1, ?, 1)
           ON CONFLICT(id) DO UPDATE SET premium=excluded.premium, premium_confirmed=1""",
        (value,),
    )
    conn.commit()
    conn.close()


def get_cycle_tracking():
    """Returns (cycle_date_str, cycle_year_processed, premium_confirmed)."""
    conn = get_db()
    row = conn.execute(
        "SELECT cycle_date, cycle_year_processed, premium_confirmed FROM mileage_settings WHERE id=1"
    ).fetchone()
    conn.close()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def set_cycle_tracking(cycle_year_processed: int, premium_confirmed: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO mileage_settings (id, cycle_year_processed, premium_confirmed) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET cycle_year_processed=excluded.cycle_year_processed,
                                          premium_confirmed=excluded.premium_confirmed""",
        (cycle_year_processed, premium_confirmed),
    )
    conn.commit()
    conn.close()


def set_cycle_date(iso_date: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO mileage_settings (id, cycle_date) VALUES (1, ?)
           ON CONFLICT(id) DO UPDATE SET cycle_date=excluded.cycle_date""",
        (iso_date,),
    )
    conn.commit()
    conn.close()


def rebate_rate_for(avg_daily_km: float) -> float:
    for cap, rate in MILEAGE_REBATE_TIERS:
        if avg_daily_km < cap:
            return rate
    return 0.0  # 33+ km/day — no tier specified, assumed no rebate


def compute_mileage_rebate(current_value, current_submitted_at, previous_row, premium):
    """Returns (rebate_amount, avg_daily_km, rate, days) or None if not computable."""
    if previous_row is None or current_value is None or premium is None:
        return None
    _, prev_value, prev_submitted_at = previous_row
    if prev_value is None:
        return None
    current_date = datetime.fromisoformat(current_submitted_at).date()
    prev_date = datetime.fromisoformat(prev_submitted_at).date()
    days = (current_date - prev_date).days
    if days <= 0:
        return None
    diff = current_value - prev_value
    avg_daily_km = diff / days
    rate = rebate_rate_for(avg_daily_km)
    rebate = avg_daily_km * rate * premium
    return rebate, avg_daily_km, rate, days


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


def set_all_pending_claimed(grp: str):
    """Mark every outstanding (summarised but not claimed) month as claimed."""
    conn = get_db()
    conn.execute(
        "UPDATE travel_status SET claimed=1 WHERE grp=? AND summary_sent=1 AND claimed=0",
        (grp,),
    )
    conn.commit()
    conn.close()


def get_pending_travel_months(grp: str):
    """All months that have a summary sent but haven't been claimed yet, oldest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT year_month, summary_text FROM travel_status "
        "WHERE grp=? AND summary_sent=1 AND claimed=0 ORDER BY year_month",
        (grp,),
    ).fetchall()
    conn.close()
    return rows


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


def elapsed_cycle_years(cycle_date: date, today: date) -> int:
    """How many full policy-year anniversaries of cycle_date have passed by today."""
    years = today.year - cycle_date.year
    if (today.month, today.day) < (cycle_date.month, cycle_date.day):
        years -= 1
    return years


# Leading-word/number date hints, e.g. "yesterday home > x", "20 home > x"
_YESTERDAY_RE = re.compile(r"^yesterday\b[:\-]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_TODAY_RE = re.compile(r"^today\b[:\-]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_DAYNUM_RE = re.compile(r"^(\d{1,2})\b[:\-]?\s*(.*)$", re.DOTALL)


def parse_travel_entry(raw_text: str, sent_date: date):
    """Figure out which calendar date a travel entry refers to.

    Supports:
      "yesterday home > x > home"      -> sent_date - 1 day
      "today home > x > home"          -> sent_date
      "20 home > x > home"             -> day 20 of sent_date's month
                                           (or previous month if 20 > sent_date.day,
                                           e.g. logging the 31st on the 1st/2nd)
      anything else (e.g. "Whole week home > kc3 > home") -> sent_date, unchanged
    Returns (target_date, cleaned_text).
    """
    text = raw_text.strip()

    m = _YESTERDAY_RE.match(text)
    if m:
        return sent_date - timedelta(days=1), m.group(1).strip()

    m = _TODAY_RE.match(text)
    if m:
        return sent_date, m.group(1).strip()

    m = _DAYNUM_RE.match(text)
    if m:
        day_num = int(m.group(1))
        if 1 <= day_num <= 31:
            year, month = sent_date.year, sent_date.month
            if day_num > sent_date.day:
                year, month = prev_month(sent_date)
            try:
                target = date(year, month, day_num)
                return target, m.group(2).strip()
            except ValueError:
                pass  # not a valid day for that month — fall through

    return sent_date, text


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

    # --- Mileage topic: only treat it as a reading if there's an actual number in it ---
    if thread_id == MILEAGE_THREAD_ID:
        reading_date, stored_text, parsed_value = parse_mileage_entry(text, today)
        if parsed_value is None:
            parsed_value = parse_mileage_value(stored_text)
        if parsed_value is None:
            return  # just chat, not a mileage reading — leave it alone

        record_mileage(reading_date, stored_text, parsed_value)
        reply = mileage_rebate_reply(reading_date)
        if reply:
            await msg.reply_text(reply)
        else:
            await react_or_reply(context, msg, "Mileage recorded, thanks!")
        return

    # --- Travel topics ---
    for grp, tid in TRAVEL_GROUPS.items():
        if thread_id == tid:
            if CLAIM_KEYWORD_RE.search(text):
                set_all_pending_claimed(grp)
                await react_or_reply(context, msg, f"Marked all outstanding {grp} transport as claimed.")
            else:
                target_date, cleaned_text = parse_travel_entry(text, today)
                add_travel_entry(grp, target_date, cleaned_text or text)
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
    for grp in TRAVEL_GROUPS:
        pending = get_pending_travel_months(grp)
        if not pending:
            continue
        parts = []
        for year_month, summary_text in pending:
            parts.append(summary_text or format_travel_summary(grp, year_month) or year_month)
        text = "\n\n".join(parts)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=TRAVEL_GROUPS[grp],
            text=text,
        )


async def job_cycle_check(context: ContextTypes.DEFAULT_TYPE):
    cycle_date_str, cycle_year_processed, premium_confirmed = get_cycle_tracking()
    if not cycle_date_str:
        return
    cycle_date = datetime.strptime(cycle_date_str, "%d/%m/%y").date()
    today = datetime.now(TZ).date()
    current_elapsed = elapsed_cycle_years(cycle_date, today)

    if current_elapsed > (cycle_year_processed or 0):
        set_cycle_tracking(current_elapsed, 0)
        premium_confirmed = 0

    if not premium_confirmed:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=MILEAGE_THREAD_ID,
            text=(
                "Your policy cycle has renewed! Please set your new premium "
                "with /setpremium <amount>."
            ),
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
        "SELECT text, value, submitted_at FROM mileage WHERE year_month=?", (ym(d),)
    ).fetchone()
    conn.close()
    if not row:
        return "No mileage recorded yet this month."

    text, value, submitted_at = row
    lines = [f"Mileage recorded this month: {text}"]
    premium, cycle_date = get_mileage_settings()
    if premium is not None:
        previous = get_previous_mileage(ym(d))
        result = compute_mileage_rebate(value, submitted_at, previous, premium)
        if result:
            rebate, avg_daily_km, rate, days = result
            lines.append(
                f"Est. rebate: ${rebate:.2f} ({avg_daily_km:.2f} km/day avg over {days} days, "
                f"{rate * 100:.2f}% tier)"
            )
    if cycle_date:
        lines.append(f"Cycle date: {cycle_date}")
    return "\n".join(lines)


def mileage_rebate_reply(d: date) -> str:
    """Called right after a new mileage submission — returns a rebate message,
    or None if there isn't enough data yet to compute one."""
    conn = get_db()
    row = conn.execute(
        "SELECT value, submitted_at FROM mileage WHERE year_month=?", (ym(d),)
    ).fetchone()
    conn.close()
    if not row:
        return None
    value, submitted_at = row
    if value is None:
        return None  # couldn't parse a number out of the message

    previous = get_previous_mileage(ym(d))
    if previous is None:
        return None  # need at least two records

    premium, _ = get_mileage_settings()
    if premium is None:
        return "Mileage recorded! Set your premium with /setpremium <amount> so I can estimate your rebate."

    result = compute_mileage_rebate(value, submitted_at, previous, premium)
    if not result:
        return None
    rebate, avg_daily_km, rate, days = result
    return (
        f"Mileage recorded: {previous[1]:.0f} km \u2192 {value:.0f} km over {days} days\n"
        f"Avg {avg_daily_km:.2f} km/day \u2192 {rate * 100:.2f}% tier\n"
        f"Estimated rebate: ${rebate:.2f}"
    )


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


MILEAGE_HELP_TEXT = (
    "Mileage topic commands:\n"
    "\u2022 Just send a number \u2014 records this month's mileage reading\n"
    "\u2022 \"14/07/26 86987\" \u2014 backdates a reading to a specific date\n"
    "\u2022 /summary \u2014 this month's mileage + rebate estimate\n"
    "\u2022 /setpremium <amount> \u2014 set your insurance premium, e.g. /setpremium 980\n"
    "\u2022 /setcycle <date> \u2014 set your policy cycle date, e.g. /setcycle 15/04/26\n"
    "  (nags daily for a new premium once the cycle renews, until you /setpremium)\n"
    "\u2022 /fixmileage <YYYY-MM> <value> \u2014 correct a stored reading, e.g. /fixmileage 2026-07 86987\n"
    "\u2022 /topicid \u2014 show this topic's chat_id / thread_id\n"
    "\u2022 /help \u2014 show this message"
)

TRAVEL_HELP_TEXT = (
    "{grp} travel topic commands:\n"
    "\u2022 Just send your trip, e.g. \"home > kc3 > home\" \u2014 logs it under today's date\n"
    "\u2022 \"yesterday ...\" / \"today ...\" / \"20 ...\" \u2014 logs under a specific date\n"
    "\u2022 /summary \u2014 trips logged so far this month\n"
    "\u2022 Reply with \"claim\" / \"claimed\" \u2014 marks all outstanding months as claimed\n"
    "\u2022 /topicid \u2014 show this topic's chat_id / thread_id\n"
    "\u2022 /help \u2014 show this message"
)

GENERAL_HELP_TEXT = (
    "Admin bot commands:\n"
    "\u2022 /summary \u2014 recorded mileage or trips\n"
    "\u2022 /setpremium <amount> \u2014 set insurance premium (mileage topic)\n"
    "\u2022 /setcycle <date> \u2014 set policy cycle date (mileage topic)\n"
    "\u2022 /topicid \u2014 show chat_id / thread_id\n"
    "\u2022 /help \u2014 this message\n\n"
    "Send /help inside the mileage, JY, or HYX topic for topic-specific commands."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return
    thread_id = msg.message_thread_id

    if thread_id == MILEAGE_THREAD_ID:
        text = MILEAGE_HELP_TEXT
    else:
        grp = next((g for g, tid in TRAVEL_GROUPS.items() if tid == thread_id), None)
        text = TRAVEL_HELP_TEXT.format(grp=grp) if grp else GENERAL_HELP_TEXT

    await msg.reply_text(text)


async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return
    if not context.args:
        await msg.reply_text("Usage: /setpremium <amount>  e.g. /setpremium 980")
        return
    try:
        value = float(context.args[0].replace(",", ""))
    except ValueError:
        await msg.reply_text("That doesn't look like a number. Usage: /setpremium 980")
        return
    set_premium(value)
    await msg.reply_text(f"Premium set to ${value:.2f}.")


async def cmd_setcycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return
    if not context.args:
        await msg.reply_text("Usage: /setcycle DD/MM/YY  e.g. /setcycle 15/04/26")
        return
    raw = context.args[0]
    parsed = None
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if not parsed:
        await msg.reply_text("Couldn't parse that date. Try DD/MM/YY, e.g. /setcycle 15/04/26")
        return
    set_cycle_date(parsed.strftime("%d/%m/%y"))
    set_cycle_tracking(elapsed_cycle_years(parsed, datetime.now(TZ).date()), 1)
    await msg.reply_text(f"Cycle date set to {parsed.strftime('%d/%m/%y')}.")


_YEAR_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


async def cmd_fixmileage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually correct a month's stored mileage value, e.g. /fixmileage 2026-07 86987."""
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return
    if len(context.args) < 2:
        await msg.reply_text(
            "Usage: /fixmileage <YYYY-MM> <value>  e.g. /fixmileage 2026-07 86987"
        )
        return
    year_month = context.args[0]
    if not _YEAR_MONTH_RE.match(year_month):
        await msg.reply_text("Month should be in YYYY-MM format, e.g. 2026-07")
        return
    try:
        value = float(context.args[1].replace(",", ""))
    except ValueError:
        await msg.reply_text("That doesn't look like a number.")
        return

    conn = get_db()
    row = conn.execute("SELECT text FROM mileage WHERE year_month=?", (year_month,)).fetchone()
    if not row:
        conn.close()
        await msg.reply_text(f"No mileage record found for {year_month}.")
        return
    conn.execute("UPDATE mileage SET value=? WHERE year_month=?", (value, year_month))
    conn.commit()
    conn.close()
    await msg.reply_text(f"Updated {year_month} mileage value to {value:.0f} km (text left as-is: \"{row[0]}\").")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("topicid", cmd_topicid))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("setpremium", cmd_setpremium))
    app.add_handler(CommandHandler("setcycle", cmd_setcycle))
    app.add_handler(CommandHandler("fixmileage", cmd_fixmileage))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    jq = app.job_queue
    jq.run_daily(job_mileage_reminder, time=MILEAGE_REMINDER_TIME, name="mileage_reminder")
    jq.run_daily(job_travel_summary_check, time=TRAVEL_CHECK_TIME, name="travel_summary_check")
    jq.run_daily(job_travel_claim_reminder, time=TRAVEL_REMINDER_TIME, name="travel_claim_reminder")
    jq.run_daily(job_cycle_check, time=CYCLE_CHECK_TIME, name="cycle_check")

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
