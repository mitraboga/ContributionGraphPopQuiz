"""Personal Telegram LeetCode reward bot with an optional CS quiz."""
from __future__ import annotations

import asyncio
import datetime as dt
from functools import wraps
import http.server
import logging
import os
import random
import secrets
import threading
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Hosting environment wins over local .env values. Load before storage imports.
load_dotenv(override=False)

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, CommandHandler, ContextTypes
from github_committer import GitHubCommitter, GitHubError, diagnose_config
from leetcode_client import LeetCodeClient, LeetCodeError, REWARDS
from questions import BANK
from rewards import RewardService
from storage import (init_db, record_result, get_score, get_daily_count, inc_daily_count,
                     set_notify_time, get_notify_time, clear_notify_time, iter_all_notify_prefs)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# HTTP request URLs can contain the Telegram token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("leetcode-bot")
DEFAULT_TZ = os.getenv("TZ", "Asia/Kolkata")
DAILY_CAP = 5
HELP_TEXT = """LeetCode Commit Bot 🧠

/daily — today's LeetCode challenge (any accepted problem qualifies)
/check — check accepted submissions and retry unfinished rewards
/status — your linked profile and reward settings
/forcecommit [1–50] [tag] — manual fallback (default: 1)
/diagnose — read-only GitHub authentication check
/notify HH:MM [Area/City] — daily reminder
/when — next reminder
/unnotify — disable reminder
/streak — recent completed LeetCode days
/csquiz or /quiz — optional 5-question CS practice; no commit reward
/score — CS practice score
/whoami — your Telegram IDs for setup
/help — this message

Easy: 10 commits • Medium: 20 • Hard: 50
One reward per problem; repeat Accepted submissions are skipped.
"""


def owner_id():
    value = os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
    return int(value) if value.isdigit() else None


def owner_only(func):
    @wraps(func)
    async def wrapped(update, context):
        if update.effective_user.id != owner_id() or update.effective_chat.type != "private":
            if update.callback_query:
                await update.callback_query.answer("This is a personal bot. Owner access is required.", show_alert=True)
            else:
                await update.effective_message.reply_text("Owner access is required. Use /whoami, then set TELEGRAM_USER_ID in the hosting environment.")
            return
        return await func(update, context)
    return wrapped


async def whoami(update, context):
    await update.effective_message.reply_text(f"Telegram user ID: {update.effective_user.id}\nChat ID: {update.effective_chat.id}")


@owner_only
async def help_cmd(update, context):
    await update.effective_message.reply_text(HELP_TEXT)


@owner_only
async def status(update, context):
    service = RewardService()
    await update.effective_message.reply_text(f"Profile: https://leetcode.com/u/{service.username}/\n"
        f"Eligible from: {service.start.date()} ({service.tz})\nEasy: 10 • Medium: 20 • Hard: 50 commits\n"
        "Automatic checks: every 15 minutes while this service is running.\n/check also retries unfinished rewards.")


async def daily_text():
    question = await asyncio.to_thread(LeetCodeClient().daily_problem)
    return (f"🧠 Today's LeetCode challenge: {question['title']} ({question['difficulty']})\n"
            f"https://leetcode.com/problems/{question['titleSlug']}/\n\n"
            "Solve this or any other problem with an Accepted submission.\n"
            "Easy → 10 commits | Medium → 20 | Hard → 50\n"
            "Use /check after solving, or wait for an automatic check.")


@owner_only
async def daily(update, context):
    try:
        message = await daily_text()
    except LeetCodeError as exc:
        message = str(exc)
    await update.effective_message.reply_text(message)


async def sync_result(context):
    lock = context.application.bot_data["commit_lock"]
    if lock.locked():
        return None
    async with lock:
        return await asyncio.to_thread(RewardService().sync)


@owner_only
async def check(update, context):
    await update.effective_message.reply_text("Checking Accepted submissions. A large reward can take a few minutes; you can keep using the bot.")
    result = await sync_result(context)
    await update.effective_message.reply_text(result.message() if result else "A commit operation is already running. Please retry shortly.")


async def auto_check(context):
    result = await sync_result(context)
    if result and (result.errors or any(n for _, n in result.completed)):
        message = result.message()
        # Avoid repeating the same error every 15 minutes.
        if message != context.application.bot_data.get("last_sync_message"):
            await context.bot.send_message(owner_id(), message)
            context.application.bot_data["last_sync_message"] = message
    elif result:
        context.application.bot_data.pop("last_sync_message", None)


@owner_only
async def diagnose(update, context):
    try:
        message = await asyncio.to_thread(GitHubCommitter.from_env().preflight)
    except GitHubError as exc:
        message = str(exc)
    await update.effective_message.reply_text(diagnose_config() + "\n\n" + message)


@owner_only
async def forcecommit(update, context):
    try:
        n = int(context.args[0]) if context.args else 1
        if not 1 <= n <= 50 or len(context.args) > 2:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /forcecommit [1–50] [tag]")
        return
    lock = context.application.bot_data["commit_lock"]
    if lock.locked():
        await update.effective_message.reply_text("A commit operation is already running. Please retry shortly.")
        return
    await update.effective_message.reply_text(f"Creating {n} manual override commit(s)…")
    async with lock:
        try:
            committer = GitHubCommitter.from_env()
            count = await asyncio.to_thread(committer.commit_n, n, context.args[1] if len(context.args) > 1 else "manual")
            message = f"✅ Created {count} manual override commits. LeetCode rewards and streaks are tracked separately."
        except GitHubError as exc:
            message = str(exc)
    await update.effective_message.reply_text(message)


async def reminder(context):
    try:
        text = await daily_text()
    except LeetCodeError as exc:
        text = str(exc)
    await context.bot.send_message(context.job.chat_id, text)


def schedule_reminder(app, chat_id, user_id, hour, minute, tzname):
    name = f"daily-{chat_id}-{user_id}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    app.job_queue.run_daily(reminder, time=dt.time(hour, minute, tzinfo=ZoneInfo(tzname)),
                            chat_id=chat_id, user_id=user_id, name=name)


@owner_only
async def notify(update, context):
    try:
        if not 1 <= len(context.args) <= 2:
            raise ValueError
        hour, minute = map(int, context.args[0].split(":"))
        tzname = context.args[1] if len(context.args) == 2 else DEFAULT_TZ
        dt.time(hour, minute, tzinfo=ZoneInfo(tzname))
    except (ValueError, KeyError):
        await update.effective_message.reply_text("Usage: /notify HH:MM [Area/City], e.g. /notify 09:00 Asia/Kolkata")
        return
    cid, uid = update.effective_chat.id, update.effective_user.id
    set_notify_time(cid, uid, hour, minute, tzname)
    schedule_reminder(context.application, cid, uid, hour, minute, tzname)
    await update.effective_message.reply_text(f"Daily LeetCode reminder set for {hour:02d}:{minute:02d} ({tzname}).")


@owner_only
async def when(update, context):
    prefs = get_notify_time(update.effective_chat.id, update.effective_user.id)
    if not prefs:
        await update.effective_message.reply_text("No reminder set. Use /notify HH:MM [Area/City].")
        return
    hour, minute, tzname = prefs
    now = dt.datetime.now(ZoneInfo(tzname))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    await update.effective_message.reply_text(f"Next reminder: {target:%Y-%m-%d %H:%M} ({tzname}).")


@owner_only
async def unnotify(update, context):
    cid, uid = update.effective_chat.id, update.effective_user.id
    clear_notify_time(cid, uid)
    for job in context.job_queue.get_jobs_by_name(f"daily-{cid}-{uid}"):
        job.schedule_removal()
    await update.effective_message.reply_text("Daily reminder disabled. Automatic reward checks remain enabled.")


@owner_only
async def streak(update, context):
    from storage import _db
    # /check refreshes this cache from the durable GitHub reward records.
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leetcode_rewards'").fetchone()
        rows = conn.execute("SELECT record FROM leetcode_rewards WHERE username=? AND completed=1", (RewardService().username,)).fetchall() if exists else []
    import json
    days = sorted({dt.date.fromisoformat(json.loads(row["record"])["day"]) for row in rows})
    best = running = 0
    previous = None
    for day in days:
        running = running + 1 if previous and day == previous + dt.timedelta(days=1) else 1
        best = max(best, running)
        previous = day
    today = dt.datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    current = running if days and days[-1] >= today - dt.timedelta(days=1) else 0
    await update.effective_message.reply_text(f"🔥 Synced LeetCode streak: {current} day(s), best {best}.\n"
        f"Synced problems: {len(rows)}. Use /check to refresh. Manual overrides and CS practice do not count.")


async def ask_cs(update, context):
    today = dt.datetime.now(ZoneInfo(DEFAULT_TZ)).date().isoformat()
    cid, uid = update.effective_chat.id, update.effective_user.id
    count = get_daily_count(cid, uid, today)
    if count >= DAILY_CAP:
        await context.bot.send_message(cid, f"CS practice complete: {DAILY_CAP}/{DAILY_CAP} today. Use /daily for LeetCode.")
        return
    seen = context.user_data.setdefault("cs_seen", set())
    pool = [i for i in range(len(BANK)) if i not in seen]
    if not pool:
        seen.clear()
        pool = list(range(len(BANK)))
    index = random.choice(pool)
    seen.add(index)
    question = BANK[index]
    nonce = secrets.token_hex(4)
    buttons = [[InlineKeyboardButton(f"{chr(65+i)}: {option}", callback_data=f"cs:{nonce}:{i}")] for i, option in enumerate(question.options)]
    message = await context.bot.send_message(cid, f"🧠 {question.category}: {question.question}\nCS practice {count+1}/{DAILY_CAP}", reply_markup=InlineKeyboardMarkup(buttons))
    context.user_data["cs_q"] = {"nonce": nonce, "message_id": message.message_id, "chat_id": cid, "question": question, "day": today, "answered": False}


@owner_only
async def csquiz(update, context):
    await ask_cs(update, context)


@owner_only
async def cs_callback(update, context):
    query = update.callback_query
    await query.answer()
    current = context.user_data.get("cs_q")
    parts = query.data.split(":")
    today = dt.datetime.now(ZoneInfo(DEFAULT_TZ)).date().isoformat()
    if len(parts) != 3 or not current or parts[1] != current["nonce"] or query.message.message_id != current["message_id"] or query.message.chat_id != current["chat_id"] or current["day"] != today:
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if parts[2] == "next" and current["answered"]:
        # Consume next before awaiting send_message, so rapid double taps cannot advance twice.
        context.user_data.pop("cs_q", None)
        await query.edit_message_reply_markup(reply_markup=None)
        await ask_cs(update, context)
        return
    if current["answered"]:
        return
    try:
        index = int(parts[2])
        if not 0 <= index < len(current["question"].options):
            return
    except ValueError:
        return
    current["answered"] = True
    question = current["question"]
    correct = index == question.correct_index
    record_result(update.effective_chat.id, update.effective_user.id, correct)
    count = inc_daily_count(update.effective_chat.id, update.effective_user.id, today)
    verdict = "✅ Correct!" if correct else f"❌ Incorrect. Answer: {question.options[question.correct_index]}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Next question", callback_data=f"cs:{current['nonce']}:next")]]) if count < DAILY_CAP else None
    await query.edit_message_text(f"{verdict}\n\n{question.question}\nCS practice: {count}/{DAILY_CAP}", reply_markup=markup)


@owner_only
async def expired_callback(update, context):
    await update.callback_query.answer("This old quiz session has expired. Use /csquiz.", show_alert=True)
    await update.callback_query.edit_message_reply_markup(reply_markup=None)


@owner_only
async def score(update, context):
    correct, total = get_score(update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text(f"Practice score: {correct}/{total} correct (includes any historical quiz scores).")


async def post_init(app):
    app.bot_data["commit_lock"] = asyncio.Lock()
    for cid, uid, hour, minute, tzname in iter_all_notify_prefs():
        if uid == owner_id():
            schedule_reminder(app, cid, uid, hour, minute, tzname)
    if owner_id():
        app.job_queue.run_repeating(auto_check, interval=900, first=5, name="leetcode-sync")
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in [
        ("daily", "Today's LeetCode challenge"), ("check", "Sync accepted problems"),
        ("forcecommit", "Manual contribution fallback"), ("status", "Profile and rewards"),
        ("notify", "Set daily reminder"), ("csquiz", "Optional CS practice"),
        ("diagnose", "Check GitHub authentication"), ("help", "All commands")]])


async def error_handler(update, context):
    # Do not log request URLs, tokens, or full update contents.
    logger.error("Bot handler failed: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("The operation could not finish. Completed reward steps are preserved. Try /check or /diagnose.")


def keepalive(port):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path in ("/", "/healthz") else 404)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def build_application(token):
    app = Application.builder().token(token).rate_limiter(AIORateLimiter()).post_init(post_init).build()
    for commands, func in [(["start", "help"], help_cmd), (["whoami"], whoami), (["status"], status),
                           (["daily"], daily), (["check"], check), (["forcecommit"], forcecommit),
                           (["diagnose"], diagnose), (["notify"], notify), (["when"], when),
                           (["unnotify"], unnotify), (["quiz", "csquiz"], csquiz), (["score"], score), (["streak"], streak)]:
        app.add_handler(CommandHandler(commands, func, block=func not in (check, forcecommit, diagnose, daily)))
    app.add_handler(CallbackQueryHandler(cs_callback, pattern=r"^cs:"))
    app.add_handler(CallbackQueryHandler(expired_callback, pattern=r"^(opt:|next$)"))
    app.add_error_handler(error_handler)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook", action="store_true")
    args = parser.parse_args()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN in the environment.")
    init_db()
    app = build_application(token)
    base = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BASE_URL")
    port = int(os.getenv("PORT", "8000"))
    if base or args.webhook:
        secret = os.getenv("WEBHOOK_SECRET", "")
        if not base or not secret or secret == "defaultsecret":
            raise SystemExit("Webhook mode requires BASE_URL or RENDER_EXTERNAL_URL and a custom WEBHOOK_SECRET.")
        # PTB verifies Telegram's secret header. No secrets in the URL or logs.
        app.run_webhook(listen="0.0.0.0", port=port, url_path="telegram", webhook_url=f"{base.rstrip('/')}/telegram", secret_token=secret)
    else:
        keepalive(port)
        app.run_polling()


if __name__ == "__main__":
    main()
