# Telegram Admin Bot

Handles two nagging workflows in your forum-mode group:

1. **Mileage** — from the 3rd of the month, nags daily at 17:30 (Asia/Singapore)
   in the mileage topic until you send any message there (that message is
   stored as the month's mileage reading).
2. **Travel claims (JY / HYX)** — every message you send in either travel
   topic is logged with the date. On the first Saturday of the following
   month it posts a formatted summary of the previous month, then nags daily
   at 09:00 until you reply in that topic with a message containing the word
   "claim" / "claimed".

Data is stored in a local SQLite file, so it survives restarts as long as the
file lives on a persistent volume (see Railway steps below).

## 1. Create the bot

1. Message **@BotFather** on Telegram → `/newbot` → follow prompts → copy the
   token it gives you (this is `TELEGRAM_BOT_TOKEN`).
2. Still in BotFather: `/setprivacy` → choose your bot → **Disable**.
   This lets the bot see every message in the group, not just commands —
   required for logging travel entries and mileage replies.

## 2. Add it to your group

1. Add the bot to your admin group (the one with Topics/forum mode on).
2. Send `/topicid` inside the **mileage** topic — the bot replies with the
   `chat_id` and `message_thread_id`. Note both down.
3. Send `/topicid` inside the **JY travel record** topic — note the
   `message_thread_id`.
4. Send `/topicid` inside the **HYX travel record** topic — note the
   `message_thread_id`.

(`chat_id` will be the same negative number every time — that's your group.)

## 3. Deploy to Railway

1. Push this folder to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**, pick the repo.
3. Add a **Volume**: Service → Settings → Volumes → mount path `/data`.
   This is what keeps your logged mileage/travel entries across deploys.
4. Add environment variables under Service → Variables:

   | Variable            | Value                                      |
   |----------------------|---------------------------------------------|
   | `TELEGRAM_BOT_TOKEN` | token from BotFather                        |
   | `CHAT_ID`            | the group chat id from `/topicid`           |
   | `MILEAGE_THREAD_ID`  | thread id from the mileage topic            |
   | `JY_THREAD_ID`       | thread id from the JY travel topic          |
   | `HYX_THREAD_ID`      | thread id from the HYX travel topic         |
   | `BOT_TZ`             | `Asia/Singapore` (default, optional to set) |
   | `DB_PATH`            | `/data/admin_bot.db` (default, optional)    |

5. Railway will pick up `Procfile` / `railway.json` and run `python bot.py`
   as a worker (no web port needed — this bot only long-polls Telegram).
6. Deploy. Check the logs for `Bot starting...`.

## Notes / things you can tweak

- Reminder times are set near the top of `bot.py`
  (`MILEAGE_REMINDER_TIME`, `TRAVEL_CHECK_TIME`, `TRAVEL_REMINDER_TIME`) —
  all in Asia/Singapore time by default.
- Acknowledgement is a 👍 reaction on your message; if reactions aren't
  available it falls back to a short reply.
- "Claimed" detection is a simple word match (`claim`/`claimed`, any case)
  anywhere in your message — so "claimed transport for july" works fine, and
  won't get logged as a travel entry.
- If you ever want to re-trigger a summary or reset a flag, easiest is to
  open the SQLite file directly (e.g. with the `sqlite3` CLI) — happy to add
  admin commands for this if useful.
