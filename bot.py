#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp
import qrcode
from telegram import (
    BotCommand,
    ChatMemberUpdated,
    InputMediaPhoto,
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import payment_config, settings
from database import MongoDatabase
from database.database import new_id, utcnow

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("auto_membership_bot")

ACTIVE_MEMBER_STATUSES = {"member", "administrator", "creator"}
SMALL_CAPS = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def small_caps(value: str) -> str:
    return value.translate(SMALL_CAPS)


def small_caps_html(value: str) -> str:
    """Convert visible text while preserving HTML tags, links and code."""
    protected = re.compile(
        r"(<a\b[^>]*>.*?</a>|<code>.*?</code>|<pre>.*?</pre>|<[^>]+>|\{\{?[A-Za-z_]+\}\}?)",
        re.IGNORECASE | re.DOTALL,
    )
    chunks = protected.split(value)
    output = []
    for chunk in chunks:
        if not chunk:
            continue
        output.append(chunk if protected.fullmatch(chunk) else small_caps(chunk))
    return "".join(output)


def quote(text: str) -> str:
    return f"<blockquote>{small_caps_html(text)}</blockquote>"


def user_mention(user_id: int, first_name: str = "there") -> str:
    return f'<a href="tg://user?id={user_id}">{esc(first_name or "there")}</a>'


def mention(user) -> str:
    return user_mention(user.id, user.first_name or "there")


def render_message_template(
    template: str,
    user_id: int,
    first_name: str = "there",
    **values: object,
) -> str:
    """Render user/admin message placeholders without leaving raw tokens."""
    replacements = {
        "mention": user_mention(user_id, first_name),
        "first_name": esc(first_name or "there"),
    }
    replacements.update({key: esc(value) for key, value in values.items()})
    for key, rendered in replacements.items():
        template = template.replace(f"{{{{{key}}}}}", rendered)
        template = template.replace(f"{{{key}}}", rendered)
    return template


def admin_only(user_id: int) -> bool:
    return user_id in settings.all_admin_ids


admin_contact_override = settings.admin_contact
ADMIN_PANEL_GIF_URL = (
    "https://www.image2url.com/r2/default/gifs/"
    "1788776067799-bc1610f0-366b-4929-b37f-170b6f37724e.gif"
)


def InlineKeyboardButton(text: str, *args, **kwargs):
    """Use Bot API button styles while remaining compatible with PTB.

    PTB 21.x exposes unknown Telegram fields through api_kwargs. This lets
    Telegram clients that support Bot API 9.4 render primary/success/danger
    buttons, while older clients simply ignore the extra field.
    """
    original_text = text
    style = kwargs.pop("style", None)
    if style is None:
        if original_text.startswith("🔵"):
            style = "primary"
        elif original_text.startswith("🟢"):
            style = "success"
        elif original_text.startswith("🔴"):
            style = "danger"
    # Keep the style signal internally, but do not show the color emoji in
    # the rendered button label.
    text = re.sub(r"^[🔵🟢🔴]\s*", "", text)
    api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
    if style:
        api_kwargs["style"] = style
    return TelegramInlineKeyboardButton(
        small_caps(text), *args, api_kwargs=api_kwargs or None, **kwargs
    )


def home_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔵 buy membership", callback_data="browse")],
        [
            InlineKeyboardButton("🟢 my memberships", callback_data="my_access"),
            contact_button(),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔴 admin panel", callback_data="admin:menu")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔵 channels", callback_data="admin:channels"),
                InlineKeyboardButton("🟢 plans", callback_data="admin:plans"),
            ],
            [
                InlineKeyboardButton(
                    "👥 premium users", callback_data="admin:premium_users"
                ),
                InlineKeyboardButton("⚙️ manage setup", callback_data="admin:setup"),
            ],
            [InlineKeyboardButton("📊 statistics", callback_data="admin:stats")],
            [InlineKeyboardButton("⬅️ user menu", callback_data="home")],
        ]
    )


def display_duration(days: int) -> str:
    if days <= 0:
        return "Lifetime"
    if days % 30 == 0:
        return f"{days // 30} month(s)"
    if days % 7 == 0:
        return f"{days // 7} week(s)"
    return f"{days} day(s)"


def format_price(price: float) -> str:
    return "FREE" if float(price) == 0 else f"₹{float(price):.2f}"


def order_id() -> str:
    return f"ORD{int(time.time() * 1000)}{new_id()[:4].upper()}"


def contact_url() -> str | None:
    contact = admin_contact_override.strip()
    if not contact:
        return None
    if contact.startswith("http://") or contact.startswith("https://"):
        return contact
    return "https://t.me/" + contact.lstrip("@")


def contact_button() -> InlineKeyboardButton:
    url = contact_url()
    if url:
        return InlineKeyboardButton("💬 contact admin", url=url)
    return InlineKeyboardButton("💬 contact admin", callback_data="contact")


def upi_uri(amount: float, oid: str) -> str:
    query = urlencode(
        {
            "pa": payment_config.upi_id,
            "pn": payment_config.upi_payee_name,
            "am": f"{amount:.2f}",
            "cu": "INR",
            "tn": f"{payment_config.upi_note} {oid}",
            "tr": oid,
        }
    )
    return f"upi://pay?{query}"


def make_qr(uri: str) -> io.BytesIO:
    image = qrcode.make(uri)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    stream.name = "payment-qr.png"
    return stream


def db_from(context: ContextTypes.DEFAULT_TYPE) -> MongoDatabase:
    return context.application.bot_data["db"]


def payment_tasks(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("payment_tasks", {})


async def send_html(bot, chat_id: int, text: str, **kwargs):
    return await bot.send_message(
        chat_id=chat_id,
        text=small_caps_html(text),
        parse_mode=ParseMode.HTML,
        **kwargs,
    )


async def send_screen(
    application: Application,
    chat_id: int,
    text: str,
    setting_key: str,
    reply_markup=None,
):
    """Send an optional image screen, falling back to formatted text."""
    db: MongoDatabase = application.bot_data["db"]
    photo_id = await db.get_setting(setting_key)
    if photo_id:
        try:
            return await application.bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=small_caps_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.warning("Could not send configured screen image %s: %s", setting_key, exc)
    return await send_html(
        application.bot, chat_id, text, reply_markup=reply_markup
    )


async def clear_preview_media(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    """Remove the previous channel gallery before navigating elsewhere."""
    message_ids = context.user_data.pop("channel_preview_message_ids", [])
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id, message_id)
        except TelegramError:
            pass


async def replace_query_message(
    query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
):
    """Edit text messages, but replace photo/media messages with a text screen.

    Telegram cannot use editMessageText on a message whose content is a photo
    or other media. The start screen may intentionally be a photo, so every
    user navigation callback must handle both message shapes.
    """
    message = query.message
    await clear_preview_media(
        context, message.chat_id if message else query.from_user.id
    )
    markup = reply_markup
    if message and message.text is not None:
        try:
            return await message.edit_text(
                small_caps_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message
        except TelegramError:
            pass
    if message:
        try:
            await message.delete()
        except TelegramError:
            pass
    chat_id = message.chat_id if message else query.from_user.id
    return await context.bot.send_message(
        chat_id=chat_id,
        text=small_caps_html(text),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def replace_query_with_optional_photo(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    setting_key: str,
    reply_markup=None,
):
    """Replace a callback screen with a photo caption when configured."""
    message = query.message
    chat_id = message.chat_id if message else query.from_user.id
    await clear_preview_media(context, chat_id)
    if message:
        try:
            await message.delete()
        except TelegramError:
            pass
    photo_id = await db_from(context).get_setting(setting_key)
    if photo_id:
        try:
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=small_caps_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.warning("Could not send optional image %s: %s", setting_key, exc)
    return await context.bot.send_message(
        chat_id=chat_id,
        text=small_caps_html(text),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def delete_user_message(bot, user_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(user_id, message_id)
    except TelegramError:
        # It may already have been deleted by the user or by Telegram.
        pass


DEFAULT_WELCOME_TEXT = (
    quote("👤 <b>WELCOME</b>")
    + "\n\n"
    + quote("hello {mention} 👋, I am your membership assistant.")
    + "\n"
    + quote(
        "browse a channel, choose a plan, pay securely by UPI, and receive a private invite link."
    )
    + "\n\n"
    + quote("use the menu below to get started.")
)

DEFAULT_AVAILABLE_CHANNELS_MESSAGE = (
    quote("🛍️ <b>AVAILABLE CHANNELS</b>")
    + "\n\n"
    + quote("choose a channel to view its description and plans.")
)

DEFAULT_ACCESS_GRANTED_MESSAGE = (
    quote("✅ <b>ACCESS GRANTED</b>")
    + "\n\n"
    + quote("channel: <b>{channel_name}</b>")
    + "\n"
    + quote("plan: <b>{plan_name}</b>")
    + "\n"
    + quote("expires: <code>{expiry}</code>")
    + "\n\n"
    + quote("tap below to join. your membership timer starts after you join.")
)

DEFAULT_ACCESS_ACTIVATED_MESSAGE = (
    quote("🎉 <b>WELCOME — ACCESS ACTIVATED</b>")
    + "\n\n"
    + quote("channel: <b>{channel_name}</b>")
    + "\n"
    + quote("plan: <b>{plan_name}</b>")
    + "\n"
    + quote("expires: <code>{expiry}</code>")
)


async def welcome_content(db: MongoDatabase, user) -> str:
    configured = await db.get_setting("welcome_text")
    text = configured or DEFAULT_WELCOME_TEXT
    return render_message_template(
        text,
        user.id,
        user.first_name or "there",
    )


async def personalized_message(
    db: MongoDatabase,
    user_id: int,
    setting_key: str,
    default: str,
    **values: object,
) -> str:
    user = await db.get_user(user_id)
    first_name = (user or {}).get("first_name") or "there"
    template = await db.get_setting(setting_key) or default
    return render_message_template(template, user_id, first_name, **values)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db = db_from(context)
    await db.upsert_user(user)
    text = await welcome_content(db, user)
    photo_id = await db.get_setting("welcome_photo_file_id")
    markup = home_keyboard(admin_only(user.id))
    if photo_id:
        await update.effective_message.reply_photo(
            photo=photo_id,
            caption=small_caps_html(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            small_caps_html(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def show_channels(query, context: ContextTypes.DEFAULT_TYPE, db: MongoDatabase) -> None:
    channels = await db.list_channels()
    configured_message = await db.get_setting("available_channels_message")
    channels_message = configured_message or DEFAULT_AVAILABLE_CHANNELS_MESSAGE
    if not channels:
        await replace_query_with_optional_photo(
            query,
            context,
            channels_message
            + "\n\n"
            + quote("📭 <b>no memberships are available yet.</b>\nplease check back soon."),
            "available_channels_photo_file_id",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ back", callback_data="home")]]
            ),
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"🔵 {(channel.get('button_text') or channel.get('title', 'Channel'))[:40]}",
                callback_data=f"channel:{channel['_id']}",
            )
        ]
        for channel in channels
    ]
    buttons.append([InlineKeyboardButton("⬅️ back", callback_data="home")])
    await replace_query_with_optional_photo(
        query,
        context,
        channels_message,
        "available_channels_photo_file_id",
        InlineKeyboardMarkup(buttons),
    )


async def show_channel(
    query, context: ContextTypes.DEFAULT_TYPE, db: MongoDatabase, channel_id: str
) -> None:
    channel = await db.get_channel(channel_id)
    if not channel or not channel.get("active"):
        await query.answer("This channel is no longer available.", show_alert=True)
        return
    plans = await db.list_plans(channel_id)
    buttons = [
        [
            InlineKeyboardButton(
                f"🟢 {plan['name']} • {format_price(plan['price'])}",
                callback_data=f"plan:{channel_id}:{plan['_id']}",
            )
        ]
        for plan in plans
    ]
    buttons.append([InlineKeyboardButton("⬅️ channels", callback_data="browse")])
    text = (
        quote(f"📣 <b>{esc(channel.get('title', 'membership'))}</b>")
        + "\n\n"
        + quote(esc(channel.get("description") or "choose a plan below."))
        + "\n\n"
        + quote("💳 <b>Plans</b>")
    )
    if not plans:
        text += "\n" + quote("plans are being prepared by the admin.")
    markup = InlineKeyboardMarkup(buttons)
    image_ids = [
        image_id
        for image_id in channel.get("preview_image_file_ids", [])
        if image_id
    ]
    if not image_ids:
        await replace_query_message(query, context, text, markup)
        return

    chat_id = query.message.chat_id if query.message else query.from_user.id
    await clear_preview_media(context, chat_id)
    if query.message:
        try:
            await query.message.delete()
        except TelegramError:
            pass
    try:
        gallery = await context.bot.send_media_group(
            chat_id=chat_id,
            media=[InputMediaPhoto(media=image_id) for image_id in image_ids[:10]],
        )
        context.user_data["channel_preview_message_ids"] = [
            message.message_id for message in gallery
        ]
        details_message = await context.bot.send_message(
            chat_id=chat_id,
            text=small_caps_html(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return details_message
    except TelegramError as exc:
        log.warning("Could not send channel preview gallery for %s: %s", channel_id, exc)
        context.user_data.pop("channel_preview_message_ids", None)
        return await context.bot.send_message(
            chat_id=chat_id,
            text=small_caps_html(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str, plan_id: str) -> None:
    query = update.callback_query
    db = db_from(context)
    channel = await db.get_channel(channel_id)
    plan = await db.get_plan(plan_id)
    if not channel or not plan or not channel.get("active") or not plan.get("active"):
        await query.answer("This plan is no longer available.", show_alert=True)
        return
    if float(plan["price"]) == 0:
        await activate_free_plan(context.application, query.from_user.id, channel, plan)
        await query.answer("Free access is being prepared.")
        await replace_query_message(
            query,
            context,
            quote("✅ <b>FREE ACCESS REQUESTED</b>")
            + "\n\n"
            + quote("check your latest message for the join link or activation details."),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ home", callback_data="home")]]
            ),
        )
        return
    oid = order_id()
    expires_at = utcnow() + timedelta(minutes=payment_config.payment_max_minutes)
    await db.create_order(
        {
            "_id": new_id(),
            "order_id": oid,
            "user_id": query.from_user.id,
            "channel_id": channel["channel_id"],
            "channel_doc_id": channel_id,
            "channel_name": channel.get("title", "Channel"),
            "plan_id": plan_id,
            "plan_name": plan["name"],
            "duration_days": int(plan["duration_days"]),
            "amount": float(plan["price"]),
            "status": "pending",
            "expires_at": expires_at,
            "created_at": utcnow(),
        }
    )
    uri = upi_uri(float(plan["price"]), oid)
    caption = (
        quote("💳 <b>PAYMENT REQUEST</b>")
        + "\n\n"
        + quote(f"📣 channel: <b>{esc(channel.get('title'))}</b>")
        + "\n"
        + quote(f"📦 plan: <b>{esc(plan['name'])}</b> • {display_duration(int(plan['duration_days']))}")
        + "\n"
        + quote(f"💰 amount: <b>₹{float(plan['price']):.2f}</b>")
        + "\n"
        + quote(f"🆔 order: <code>{oid}</code>")
        + "\n\n"
        + quote(
            f"scan the QR or pay to <code>{esc(payment_config.upi_id)}</code>."
            f"\nthis order expires in {payment_config.payment_max_minutes} minutes."
        )
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 check payment", callback_data=f"paycheck:{oid}")],
            [
                InlineKeyboardButton("🔴 cancel", callback_data=f"paycancel:{oid}"),
                contact_button(),
            ],
        ]
    )
    await query.answer()
    sent = await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=make_qr(uri),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await db.set_order_message(oid, sent.message_id)
    task = asyncio.create_task(payment_monitor(context.application, oid))
    payment_tasks(context)[oid] = task
    await replace_query_message(
        query,
        context,
        quote("✅ payment screen created. complete the payment using the QR above.")
        + "\n\n"
        + quote("the bot will verify it automatically. you can also tap <b>check payment</b>."),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ choose another plan", callback_data=f"channel:{channel_id}")]]
        ),
    )
    await db.update_order(oid, {"notice_message_id": query.message.message_id})


async def verify_payment_once(order: dict, db: MongoDatabase) -> dict | None:
    params = {
        "api_key": payment_config.payment_api_key,
        "order_id": order["order_id"],
        "amount": order["amount"],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(payment_config.payment_api_url, params=params) as response:
                if response.status != 200:
                    log.warning("Payment API returned HTTP %s for %s", response.status, order["order_id"])
                    return None
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("Payment verification failed for %s: %s", order["order_id"], exc)
        return None
    if not isinstance(payload, dict):
        return None

    def first(*keys):
        for key in keys:
            if payload.get(key) not in (None, ""):
                return payload[key]
        return None

    status = str(first("status", "STATUS", "Status", "payment_status") or "").upper()
    success = str(first("success", "Success") or "").upper()
    if status not in {"SUCCESS", "TXN_SUCCESS", "PAID", "COMPLETED", "COMPLETE", "TRUE", "1", "OK"} and success not in {"TRUE", "1", "YES"}:
        return None
    response_order = first("order_id", "orderId", "ORDERID", "oid")
    if response_order is not None and str(response_order) != order["order_id"]:
        return None
    paid_raw = first("amount_credited", "amount", "TXNAMOUNT", "txn_amount", "paid_amount")
    try:
        paid = float(order["amount"] if paid_raw is None else paid_raw)
    except (TypeError, ValueError):
        return None
    if abs(paid - float(order["amount"])) > payment_config.amount_tolerance:
        return None
    txn_id = first("txn_id", "TXNID", "transaction_id", "utr", "txnId")
    if txn_id and await db.is_txn_used(str(txn_id)):
        return None
    return {
        "txn_id": str(txn_id) if txn_id else "",
        "bank_txn_id": first("bank_txn_id", "BANKTXNID", "utr") or "",
        "txn_amount": paid,
        "txn_date": first("txn_date", "TXNDATE", "date", "created_at") or "",
    }


async def delete_payment_message(
    bot, order: dict, include_notice: bool = False
) -> None:
    message_ids = [order.get("payment_message_id")]
    if include_notice:
        message_ids.append(order.get("notice_message_id"))
    for message_id in {item for item in message_ids if item}:
        try:
            await bot.delete_message(order["user_id"], message_id)
        except TelegramError:
            pass


async def member_now(bot, user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ACTIVE_MEMBER_STATUSES
    except TelegramError:
        return False


async def activate_free_plan(
    application: Application, user_id: int, channel: dict, plan: dict
) -> None:
    db: MongoDatabase = application.bot_data["db"]
    now = utcnow()
    duration = int(plan["duration_days"])
    current = await db.find_subscription(user_id, channel["channel_id"], active_only=True)
    already_member = await member_now(application.bot, user_id, channel["channel_id"])

    if current and already_member:
        start = max(now, current.get("ends_at") or now)
        end = None if duration <= 0 else start + timedelta(days=duration)
        await db.update_subscription(
            current["_id"],
            {
                "ends_at": end,
                "status": "active",
                "reminder_sent": False,
                "last_order_id": None,
                "source": "free",
            },
        )
        await send_html(
            application.bot,
            user_id,
            quote("🎉 <b>FREE ACCESS EXTENDED</b>")
            + "\n\n"
            + quote(f"plan: <b>{esc(plan['name'])}</b>")
            + "\n"
            + quote(
                f"valid until: <code>{end.strftime('%d %b %Y, %I:%M %p UTC') if end else 'Lifetime'}</code>"
            ),
            reply_markup=home_keyboard(False),
        )
        return

    if already_member:
        end = None if duration <= 0 else now + timedelta(days=duration)
        await db.create_subscription(
            {
                "user_id": user_id,
                "channel_id": channel["channel_id"],
                "channel_doc_id": channel["_id"],
                "channel_name": channel.get("title", "Channel"),
                "plan_id": plan["_id"],
                "plan_name": plan["name"],
                "duration_days": duration,
                "starts_at": now,
                "ends_at": end,
                "status": "active",
                "source": "free",
                "reminder_sent": False,
            }
        )
        await send_screen(
            application,
            user_id,
            await personalized_message(
                db,
                user_id,
                "access_activated_message",
                DEFAULT_ACCESS_ACTIVATED_MESSAGE,
                channel_name=channel.get("title", "Channel"),
                plan_name=plan.get("name", "Membership"),
                expiry=format_expiry(end),
                order_id="FREE",
            ),
            "access_activated_photo_file_id",
            reply_markup=home_keyboard(False),
        )
        return

    try:
        invite = await application.bot.create_chat_invite_link(
            chat_id=channel["channel_id"],
            name=f"FREE-{new_id()[:8].upper()}",
            expire_date=now + timedelta(minutes=30),
            member_limit=1,
        )
    except TelegramError:
        await send_html(
            application.bot,
            user_id,
            quote("⚠️ <b>FREE ACCESS IS READY</b>")
            + "\n\n"
            + quote("the channel invite could not be created. please contact admin."),
            reply_markup=InlineKeyboardMarkup([[contact_button()]]),
        )
        return

    end = None if duration <= 0 else now + timedelta(days=duration)
    subscription_id = await db.create_subscription(
        {
            "user_id": user_id,
            "channel_id": channel["channel_id"],
            "channel_doc_id": channel["_id"],
            "channel_name": channel.get("title", "Channel"),
            "plan_id": plan["_id"],
            "plan_name": plan["name"],
            "duration_days": duration,
            "starts_at": now,
            "ends_at": end,
            "status": "pending_join",
            "source": "free",
            "invite_link": invite.invite_link,
            "reminder_sent": False,
        }
    )
    access_message = await send_screen(
        application,
        user_id,
        await personalized_message(
            db,
            user_id,
            "access_granted_message",
            DEFAULT_ACCESS_GRANTED_MESSAGE,
            channel_name=channel.get("title", "Channel"),
            plan_name=plan.get("name", "Membership"),
            expiry=format_expiry(end),
            order_id="FREE",
        ),
        "access_granted_photo_file_id",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔵 join channel", url=invite.invite_link)],
                [contact_button()],
            ]
        ),
    )
    await db.update_subscription(
        subscription_id, {"access_message_id": access_message.message_id}
    )


async def activate_after_payment(application: Application, order: dict, txn: dict) -> None:
    db: MongoDatabase = application.bot_data["db"]
    if not await db.claim_success(order["order_id"], txn):
        return
    await delete_payment_message(application.bot, order, include_notice=True)
    now = utcnow()
    current = await db.find_subscription(order["user_id"], order["channel_id"], active_only=True)
    already_member = await member_now(application.bot, order["user_id"], order["channel_id"])
    duration = int(order["duration_days"])
    if current and already_member:
        start = max(now, current.get("ends_at") or now)
        end = None if duration <= 0 else start + timedelta(days=duration)
        await db.update_subscription(current["_id"], {"ends_at": end, "status": "active", "reminder_sent": False, "last_order_id": order["order_id"]})
        await send_html(
            application.bot,
            order["user_id"],
            quote("🎉 <b>MEMBERSHIP EXTENDED</b>")
            + "\n\n"
            + quote(f"plan: <b>{esc(order['plan_name'])}</b>")
            + "\n"
            + quote(f"valid until: <code>{end.isoformat() if end else 'Lifetime'}</code>"),
            reply_markup=home_keyboard(False),
        )
        await log_sale(application, order, txn, "extended")
        return

    if already_member:
        end = None if duration <= 0 else now + timedelta(days=duration)
        await db.create_subscription(
            {
                "user_id": order["user_id"],
                "channel_id": order["channel_id"],
                "channel_doc_id": order["channel_doc_id"],
                "channel_name": order.get("channel_name", str(order["channel_id"])),
                "plan_id": order["plan_id"],
                "plan_name": order["plan_name"],
                "duration_days": duration,
                "starts_at": now,
                "ends_at": end,
                "status": "active",
                "order_id": order["order_id"],
                "reminder_sent": False,
            }
        )
        await send_screen(
            application,
            order["user_id"],
            await personalized_message(
                db,
                order["user_id"],
                "access_activated_message",
                DEFAULT_ACCESS_ACTIVATED_MESSAGE,
                channel_name=order.get("channel_name", str(order.get("channel_id"))),
                plan_name=order.get("plan_name", "Membership"),
                expiry=format_expiry(end),
                order_id=order.get("order_id", ""),
            ),
            "access_activated_photo_file_id",
            reply_markup=home_keyboard(False),
        )
        await log_sale(application, order, txn, "new")
        return

    try:
        invite = await application.bot.create_chat_invite_link(
            chat_id=order["channel_id"],
            name=order["order_id"],
            expire_date=now + timedelta(minutes=30),
            member_limit=1,
        )
    except TelegramError as exc:
        log.exception("Could not create invite for %s", order["order_id"])
        await send_html(
            application.bot,
            order["user_id"],
            quote("⚠️ <b>PAYMENT RECEIVED</b>")
            + "\n\n"
            + quote("your payment is recorded, but the channel invite could not be created.")
            + "\n"
            + quote(f"please contact admin with order <code>{order['order_id']}</code>."),
            reply_markup=InlineKeyboardMarkup([[contact_button()]]),
        )
        return

    start = now
    end = None if duration <= 0 else start + timedelta(days=duration)
    subscription_id = await db.create_subscription(
        {
            "user_id": order["user_id"],
            "channel_id": order["channel_id"],
            "channel_doc_id": order["channel_doc_id"],
            "channel_name": order.get("channel_name", str(order["channel_id"])),
            "plan_id": order["plan_id"],
            "plan_name": order["plan_name"],
            "duration_days": duration,
            "starts_at": start,
            "ends_at": end,
            "status": "pending_join",
            "invite_link": invite.invite_link,
            "order_id": order["order_id"],
            "reminder_sent": False,
        }
    )
    access_message = await send_screen(
        application,
        order["user_id"],
        await personalized_message(
            db,
            order["user_id"],
            "access_granted_message",
            DEFAULT_ACCESS_GRANTED_MESSAGE,
            channel_name=order.get("channel_name", order.get("channel_id")),
            plan_name=order.get("plan_name", "Membership"),
            expiry=format_expiry(end),
            order_id=order.get("order_id", ""),
        ),
        "access_granted_photo_file_id",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔵 join channel", url=invite.invite_link)],
                [contact_button()],
            ]
        ),
    )
    await db.update_subscription(
        subscription_id, {"access_message_id": access_message.message_id}
    )
    await log_sale(application, order, txn, "new")


async def log_sale(application: Application, order: dict, txn: dict, kind: str) -> None:
    if payment_config.payment_log_channel_id == 0:
        return
    text = (
        quote(f"💰 <b>NEW SALE • {kind.upper()}</b>")
        + "\n\n"
        + quote(f"👤 user: <code>{order['user_id']}</code>")
        + "\n"
        + quote(f"📣 channel: <code>{order['channel_id']}</code>")
        + "\n"
        + quote(f"📦 plan: <b>{esc(order['plan_name'])}</b>")
        + "\n"
        + quote(f"💳 amount: <b>₹{float(order['amount']):.2f}</b>")
        + "\n"
        + quote(f"🆔 order: <code>{order['order_id']}</code>")
        + "\n"
        + quote(f"🏦 transaction: <code>{esc(txn.get('txn_id') or 'N/A')}</code>")
    )
    try:
        await send_html(application.bot, payment_config.payment_log_channel_id, text)
    except TelegramError as exc:
        log.warning("Could not log sale: %s", exc)


async def payment_monitor(application: Application, oid: str) -> None:
    db: MongoDatabase = application.bot_data["db"]
    try:
        while True:
            await asyncio.sleep(max(10, payment_config.payment_verify_interval))
            order = await db.get_order(oid)
            if not order or order.get("status") != "pending":
                return
            if utcnow() >= order["expires_at"]:
                await db.update_order(oid, {"status": "expired"})
                await delete_payment_message(application.bot, order, include_notice=True)
                await send_html(
                    application.bot,
                    order["user_id"],
                    quote("⏰ <b>PAYMENT SESSION EXPIRED</b>")
                    + "\n\n"
                    + quote("no payment was detected within the allowed window.")
                    + "\n"
                    + quote("if you paid, contact admin with your order ID."),
                    reply_markup=InlineKeyboardMarkup([[contact_button()]]),
                )
                return
            txn = await verify_payment_once(order, db)
            if txn:
                await activate_after_payment(application, order, txn)
                return
    except asyncio.CancelledError:
        return
    finally:
        application.bot_data.get("payment_tasks", {}).pop(oid, None)


async def check_payment(query, context: ContextTypes.DEFAULT_TYPE, oid: str) -> None:
    db = db_from(context)
    order = await db.get_order(oid)
    if not order or order["user_id"] != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    if order["status"] != "pending":
        await query.answer(f"Order is {order['status']}.", show_alert=True)
        return
    await query.answer("Checking payment…")
    txn = await verify_payment_once(order, db)
    if txn:
        await activate_after_payment(context.application, order, txn)
    else:
        await send_html(
            context.bot,
            query.from_user.id,
            quote("🔎 <b>PAYMENT NOT DETECTED YET</b>")
            + "\n\n"
            + quote("if you have just paid, wait a little and try again. the automatic checker is still running."),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🟢 check again", callback_data=f"paycheck:{oid}")],
                    [InlineKeyboardButton("🔴 cancel", callback_data=f"paycancel:{oid}")],
                ]
            ),
        )


async def cancel_payment(query, context: ContextTypes.DEFAULT_TYPE, oid: str) -> None:
    db = db_from(context)
    order = await db.get_order(oid)
    if not order or order["user_id"] != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    if order["status"] != "pending":
        await query.answer("This order is already closed.", show_alert=True)
        return
    await db.update_order(oid, {"status": "cancelled"})
    task = payment_tasks(context).pop(oid, None)
    if task:
        task.cancel()
    await query.answer("Payment cancelled.")
    await delete_payment_message(context.bot, order, include_notice=True)
    try:
        await query.message.delete()
    except TelegramError:
        pass
    await send_html(
        context.bot,
        query.from_user.id,
        quote("🔴 <b>PAYMENT CANCELLED</b>")
        + "\n\n"
        + quote("the QR payment message was removed. you can choose another plan below."),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 choose another plan",
                        callback_data=f"channel:{order['channel_doc_id']}",
                    )
                ],
                [InlineKeyboardButton("⬅️ home", callback_data="home")],
            ]
        ),
    )


async def show_access(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    subs = await db.db.subscriptions.find(
        {"user_id": query.from_user.id, "status": {"$in": ["active", "pending_join"]}}
    ).to_list(50)
    if not subs:
        text = quote("📭 <b>NO ACTIVE ACCESS</b>\nYou have no active memberships yet.")
    else:
        lines = []
        for sub in subs:
            status = "Waiting to join" if sub["status"] == "pending_join" else "Active"
            ends = "Lifetime" if not sub.get("ends_at") else sub["ends_at"].strftime("%d %b %Y, %I:%M %p UTC")
            lines.append(f"• <b>{esc(sub.get('plan_name', 'Membership'))}</b> — {status} — {ends}")
        text = quote("🔐 <b>YOUR ACCESS</b>") + "\n\n" + quote("\n".join(lines))
    await replace_query_message(
        query,
        context,
        text,
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]),
    )


async def admin_screen(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup,
    previous_message=None,
    animation_url: str | None = None,
) -> object:
    """Replace the previous admin screen instead of stacking panel messages."""
    old_message = previous_message
    if old_message is None:
        old_id = context.user_data.get("admin_panel_message_id")
        old_chat_id = context.user_data.get("admin_panel_chat_id", chat_id)
        if old_id:
            try:
                await context.bot.delete_message(old_chat_id, old_id)
            except TelegramError:
                pass
    else:
        try:
            await old_message.delete()
        except TelegramError:
            pass
    if animation_url:
        try:
            sent = await context.bot.send_animation(
                chat_id=chat_id,
                animation=animation_url,
                caption=small_caps_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramError as exc:
            log.warning("Could not send admin panel animation: %s", exc)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=small_caps_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
    else:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=small_caps_html(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    context.user_data["admin_panel_message_id"] = sent.message_id
    context.user_data["admin_panel_chat_id"] = chat_id
    return sent


async def admin_replace_query(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    markup: InlineKeyboardMarkup,
    animation_url: str | None = None,
) -> None:
    await query.answer()
    await admin_screen(
        context,
        query.message.chat_id,
        text,
        markup,
        previous_message=query.message,
        animation_url=animation_url,
    )


async def admin_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(query.from_user.id):
        await query.answer("Admin access only.", show_alert=True)
        return
    await admin_replace_query(
        query,
        context,
        quote("🔴 <b>ADMIN CONTROL PANEL</b>")
        + "\n\n"
        + quote("Manage sales channels, plans, pricing, durations, and support contact settings."),
        admin_keyboard(),
        animation_url=ADMIN_PANEL_GIF_URL,
    )


async def admin_channels(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    channels = await db.list_channels(active_only=False)
    buttons = [[InlineKeyboardButton("➕ Add channel", callback_data="admin:add_channel")]]
    for channel in channels:
        state = "🟢" if channel.get("active") else "🔴"
        label = channel.get("button_text") or channel.get("title", "Channel")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{state} {label[:35]}",
                    callback_data=f"admin:channel:{channel['_id']}",
                    style="success" if channel.get("active") else "danger",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin:menu")])
    await admin_replace_query(
        query,
        context,
        quote("📣 <b>SALES CHANNELS</b>") + "\n\n" + quote("Add a channel ID, then configure its plans."),
        InlineKeyboardMarkup(buttons),
    )


async def admin_channel(query, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    db = db_from(context)
    channel = await db.get_channel(channel_id)
    if not channel:
        await query.answer("Channel not found.", show_alert=True)
        return
    state = "Active" if channel.get("active") else "Paused"
    state_button = (
        InlineKeyboardButton(
            "🟢 Active — click to pause",
            callback_data=f"admin:toggle_channel:{channel_id}",
            style="success",
        )
        if channel.get("active")
        else InlineKeyboardButton(
            "🔴 Paused — click to activate",
            callback_data=f"admin:toggle_channel:{channel_id}",
            style="danger",
        )
    )
    buttons = [
        [InlineKeyboardButton("🟢 Manage plans", callback_data=f"admin:channel_plans:{channel_id}")],
        [InlineKeyboardButton("✏️ Edit description", callback_data=f"admin:edit_desc:{channel_id}")],
        [InlineKeyboardButton("✏️ Edit user button text", callback_data=f"admin:edit_button:{channel_id}")],
        [InlineKeyboardButton("🖼 Set channel preview images", callback_data=f"admin:channel_images:{channel_id}")],
        [state_button],
        [InlineKeyboardButton("🔴 Delete channel", callback_data=f"admin:delete_channel_confirm:{channel_id}", style="danger")],
        [InlineKeyboardButton("⬅️ Channels", callback_data="admin:channels")],
    ]
    await admin_replace_query(
        query,
        context,
        quote(f"📣 <b>{esc(channel.get('title'))}</b>")
        + "\n\n"
        + quote(f"ID: <code>{channel['channel_id']}</code>\nStatus: <b>{state}</b>")
        + "\n"
        + quote(
            f"User list button: <b>{esc(channel.get('button_text') or channel.get('title', 'Channel'))}</b>"
        )
        + "\n"
        + quote(esc(channel.get("description") or "No description")),
        InlineKeyboardMarkup(buttons),
    )


async def admin_delete_channel_confirm(
    query, context: ContextTypes.DEFAULT_TYPE, channel_id: str
) -> None:
    db = db_from(context)
    channel = await db.get_channel(channel_id)
    if not channel:
        await query.answer("Channel not found.", show_alert=True)
        return
    await admin_replace_query(
        query,
        context,
        quote("⚠️ <b>DELETE CHANNEL?</b>")
        + "\n\n"
        + quote(
            f"This removes <b>{esc(channel.get('title', 'Channel'))}</b> and its plans from the user catalogue."
        )
        + "\n"
        + quote("Existing subscription history is preserved, but this action cannot be undone from the panel."),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔴 Yes, delete it",
                        callback_data=f"admin:delete_channel:{channel_id}",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Keep channel",
                        callback_data=f"admin:channel:{channel_id}",
                    )
                ],
            ]
        ),
    )


async def admin_plans(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    channels = await db.list_channels(active_only=False)
    buttons = [
        [InlineKeyboardButton(f"🟢 {c.get('title', 'Channel')[:35]}", callback_data=f"admin:channel_plans:{c['_id']}")]
        for c in channels
    ]
    buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin:menu")])
    await admin_replace_query(
        query,
        context,
        quote("🟢 <b>PLAN MANAGER</b>\nChoose a channel to add or edit its plans."),
        InlineKeyboardMarkup(buttons),
    )


async def admin_channel_plans(query, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    db = db_from(context)
    channel = await db.get_channel(channel_id)
    plans = await db.list_plans(channel_id, active_only=False)
    buttons = [[InlineKeyboardButton("➕ Add plan", callback_data=f"admin:add_plan:{channel_id}")]]
    for plan in plans:
        state = "✅" if plan.get("active") else "⏸️"
        buttons.append(
            [InlineKeyboardButton(f"{state} {plan['name']} • {format_price(plan['price'])}", callback_data=f"admin:edit_plan:{channel_id}:{plan['_id']}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Channel", callback_data=f"admin:channel:{channel_id}")])
    await admin_replace_query(
        query,
        context,
        quote(f"📦 <b>PLANS — {esc(channel.get('title', 'Channel'))}</b>")
        + "\n\n"
        + quote("Create custom durations in days. Use 0 days for lifetime."),
        InlineKeyboardMarkup(buttons),
    )


async def begin_admin_flow(query, context: ContextTypes.DEFAULT_TYPE, flow: dict, prompt: str) -> None:
    context.user_data["admin_flow"] = flow
    await query.answer()
    await admin_screen(
        context,
        query.message.chat_id,
        quote(prompt) + "\n\n" + quote("Send /cancel to stop."),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
        ),
        previous_message=query.message,
    )


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user or not admin_only(update.effective_user.id):
        return False
    flow = context.user_data.get("admin_flow")
    if not flow:
        return False
    value = update.effective_message.text.strip()
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass
    if value == "/cancel":
        context.user_data.pop("admin_flow", None)
        await admin_screen(
            context,
            update.effective_chat.id,
            quote("🔴 <b>ACTION CANCELLED</b>"),
            admin_keyboard(),
        )
        return True
    db = db_from(context)
    kind = flow["kind"]
    try:
        if kind == "channel_id":
            channel_id = int(value)
            chat = await context.bot.get_chat(channel_id)
            context.user_data["admin_flow"] = {
                "kind": "channel_description",
                "channel_id": channel_id,
                "title": chat.title or str(channel_id),
                "username": chat.username or "",
            }
            await admin_screen(
                context,
                update.effective_chat.id,
                quote(f"Found <b>{esc(chat.title or channel_id)}</b>.\nNow send the channel description."),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
                ),
            )
        elif kind == "channel_description":
            doc_id = await db.save_channel(flow["channel_id"], flow["title"], flow["username"], value)
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>CHANNEL SAVED</b>\nNow add plans from the admin panel."),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🟢 Manage plans",
                                callback_data=f"admin:channel_plans:{doc_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🖼 Set channel preview images",
                                callback_data=f"admin:channel_images:{doc_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ Channels", callback_data="admin:channels"
                            )
                        ],
                    ]
                ),
            )
        elif kind == "description":
            await db.update_channel(flow["channel_id"], description=value)
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>DESCRIPTION UPDATED</b>"),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Channel",
                                callback_data=f"admin:channel:{flow['channel_id']}",
                            )
                        ]
                    ]
                ),
            )
        elif kind == "channel_button_text":
            if not 1 <= len(value) <= 50:
                raise ValueError("Button text must be between 1 and 50 characters.")
            await db.update_channel(flow["channel_id"], button_text=value)
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>USER BUTTON TEXT UPDATED</b>"),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Channel",
                                callback_data=f"admin:channel:{flow['channel_id']}",
                            )
                        ]
                    ]
                ),
            )
        elif kind == "contact":
            if not re.match(r"^(?:https?://t\.me/|@?)[A-Za-z0-9_]{3,}$", value):
                raise ValueError("Use an @username or https://t.me/username.")
            global admin_contact_override
            admin_contact_override = value
            await db.set_setting("admin_contact", value)
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>CONTACT LINK UPDATED</b>"),
                admin_keyboard(),
            )
        elif kind == "plan_name":
            context.user_data["admin_flow"] = {**flow, "kind": "plan_days", "name": value}
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("Send duration in days.\nUse <code>0</code> for lifetime."),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
                ),
            )
        elif kind == "plan_days":
            days = int(value)
            if days < 0 or days > 36500:
                raise ValueError("Duration must be between 0 and 36500 days.")
            context.user_data["admin_flow"] = {**flow, "kind": "plan_price", "days": days}
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("Send the price in INR, for example: <code>199</code>.")
                + "\n"
                + quote("Use <code>0</code> to make this a free channel/plan."),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
                ),
            )
        elif kind == "plan_price":
            price = float(value)
            if price < 0 or price > 10_000_000:
                raise ValueError("Price must be zero or greater.")
            context.user_data["admin_flow"] = {**flow, "kind": "plan_description", "price": price}
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("Send a short plan description."),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
                ),
            )
        elif kind == "plan_description":
            plan_id = await db.save_plan(flow["channel_id"], flow["name"], flow["days"], flow["price"], value, flow.get("plan_id"))
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>PLAN SAVED</b>")
                + "\n\n"
                + quote(
                    "It is now visible to users if the channel is active."
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Plan list",
                                callback_data=f"admin:channel_plans:{flow['channel_id']}",
                            )
                        ]
                    ]
                ),
            )
        elif kind == "welcome_text":
            if len(value) > 3500:
                raise ValueError("Welcome text must be under 3500 characters.")
            await db.set_setting("welcome_text", value)
            context.user_data.pop("admin_flow", None)
            await admin_screen(
                context,
                update.effective_chat.id,
                quote("✅ <b>WELCOME MESSAGE UPDATED</b>"),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Welcome setup", callback_data="admin:welcome")]]
                ),
            )
        elif kind in {
            "access_granted_message",
            "access_activated_message",
            "available_channels_message",
        }:
            if len(value) > 3500:
                raise ValueError("Message must be under 3500 characters.")
            setting_key = {
                "access_granted_message": "access_granted_message",
                "access_activated_message": "access_activated_message",
                "available_channels_message": "available_channels_message",
            }[kind]
            await db.set_setting(setting_key, value)
            context.user_data.pop("admin_flow", None)
            if kind == "access_granted_message":
                back_callback = "admin:access_granted_setup"
                confirmation = "ACCESS GRANTED MESSAGE UPDATED"
            elif kind == "access_activated_message":
                back_callback = "admin:access_activated_setup"
                confirmation = "ACCESS ACTIVATED MESSAGE UPDATED"
            else:
                back_callback = "admin:available_channels"
                confirmation = "AVAILABLE CHANNELS MESSAGE UPDATED"
            await admin_screen(
                context,
                update.effective_chat.id,
                quote(f"✅ <b>{confirmation}</b>"),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data=back_callback)]]
                ),
            )
        else:
            return False
    except (ValueError, TelegramError) as exc:
        await admin_screen(
            context,
            update.effective_chat.id,
            quote(f"⚠️ <b>Could not save that:</b> {esc(exc)}")
            + "\n\n"
            + quote("Please send a corrected value or use /cancel."),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")]]
            ),
        )
    return True


async def admin_stats(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    users = await db.count("users")
    channels = await db.count("channels", {"active": True})
    orders = await db.count("orders", {"status": "paid"})
    active = await db.count("subscriptions", {"status": "active"})
    await admin_replace_query(
        query,
        context,
        quote("📊 <b>BOT STATISTICS</b>")
        + "\n\n"
        + quote(f"Users: <b>{users}</b>\nActive channels: <b>{channels}</b>\nPaid orders: <b>{orders}</b>\nActive memberships: <b>{active}</b>"),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Panel", callback_data="admin:menu")]]
        ),
    )


def format_expiry(value) -> str:
    if not value:
        return "Lifetime"
    if isinstance(value, str):
        return value[:19]
    return value.strftime("%d %b %Y, %I:%M %p UTC")


async def admin_premium_users(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    users = await db.list_premium_users()
    buttons = []
    for user in users:
        name = user.get("first_name") or "User"
        username = f" @{user['username']}" if user.get("username") else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    f"👤 {name}{username} • {user['telegram_id']}",
                    callback_data=f"admin:user:{user['telegram_id']}",
                )
            ]
        )
    if not buttons:
        buttons.append(
            [InlineKeyboardButton("📭 No premium users yet", callback_data="admin:menu")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin:menu")])
    await admin_replace_query(
        query,
        context,
        quote("👥 <b>PREMIUM USERS</b>")
        + "\n\n"
        + quote("Every user with a paid or manually granted membership is listed here."),
        InlineKeyboardMarkup(buttons),
    )


async def admin_user_detail(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    db = db_from(context)
    user = await db.get_user(user_id)
    subscriptions = await db.list_user_subscriptions(user_id)
    if not user:
        await query.answer("User not found.", show_alert=True)
        return
    display_name = esc(user.get("first_name") or "User")
    username = f"@{esc(user['username'])}" if user.get("username") else "No username"
    text = (
        quote("👤 <b>PREMIUM USER DETAILS</b>")
        + "\n\n"
        + quote(
            f"Name: <b>{display_name}</b>\nUsername: <code>{username}</code>\nTelegram ID: <code>{user_id}</code>"
        )
    )
    buttons = []
    for sub in subscriptions:
        channel = await db.get_channel(sub.get("channel_doc_id", ""))
        channel_name = channel.get("title") if channel else str(sub.get("channel_id"))
        status = sub.get("status", "unknown")
        text += "\n\n" + quote(
            f"📣 <b>{esc(channel_name)}</b>\n"
            f"Plan: <b>{esc(sub.get('plan_name', 'Membership'))}</b>\n"
            f"Duration: {display_duration(int(sub.get('duration_days', 0)))}\n"
            f"Bought: <code>{format_expiry(sub.get('created_at'))}</code>\n"
            f"Source: <b>{esc(sub.get('source', 'payment'))}</b>\n"
            f"Order: <code>{esc(sub.get('order_id', 'manual'))}</code>\n"
            f"Status: <b>{esc(status)}</b>\n"
            f"Expires: <code>{format_expiry(sub.get('ends_at'))}</code>"
        )
        if status in {"active", "pending_join"}:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔴 Terminate {channel_name[:24]}",
                        callback_data=f"admin:terminate:{sub['_id']}",
                    )
                ]
            )
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Premium users", callback_data="admin:premium_users"
            )
        ]
    )
    await admin_replace_query(query, context, text, InlineKeyboardMarkup(buttons))


async def terminate_subscription(
    application: Application, subscription: dict, notify_user: bool = True
) -> None:
    db: MongoDatabase = application.bot_data["db"]
    if subscription.get("invite_link"):
        try:
            await application.bot.revoke_chat_invite_link(
                subscription["channel_id"], subscription["invite_link"]
            )
        except TelegramError:
            pass
    await db.update_subscription(
        subscription["_id"],
        {"status": "terminated", "terminated_at": utcnow(), "invite_link": None},
    )
    await delete_user_message(
        application.bot,
        subscription["user_id"],
        subscription.get("access_message_id"),
    )
    try:
        await application.bot.ban_chat_member(
            subscription["channel_id"], subscription["user_id"]
        )
        await application.bot.unban_chat_member(
            subscription["channel_id"],
            subscription["user_id"],
            only_if_banned=True,
        )
    except TelegramError as exc:
        log.warning(
            "Could not manually remove user %s from %s: %s",
            subscription["user_id"],
            subscription["channel_id"],
            exc,
        )
    if notify_user:
        try:
            await send_html(
                application.bot,
                subscription["user_id"],
                quote("🔴 <b>MEMBERSHIP TERMINATED</b>")
                + "\n\n"
                + quote("Your access was manually removed by admin."),
                reply_markup=home_keyboard(False),
            )
        except TelegramError:
            pass


async def admin_welcome(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    configured = await db.get_setting("welcome_text")
    photo_id = await db.get_setting("welcome_photo_file_id")
    preview = configured or "Default welcome message"
    photo_state = "Configured" if photo_id else "Not configured"
    buttons = [
        [InlineKeyboardButton("✏️ Edit welcome text", callback_data="admin:welcome_text")],
        [InlineKeyboardButton("🖼 Set welcome photo", callback_data="admin:set_welcome_photo")],
        [InlineKeyboardButton("🗑 Remove welcome photo", callback_data="admin:remove_welcome_photo")],
        [InlineKeyboardButton("⬅️ Manage setup", callback_data="admin:setup")],
    ]
    await admin_replace_query(
        query,
        context,
        quote("🖼 <b>WELCOME / START SETUP</b>")
        + "\n\n"
        + quote(f"Photo: <b>{photo_state}</b>")
        + "\n"
        + quote(f"Text preview:\n<code>{esc(preview[:700])}</code>")
        + "\n\n"
        + quote("Supported placeholders: <code>{mention}</code> and <code>{first_name}</code>. HTML tags such as <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, and <code>&lt;blockquote&gt;</code> are supported."),
        InlineKeyboardMarkup(buttons),
    )


async def admin_setup(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_replace_query(
        query,
        context,
        quote("⚙️ <b>MANAGE SETUP</b>")
        + "\n\n"
        + quote("Configure every user-facing message and image from this menu."),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 Access message setup",
                        callback_data="admin:access_messages",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🛍 Available channels message",
                        callback_data="admin:available_channels",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Welcome / Start setup",
                        callback_data="admin:welcome",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚙️ Contact settings", callback_data="admin:contact"
                    )
                ],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin:menu")],
            ]
        ),
    )


async def admin_access_messages(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_replace_query(
        query,
        context,
        quote("💳 <b>ACCESS MESSAGE SETUP</b>")
        + "\n\n"
        + quote("Choose which access message you want to customize."),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Access granted message",
                        callback_data="admin:access_granted_setup",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🟢 Access activated message",
                        callback_data="admin:access_activated_setup",
                        style="success",
                    )
                ],
                [InlineKeyboardButton("⬅️ Manage setup", callback_data="admin:setup")],
            ]
        ),
    )


async def admin_access_message_setup(
    query, context: ContextTypes.DEFAULT_TYPE, kind: str
) -> None:
    db = db_from(context)
    if kind == "granted":
        image_key = "access_granted_photo_file_id"
        text_key = "access_granted_message"
        title = "ACCESS GRANTED MESSAGE"
        set_callback = "admin:set_access_granted"
        remove_callback = "admin:remove_access_granted"
        edit_callback = "admin:edit_access_granted_message"
        default = DEFAULT_ACCESS_GRANTED_MESSAGE
    else:
        image_key = "access_activated_photo_file_id"
        text_key = "access_activated_message"
        title = "ACCESS ACTIVATED MESSAGE"
        set_callback = "admin:set_access_activated"
        remove_callback = "admin:remove_access_activated"
        edit_callback = "admin:edit_access_activated_message"
        default = DEFAULT_ACCESS_ACTIVATED_MESSAGE
    image = await db.get_setting(image_key)
    configured = await db.get_setting(text_key)
    await admin_replace_query(
        query,
        context,
        quote(f"💳 <b>{title}</b>")
        + "\n\n"
        + quote(
            f"Image: <b>{'Configured' if image else 'Default text'}</b>\n"
            f"Message: <b>{'Custom' if configured else 'Default'}</b>"
        )
        + "\n\n"
        + quote(f"Current message preview:\n{(configured or default)[:700]}")
        + "\n\n"
        + quote(
            "HTML is supported. Placeholders: {mention}, {first_name}, "
            "{channel_name}, {plan_name}, {expiry}, and {order_id}."
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🖼 Set image", callback_data=set_callback)],
                [InlineKeyboardButton("🗑 Remove image", callback_data=remove_callback)],
                [InlineKeyboardButton("✏️ Edit message", callback_data=edit_callback)],
                [InlineKeyboardButton("⬅️ Access messages", callback_data="admin:access_messages")],
            ]
        ),
    )


async def admin_available_channels(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = db_from(context)
    image = await db.get_setting("available_channels_photo_file_id")
    configured = await db.get_setting("available_channels_message")
    await admin_replace_query(
        query,
        context,
        quote("🛍 <b>AVAILABLE CHANNELS MESSAGE</b>")
        + "\n\n"
        + quote(
            f"Image: <b>{'Configured' if image else 'Default text'}</b>\n"
            f"Message: <b>{'Custom' if configured else 'Default'}</b>"
        )
        + "\n\n"
        + quote(
            "This message appears when the user taps buy membership. "
            "HTML formatting is supported."
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🖼 Add image",
                        callback_data="admin:set_available_channels_image",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Remove image",
                        callback_data="admin:remove_available_channels_image",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Edit message",
                        callback_data="admin:edit_available_channels_message",
                    )
                ],
                [InlineKeyboardButton("⬅️ Manage setup", callback_data="admin:setup")],
            ]
        ),
    )


async def finish_channel_images(
    query, context: ContextTypes.DEFAULT_TYPE, channel_id: str
) -> None:
    db = db_from(context)
    flow = context.user_data.pop("admin_flow", {})
    images = flow.get("images", [])
    await db.update_channel(channel_id, preview_image_file_ids=images[:10])
    await admin_replace_query(
        query,
        context,
        quote("✅ <b>CHANNEL PREVIEW IMAGES UPDATED</b>")
        + "\n\n"
        + quote(
            f"{len(images[:10])} image(s) will be shown before the channel description and plans."
        ),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Channel", callback_data=f"admin:channel:{channel_id}")]]
        ),
    )


async def admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user or not admin_only(update.effective_user.id):
        return False
    flow = context.user_data.get("admin_flow")
    if not flow or flow.get("kind") not in {
        "welcome_photo",
        "channel_images",
        "access_granted_photo",
        "access_activated_photo",
        "available_channels_photo",
    }:
        return False
    file_id = update.effective_message.photo[-1].file_id
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass
    kind = flow["kind"]
    if kind == "channel_images":
        images = [*flow.get("images", []), file_id][:10]
        context.user_data["admin_flow"] = {**flow, "images": images}
        await admin_screen(
            context,
            update.effective_chat.id,
            quote(f"✅ <b>PREVIEW IMAGE {len(images)}/10 ADDED</b>")
            + "\n\n"
            + quote("Send another photo or tap Done to save this gallery."),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🟢 Done",
                            callback_data=f"admin:channel_images_done:{flow['channel_id']}",
                        )
                    ],
                    [InlineKeyboardButton("🔴 Cancel", callback_data="admin:cancel_flow")],
                ]
            ),
        )
        return True

    setting_key = {
        "welcome_photo": "welcome_photo_file_id",
        "access_granted_photo": "access_granted_photo_file_id",
        "access_activated_photo": "access_activated_photo_file_id",
        "available_channels_photo": "available_channels_photo_file_id",
    }[kind]
    await db_from(context).set_setting(setting_key, file_id)
    context.user_data.pop("admin_flow", None)
    if kind == "welcome_photo":
        back_callback = "admin:welcome"
        confirmation = "WELCOME PHOTO UPDATED"
    elif kind == "access_granted_photo":
        back_callback = "admin:access_granted_setup"
        confirmation = "ACCESS GRANTED IMAGE UPDATED"
    elif kind == "access_activated_photo":
        back_callback = "admin:access_activated_setup"
        confirmation = "ACCESS ACTIVATED IMAGE UPDATED"
    else:
        back_callback = "admin:available_channels"
        confirmation = "AVAILABLE CHANNELS IMAGE UPDATED"
    await admin_screen(
        context,
        update.effective_chat.id,
        quote(f"✅ <b>{confirmation}</b>"),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=back_callback)]]
        ),
    )
    return True


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    db = db_from(context)
    if data.startswith("admin:") and not admin_only(query.from_user.id):
        await query.answer("Admin access only.", show_alert=True)
        return
    if data == "home":
        await query.answer()
        await replace_query_message(
            query,
            context,
            quote("🏠 <b>MEMBERSHIP HOME</b>\nChoose an option below."),
            home_keyboard(admin_only(query.from_user.id)),
        )
    elif data == "browse":
        await query.answer()
        await show_channels(query, context, db)
    elif data == "my_access":
        await query.answer()
        await show_access(query, context)
    elif data == "contact":
        url = contact_url()
        await query.answer(
            "Admin contact is not configured." if not url else "Use the contact button.",
            show_alert=not bool(url),
        )
    elif data.startswith("channel:"):
        await query.answer()
        await show_channel(query, context, db, data.split(":", 1)[1])
    elif data.startswith("plan:"):
        _, channel_id, plan_id = data.split(":")
        await create_payment(update, context, channel_id, plan_id)
    elif data.startswith("paycheck:"):
        await check_payment(query, context, data.split(":", 1)[1])
    elif data.startswith("paycancel:"):
        await cancel_payment(query, context, data.split(":", 1)[1])
    elif data == "admin:menu":
        await admin_menu(query, context)
    elif data == "admin:channels":
        await admin_channels(query, context)
    elif data == "admin:plans":
        await admin_plans(query, context)
    elif data == "admin:premium_users":
        await admin_premium_users(query, context)
    elif data == "admin:setup":
        await admin_setup(query, context)
    elif data.startswith("admin:user:"):
        await admin_user_detail(query, context, int(data.split(":")[-1]))
    elif data.startswith("admin:terminate:"):
        subscription = await db.get_subscription(data.split(":")[-1])
        if not subscription:
            await query.answer("Membership not found.", show_alert=True)
            return
        await terminate_subscription(context.application, subscription)
        await admin_user_detail(query, context, subscription["user_id"])
    elif data == "admin:welcome":
        await admin_welcome(query, context)
    elif data == "admin:access_messages":
        await admin_access_messages(query, context)
    elif data == "admin:access_granted_setup":
        await admin_access_message_setup(query, context, "granted")
    elif data == "admin:access_activated_setup":
        await admin_access_message_setup(query, context, "activated")
    elif data == "admin:available_channels":
        await admin_available_channels(query, context)
    elif data == "admin:welcome_text":
        await begin_admin_flow(
            query,
            context,
            {"kind": "welcome_text"},
            "Send the new /start message. Use HTML formatting and {mention} where the user name should appear.",
        )
    elif data == "admin:set_welcome_photo":
        await begin_admin_flow(
            query,
            context,
            {"kind": "welcome_photo"},
            "Send the photo to use with the /start message.",
        )
    elif data == "admin:set_access_granted":
        await begin_admin_flow(
            query,
            context,
            {"kind": "access_granted_photo"},
            "Send the image to use when access is granted and the invite link is sent.",
        )
    elif data == "admin:set_access_activated":
        await begin_admin_flow(
            query,
            context,
            {"kind": "access_activated_photo"},
            "Send the image to use after the user joins and access is activated.",
        )
    elif data == "admin:set_available_channels_image":
        await begin_admin_flow(
            query,
            context,
            {"kind": "available_channels_photo"},
            "Send the image to show with the buy membership message.",
        )
    elif data == "admin:edit_access_granted_message":
        await begin_admin_flow(
            query,
            context,
            {"kind": "access_granted_message"},
            "Send the new access granted message.\n\n"
            "HTML is supported. Available placeholders:\n"
            "{mention}, {first_name}, {channel_name}, {plan_name}, {expiry}, {order_id}\n\n"
            "Example: <b>Welcome {mention}</b>\nYour <b>{plan_name}</b> access to "
            "<b>{channel_name}</b> is ready until <code>{expiry}</code>.",
        )
    elif data == "admin:edit_access_activated_message":
        await begin_admin_flow(
            query,
            context,
            {"kind": "access_activated_message"},
            "Send the new access activated message.\n\n"
            "HTML is supported. Available placeholders:\n"
            "{mention}, {first_name}, {channel_name}, {plan_name}, {expiry}, {order_id}\n\n"
            "This message is sent after the user joins the channel.",
        )
    elif data == "admin:edit_available_channels_message":
        await begin_admin_flow(
            query,
            context,
            {"kind": "available_channels_message"},
            "Send the buy membership message.\n\n"
            "HTML and <blockquote> are supported. This message appears above "
            "the available channel buttons.",
        )
    elif data == "admin:remove_welcome_photo":
        await db.set_setting("welcome_photo_file_id", None)
        await admin_welcome(query, context)
    elif data == "admin:remove_access_granted":
        await db.set_setting("access_granted_photo_file_id", None)
        await admin_access_message_setup(query, context, "granted")
    elif data == "admin:remove_access_activated":
        await db.set_setting("access_activated_photo_file_id", None)
        await admin_access_message_setup(query, context, "activated")
    elif data == "admin:remove_available_channels_image":
        await db.set_setting("available_channels_photo_file_id", None)
        await admin_available_channels(query, context)
    elif data == "admin:cancel_flow":
        context.user_data.pop("admin_flow", None)
        await admin_menu(query, context)
    elif data == "admin:add_channel":
        await begin_admin_flow(query, context, {"kind": "channel_id"}, "Send the Telegram channel ID, for example <code>-1001234567890</code>.")
    elif data.startswith("admin:channel:"):
        await admin_channel(query, context, data.split(":")[-1])
    elif data.startswith("admin:edit_desc:"):
        await begin_admin_flow(query, context, {"kind": "description", "channel_id": data.split(":")[-1]}, "Send the new channel description.")
    elif data.startswith("admin:edit_button:"):
        channel_id = data.split(":")[-1]
        channel = await db.get_channel(channel_id)
        if not channel:
            await query.answer("Channel not found.", show_alert=True)
            return
        await begin_admin_flow(
            query,
            context,
            {"kind": "channel_button_text", "channel_id": channel_id},
            "Send the text users should see in the buy membership list.\n"
            f"Current: <b>{esc(channel.get('button_text') or channel.get('title', 'Channel'))}</b>\n"
            "Use up to 50 characters.",
        )
    elif data.startswith("admin:channel_images:"):
        channel_id = data.split(":")[-1]
        channel = await db.get_channel(channel_id)
        if not channel:
            await query.answer("Channel not found.", show_alert=True)
            return
        await begin_admin_flow(
            query,
            context,
            {"kind": "channel_images", "channel_id": channel_id, "images": []},
            "Send one or more channel preview photos, one at a time.\n"
            "This replaces the current gallery. You can add up to 10 images, then tap Done.",
        )
    elif data.startswith("admin:channel_images_done:"):
        await finish_channel_images(query, context, data.split(":")[-1])
    elif data.startswith("admin:toggle_channel:"):
        channel_id = data.split(":")[-1]
        channel = await db.get_channel(channel_id)
        if not channel:
            await query.answer("Channel not found.", show_alert=True)
            return
        await db.update_channel(channel_id, active=not channel.get("active", True))
        await admin_channel(query, context, channel_id)
    elif data.startswith("admin:delete_channel_confirm:"):
        await admin_delete_channel_confirm(query, context, data.split(":")[-1])
    elif data.startswith("admin:delete_channel:"):
        channel_id = data.split(":")[-1]
        channel = await db.get_channel(channel_id)
        if not channel:
            await query.answer("Channel already removed.", show_alert=True)
            return
        await db.delete_channel(channel_id)
        await admin_channels(query, context)
    elif data.startswith("admin:channel_plans:"):
        await admin_channel_plans(query, context, data.split(":")[-1])
    elif data.startswith("admin:add_plan:"):
        await begin_admin_flow(query, context, {"kind": "plan_name", "channel_id": data.split(":")[-1]}, "Send the plan name, for example <b>7 Days</b>.")
    elif data.startswith("admin:edit_plan:"):
        _, _, channel_id, plan_id = data.split(":")
        plan = await db.get_plan(plan_id)
        if not plan:
            await query.answer("Plan not found.", show_alert=True)
            return
        await begin_admin_flow(query, context, {"kind": "plan_name", "channel_id": channel_id, "plan_id": plan_id}, f"Send the new plan name.\nCurrent: <b>{esc(plan['name'])}</b>")
    elif data == "admin:contact":
        await begin_admin_flow(query, context, {"kind": "contact"}, "Send admin contact as @username or https://t.me/username.")
    elif data == "admin:stats":
        await admin_stats(query, context)
    elif data.startswith("renew:"):
        await query.answer()
        await show_channel(query, context, db, data.split(":", 1)[1])
    else:
        await query.answer()


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event: ChatMemberUpdated = update.chat_member
    if not event or event.new_chat_member.status not in ACTIVE_MEMBER_STATUSES:
        return
    if event.old_chat_member.status in ACTIVE_MEMBER_STATUSES:
        return
    db = db_from(context)
    user_id = event.new_chat_member.user.id
    sub = await db.find_pending_subscription(user_id, event.chat.id)
    if not sub:
        return
    if sub.get("invite_link"):
        try:
            await context.bot.revoke_chat_invite_link(event.chat.id, sub["invite_link"])
        except TelegramError:
            pass
    now = utcnow()
    duration = int(sub.get("duration_days", 0))
    end = None if duration <= 0 else now + timedelta(days=duration)
    if not await db.claim_pending_join(sub["_id"], now, end):
        # A duplicate Telegram update was already handled by another worker.
        return
    # Remove the earlier "access granted" message with the invite before
    # sending the final activation confirmation.
    await delete_user_message(
        context.bot, user_id, sub.get("access_message_id")
    )
    activated_message = await send_screen(
        context.application,
        user_id,
        await personalized_message(
            db,
            user_id,
            "access_activated_message",
            DEFAULT_ACCESS_ACTIVATED_MESSAGE,
            channel_name=sub.get("channel_name", str(sub.get("channel_id"))),
            plan_name=sub.get("plan_name", "Membership"),
            expiry=format_expiry(end),
            order_id=sub.get("order_id", ""),
        ),
        "access_activated_photo_file_id",
        reply_markup=home_keyboard(False),
    )
    await db.update_subscription(
        sub["_id"], {"access_message_id": activated_message.message_id}
    )


async def maintenance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: MongoDatabase = context.application.bot_data["db"]
    now = utcnow()
    for sub in await db.due_reminders(now + timedelta(hours=24)):
        await db.update_subscription(sub["_id"], {"reminder_sent": True})
        try:
            await send_html(
                context.bot,
                sub["user_id"],
                quote("⏳ <b>MEMBERSHIP EXPIRING SOON</b>")
                + "\n\n"
                + quote(f"Your <b>{esc(sub.get('plan_name'))}</b> membership expires soon.")
                + "\n"
                + quote("Renew now to keep your access."),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🟢 Renew", callback_data=f"renew:{sub['channel_doc_id']}")],
                        [InlineKeyboardButton("🔴 Cancel", callback_data="home")],
                    ]
                ),
            )
        except TelegramError:
            pass
    for sub in await db.active_expiring(now):
        await db.update_subscription(sub["_id"], {"status": "expired"})
        try:
            await context.bot.ban_chat_member(sub["channel_id"], sub["user_id"])
            await context.bot.unban_chat_member(sub["channel_id"], sub["user_id"], only_if_banned=True)
        except TelegramError as exc:
            log.warning("Could not remove expired member %s: %s", sub["user_id"], exc)
        try:
            await send_html(
                context.bot,
                sub["user_id"],
                quote("🔴 <b>MEMBERSHIP EXPIRED</b>\nYour access has been removed."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 Renew", callback_data=f"renew:{sub['channel_doc_id']}")]]),
            )
        except TelegramError:
            pass


async def manual_add_premium(
    application: Application, channel_id: int, user_id: int, duration_days: int
) -> str:
    db: MongoDatabase = application.bot_data["db"]
    channel = await db.get_channel_by_telegram_id(channel_id)
    if not channel:
        return "That channel is not configured in the admin panel."
    if duration_days < 0 or duration_days > 36500:
        return "Duration must be between 0 and 36500 days. Use 0 for lifetime."

    try:
        telegram_user = await application.bot.get_chat(user_id)
        await db.upsert_user(telegram_user)
        user_label = telegram_user.first_name or str(user_id)
    except TelegramError:
        existing = await db.get_user(user_id)
        if not existing:
            await db.db.users.update_one(
                {"telegram_id": user_id},
                {
                    "$set": {
                        "telegram_id": user_id,
                        "first_name": "",
                        "created_at": utcnow(),
                        "updated_at": utcnow(),
                    }
                },
                upsert=True,
            )
        user_label = str(user_id)

    now = utcnow()
    current = await db.find_subscription(user_id, channel_id, active_only=True)
    already_member = await member_now(application.bot, user_id, channel_id)
    end = None if duration_days == 0 else now + timedelta(days=duration_days)

    if current and already_member:
        start = max(now, current.get("ends_at") or now)
        extended_end = None if duration_days == 0 else start + timedelta(days=duration_days)
        await db.update_subscription(
            current["_id"],
            {
                "ends_at": extended_end,
                "status": "active",
                "reminder_sent": False,
                "source": "manual",
                "last_manual_grant_at": now,
            },
        )
        return f"Extended {user_label} in {channel['title']} until {format_expiry(extended_end)}."

    if already_member:
        await db.create_subscription(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "channel_doc_id": channel["_id"],
                "plan_id": "manual",
                "plan_name": "Manual premium",
                "duration_days": duration_days,
                "starts_at": now,
                "ends_at": end,
                "status": "active",
                "source": "manual",
                "reminder_sent": False,
            }
        )
        return f"Added {user_label} to {channel['title']} until {format_expiry(end)}."

    try:
        invite = await application.bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"MANUAL-{new_id()[:8].upper()}",
            expire_date=now + timedelta(minutes=30),
            member_limit=1,
        )
    except TelegramError as exc:
        return f"Membership was recorded, but Telegram could not create an invite: {exc}"

    subscription_id = await db.create_subscription(
        {
            "user_id": user_id,
            "channel_id": channel_id,
            "channel_doc_id": channel["_id"],
            "channel_name": channel.get("title", "Channel"),
            "plan_id": "manual",
            "plan_name": "Manual premium",
            "duration_days": duration_days,
            "starts_at": now,
            "ends_at": end,
            "status": "pending_join",
            "source": "manual",
            "invite_link": invite.invite_link,
            "reminder_sent": False,
        }
    )
    try:
        access_message = await send_screen(
            application,
            user_id,
            quote("✅ <b>PREMIUM ACCESS GRANTED</b>")
            + "\n\n"
            + quote(f"Channel: <b>{esc(channel['title'])}</b>")
            + "\n"
            + quote(
                f"Access: <code>{format_expiry(end)}</code>\nJoin using the single-use invite below."
            ),
            "access_granted_photo_file_id",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔵 Join channel", url=invite.invite_link)]]
            ),
        )
        await db.update_subscription(
            subscription_id, {"access_message_id": access_message.message_id}
        )
        return f"Invite sent to {user_label} for {channel['title']}."
    except TelegramError:
        return f"Membership recorded and invite created, but I could not message user {user_id}."


async def manual_remove_premium(
    application: Application, channel_id: int, user_id: int
) -> str:
    db: MongoDatabase = application.bot_data["db"]
    records = await db.terminate_user_subscriptions(user_id, channel_id)
    if not records:
        return "No active or pending premium membership was found for that user and channel."
    for record in records:
        await terminate_subscription(application, record)
    return f"Terminated {len(records)} membership record(s) and removed user {user_id}."


async def addpremium_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not admin_only(update.effective_user.id):
        return
    args = context.args
    try:
        if len(args) != 3:
            raise ValueError("Usage: /addpremium <channel_id> <user_id> <duration_days>")
        result = await manual_add_premium(
            context.application, int(args[0]), int(args[1]), int(args[2])
        )
    except ValueError as exc:
        result = str(exc)
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass
    await send_html(context.bot, update.effective_chat.id, quote(f"🟢 {esc(result)}"))


async def removepremium_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not admin_only(update.effective_user.id):
        return
    args = context.args
    try:
        if len(args) != 2:
            raise ValueError("Usage: /removepremium <channel_id> <user_id>")
        result = await manual_remove_premium(
            context.application, int(args[0]), int(args[1])
        )
    except ValueError as exc:
        result = str(exc)
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass
    await send_html(context.bot, update.effective_chat.id, quote(f"🔴 {esc(result)}"))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        quote("ℹ️ <b>HELP</b>")
        + "\n\n"
        + quote("Use /start to buy membership and memberships.")
        + "\n"
        + quote("If a payment is pending, use the check button on its payment screen.")
    )
    await update.effective_message.reply_text(
        small_caps_html(text),
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(admin_only(update.effective_user.id)),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("admin_flow", None)
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass
    if admin_only(update.effective_user.id):
        await admin_screen(
            context,
            update.effective_chat.id,
            quote("🔴 <b>ACTION CANCELLED</b>"),
            admin_keyboard(),
        )


async def post_init(application: Application) -> None:
    global admin_contact_override
    db = MongoDatabase()
    await db.connect()
    application.bot_data["db"] = db
    application.bot_data["payment_tasks"] = {}
    saved_contact = await db.get_setting("admin_contact")
    if saved_contact:
        admin_contact_override = saved_contact
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open the membership menu"),
            BotCommand("help", "Show help"),
            BotCommand("cancel", "Cancel the current action"),
            BotCommand("addpremium", "Admin: grant premium access"),
            BotCommand("removepremium", "Admin: remove premium access"),
        ]
    )
    application.job_queue.run_repeating(maintenance_job, interval=300, first=20)
    log.info("MongoDB connected and maintenance jobs started")


async def post_shutdown(application: Application) -> None:
    for task in application.bot_data.get("payment_tasks", {}).values():
        task.cancel()
    db = application.bot_data.get("db")
    if db:
        await db.close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled update error", exc_info=context.error)


def build_application() -> Application:
    settings.validate()
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(32)
        .connection_pool_size(64)
        .pool_timeout(10)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("addpremium", addpremium_command))
    app.add_handler(CommandHandler("removepremium", removepremium_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO, admin_photo), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text), group=0)
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    application = build_application()
    log.info("Starting Telegram polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
