"""Telegram front end. Reads only the local DB; the one thing that can spend
money, the scheduled refresh check, lives in refresh.py and runs on a timer,
never because a user asked for something.

    /pad -> pick a size (or Custom -> type a length) -> top 5 by price per pad
         -> [📷 n] photo, [🔗 n] product page, [Show 5 more]
"""

import asyncio
import logging
import os
import warnings
from datetime import datetime, timezone

from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import config, db, messages, refresh
from .messages import PARSE_MODE
from .repo import Repo
from .sizes import BUCKETS, SizeInputError, SizeRange, parse_length_input, size_from_token

log = logging.getLogger("padbot.bot")

CHOOSE, CUSTOM = range(2)


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Repo:
    return context.application.bot_data["repo"]


async def _show_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int, size: SizeRange, offset: int) -> None:
    repo = _repo(context)
    updated = await asyncio.to_thread(repo.last_updated)
    if updated is None:
        await context.bot.send_message(chat_id, messages.NO_DATA)
        return
    rows, has_more = await asyncio.to_thread(repo.page, size, offset, config.RESULTS_PER_PAGE)
    if not rows:
        await context.bot.send_message(chat_id, messages.empty_text(size, offset), parse_mode=PARSE_MODE)
        return
    age = messages.format_age(updated, datetime.now(timezone.utc))
    await context.bot.send_message(
        chat_id,
        messages.results_text(rows, size, offset, age),
        parse_mode=PARSE_MODE,
        reply_markup=messages.results_keyboard(rows, size, offset, has_more),
    )


# --- /pad conversation ----------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(messages.WELCOME)


async def pad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(messages.ASK_SIZE, reply_markup=messages.size_keyboard())
    return CHOOSE


async def size_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await _show_page(context, update.effective_chat.id, BUCKETS[query.data.split(":", 1)[1]], 0)
    return ConversationHandler.END


async def custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await context.bot.send_message(update.effective_chat.id, messages.ASK_CUSTOM)
    return CUSTOM


async def custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        size = parse_length_input(update.effective_message.text)
    except SizeInputError as exc:
        await update.effective_message.reply_text(str(exc))
        return CUSTOM  # ask again
    await _show_page(context, update.effective_chat.id, size, 0)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(messages.CANCELLED)
    return ConversationHandler.END


# --- buttons on a results message (work after the conversation has ended) --------


async def show_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, token, offset = query.data.split(":")
        size, offset = size_from_token(token), int(offset)
    except ValueError:
        return
    if size is None:
        return
    # Drop the button first so a double tap can't show the page twice.
    markup = getattr(query.message, "reply_markup", None)
    if markup is not None:
        try:
            await query.edit_message_reply_markup(messages.strip_more(markup))
        except TelegramError as exc:
            log.debug("could not strip the 'more' button: %s", exc)
    await _show_page(context, update.effective_chat.id, size, offset)


async def send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First tap: Telegram fetches image_url and we keep the returned file_id.
    Later taps reuse the file_id, which is instant."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    code = query.data.split(":", 1)[1]
    repo = _repo(context)
    product = await asyncio.to_thread(repo.product, code)
    if product is None or not product["image_url"]:
        await context.bot.send_message(chat_id, "Sorry, I don't have a photo for that one.")
        return
    caption = messages.photo_caption(product)

    if product["tg_file_id"]:
        try:
            await context.bot.send_photo(chat_id, product["tg_file_id"], caption=caption, parse_mode=PARSE_MODE)
            return
        except BadRequest as exc:  # stale file_id: forget it and fall through to the URL
            log.warning("cached file_id for %s rejected (%s); refetching", code, exc)
            await asyncio.to_thread(repo.remember_file_id, code, None)
    try:
        sent = await context.bot.send_photo(chat_id, product["image_url"], caption=caption, parse_mode=PARSE_MODE)
    except TelegramError as exc:
        log.warning("photo for %s failed: %s", code, exc)
        await context.bot.send_message(chat_id, "Sorry, I couldn't load that photo.")
        return
    await asyncio.to_thread(repo.remember_file_id, code, sent.photo[-1].file_id)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handy for filling in TELEGRAM_ADMIN_CHAT_ID (who gets refresh alerts)."""
    await update.effective_message.reply_text(f"This chat's id is {update.effective_chat.id}.")


async def nudge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(messages.NUDGE)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)


# --- scheduled refresh ------------------------------------------------------------


def _human(seconds: float) -> str:
    return f"{seconds / 86400:.1f} days" if seconds >= 2 * 86400 else f"{seconds / 3600:.1f} hours"


def schedule_refresh(job_queue, db_path: str, outcome: refresh.Outcome | None = None) -> float:
    """Queue the next refresh check for when one is actually due (~14 days after
    a good scrape), instead of polling. It is re-planned after every check, and
    again on restart, from what the database says."""
    delay = refresh.seconds_until_next_check(db_path, outcome)
    for job in job_queue.get_jobs_by_name("refresh"):
        job.schedule_removal()
    # misfire_grace_time=None: if the machine slept through the due time, still run.
    job_queue.run_once(refresh_job, when=delay, name="refresh", job_kwargs={"misfire_grace_time": None})
    log.info("next refresh check in %s", _human(delay))
    return delay


ALERTS = {
    "ran": "✅ Price data refreshed",
    "failed": "⚠️ Refresh failed",
    "halted": "🛑 Auto-refresh stopped",
    "no_data": "⚠️ No price data",
}


async def refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.application.bot_data
    lock: asyncio.Lock = data["refresh_lock"]
    if lock.locked():
        return
    db_path = data["repo"].db_path
    async with lock:
        outcome = await asyncio.to_thread(refresh.refresh_if_due, db_path, source_factory=data["source_factory"])
    if outcome.status != "fresh":
        log.log(logging.WARNING if outcome.status in ("failed", "halted", "no_data") else logging.INFO,
                "refresh check: %s %s", outcome.status, outcome.detail)  # fmt: skip

    # Tell the owner about results and problems, once per change of state
    # (a persistent problem would otherwise message every check).
    admin = data.get("admin_chat_id")
    last = data.get("last_alert")
    data["last_alert"] = outcome.status if outcome.status in ("failed", "halted", "no_data") else None
    if admin and (outcome.status == "ran" or (outcome.status in ALERTS and outcome.status != last)):
        try:
            await context.bot.send_message(admin, f"{ALERTS[outcome.status]}: {outcome.detail}")
        except TelegramError as exc:
            log.warning("could not alert admin: %s", exc)

    schedule_refresh(context.job_queue, db_path, outcome)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([("pad", "Find the cheapest sanitary pads"), ("cancel", "Cancel the current search")])


def build_application(
    token: str,
    db_path: str,
    *,
    request=None,
    auto_refresh: bool = config.AUTO_REFRESH,
    admin_chat_id: int | None = None,
    source_factory=refresh.default_source,
) -> Application:
    builder = ApplicationBuilder().token(token).post_init(_post_init)
    if request is not None:  # tests swap in a fake transport
        builder = builder.request(request)
    app = builder.build()
    app.bot_data.update(
        repo=Repo(db_path),
        refresh_lock=asyncio.Lock(),
        admin_chat_id=admin_chat_id,
        source_factory=source_factory,
    )

    # PTB warns that CallbackQueryHandlers in a per-chat conversation aren't tied
    # to one message. That is the point: any earlier size keyboard keeps working.
    warnings.filterwarnings("ignore", message=".*per_message=False.*", category=PTBUserWarning)
    app.add_handler(
        ConversationHandler(
            # The size buttons are entry points too, so an old keyboard still works
            # after the conversation timed out or the bot restarted.
            entry_points=[
                CommandHandler("pad", pad),
                CallbackQueryHandler(size_chosen, pattern=r"^sz:(light|medium|heavy)$"),
                CallbackQueryHandler(custom_prompt, pattern=r"^sz:custom$"),
            ],
            states={
                CHOOSE: [
                    CallbackQueryHandler(size_chosen, pattern=r"^sz:(light|medium|heavy)$"),
                    CallbackQueryHandler(custom_prompt, pattern=r"^sz:custom$"),
                ],
                CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_text)],
            },
            fallbacks=[CommandHandler("cancel", cancel), CommandHandler("pad", pad)],
            allow_reentry=True,
            conversation_timeout=15 * 60,
        )
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(CallbackQueryHandler(show_more, pattern=r"^more:"))
    app.add_handler(CallbackQueryHandler(send_photo, pattern=r"^pic:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, nudge))
    app.add_error_handler(on_error)

    if auto_refresh:
        if app.job_queue is None:
            log.warning("python-telegram-bot[job-queue] is not installed: automatic refresh is off")
        else:
            schedule_refresh(app.job_queue, db_path)
    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs, and Telegram's contain the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set (put it in .env)")
        return 1
    level, summary = refresh.describe(config.DB_PATH)  # also creates the schema if the file is new
    log.log(level, "%s", summary)

    admin = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    app = build_application(token, config.DB_PATH, admin_chat_id=int(admin) if admin else None)
    log.info("starting; auto-refresh %s", "on" if config.AUTO_REFRESH else "off")
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
