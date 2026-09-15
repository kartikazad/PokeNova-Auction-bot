"""
PokeNova Auction Bot
---------------
A full-featured Telegram auction bot for Pokémon items (Pokémon with
nature/IVs, TMs, mega stones, key items, etc.), backed by MongoDB.

See README.md for setup, and branding/BRANDING_AND_SETUP.md for the full
command reference and BotFather configuration.
"""

import logging
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

_raw_admin_usernames = os.environ.get("ADMIN_USERNAMES", "")
SEED_ADMIN_USERNAMES = {
    username.strip().lstrip("@").lower() 
    for username in _raw_admin_usernames.split(",") 
    if username.strip()
}

AUCTION_GROUP_ID = os.environ.get("AUCTION_GROUP_ID")
AUCTION_GROUP_ID = int(AUCTION_GROUP_ID) if AUCTION_GROUP_ID else None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

NATURES = [
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
]

IV_PATTERN = re.compile(
    r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$"
)
IV_STAT_NAMES = ["HP", "Atk", "Def", "SpA", "SpD", "Spe"]

_NAME_LINE_PATTERN = re.compile(r"^[^\w]*([A-Za-z][A-Za-z0-9\-]*)")
_NATURE_PATTERN = re.compile(r"Nature:\s*([A-Za-z]+)", re.IGNORECASE)
_IV_STAT_PATTERNS = {
    "HP": re.compile(r"\bHP\s+(\d{1,3})"),
    "Atk": re.compile(r"\bAttack\s+(\d{1,3})"),
    "Def": re.compile(r"\bDefense\s+(\d{1,3})"),
    "SpA": re.compile(r"Sp\.?\s*Atk\s+(\d{1,3})", re.IGNORECASE),
    "SpD": re.compile(r"Sp\.?\s*Def\s+(\d{1,3})", re.IGNORECASE),
    "Spe": re.compile(r"\bSpeed\s+(\d{1,3})"),
}

BIDDING_RULES_TEXT = (
    "📜 <b>Bidding Rules</b>\n\n"
    "1. You must be verified (/get_verified) before bidding or listing items.\n"
    "2. Each new bid must be strictly higher than the current bid.\n"
    "3. Bids are binding — don't bid if you don't intend to pay.\n"
    "4. Admins may cancel bids that violate these rules (/reverse, /reset).\n"
    "5. Once an auction ends, the winner has 24 hours to complete the trade "
    "with the seller unless stated otherwise.\n"
    "6. Be respectful — harassment or scamming will result in a ban.\n\n"
    "Contact an admin via /report if you have a dispute."
)


# --------------------------------------------------------------------------
# Conversation states
# --------------------------------------------------------------------------

(
    CHOOSING_TYPE,
    POKEMON_METHOD,
    IMPORT_STATS_MSG,
    IMPORT_IVEV_MSG,
    POKEMON_NAME,
    POKEMON_NATURE,
    POKEMON_IVS,
    POKEMON_EXTRA,
    TM_NAME,
) = range(9)


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def format_money(amount) -> str:
    if amount is None:
        return "0"
    amount = float(amount)
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}"


async def reply(update: Update, text: str, reply_markup=None) -> None:
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
    )


def get_message_text(update: Update) -> str:
    msg = update.message
    return (msg.text or msg.caption or "").strip()


def user_display(doc: dict) -> str:
    if not doc:
        return "Unknown"
    if doc.get("username"):
        return f"{doc['name']} (@{doc['username']})"
    return doc["name"]


async def ensure_user(update: Update) -> dict:
    user = update.effective_user
    return await db.get_or_create_user(user.id, user.full_name, user.username)


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


async def is_admin(user_id: int, username: str | None = None) -> bool:
    if is_owner(user_id):
        return True
    if username and username.lower() in SEED_ADMIN_USERNAMES:
        return True
    doc = await db.get_user(user_id)

    return bool(
        doc and (
            doc.get("is_admin")
            or doc.get("is_super_admin")
        )
    )

async def is_super_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    doc = await db.get_user(user_id)
    return bool(doc and doc.get("is_super_admin"))


async def require_admin(update: Update) -> bool:
    user = update.effective_user

    if not await is_admin(user.id, user.username):
        await reply(update, "🚫 This command is for admins only.")
        return False

    return True

async def require_super_admin(update: Update) -> bool:
    if not await is_super_admin(update.effective_user.id):
        await reply(update, "🚫 This command is for super admins/owner only.")
        return False
    return True


async def require_owner(update: Update) -> bool:
    if not is_owner(update.effective_user.id):
        await reply(update, "🚫 This command is for the bot owner only.")
        return False
    return True




async def require_not_banned(update: Update) -> bool:
    doc = await db.get_user(update.effective_user.id)
    if doc and doc.get("is_banned"):
        await reply(update, "🚫 You are banned from using this bot.")
        return False
    return True


async def require_verified(update: Update) -> bool:
    doc = await ensure_user(update)
    if not doc.get("is_verified"):
        await reply(
            update,
            "🔒 You need to be verified first. Run /get_verified in the "
            "auction group to request verification.",
        )
        return False
    return True


def nature_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, nature in enumerate(NATURES, start=1):
        row.append(InlineKeyboardButton(nature, callback_data=f"nature:{nature}"))
        if i % 5 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🐉 Pokémon", callback_data="type:pokemon"),
            InlineKeyboardButton("💿 TM", callback_data="type:tm"),
        ]]
    )


def build_item_summary(data: dict) -> str:
    if data["item_type"] == "pokemon":
        lines = [f"🐉 <b>{data['name']}</b>"]
        if data.get("nature"):
            lines.append(f"Nature: <b>{data['nature']}</b>")
        if data.get("ivs"):
            iv_line = " / ".join(
                f"{stat} {val}" for stat, val in zip(IV_STAT_NAMES, data["ivs"])
            )
            lines.append(f"IVs: {iv_line}")
        if data.get("extra"):
            lines.append(f"Extra: {data['extra']}")
        return "\n".join(lines)
    else:
        lines = [f"💿 <b>TM: {data['name']}</b>"]
        if data.get("extra"):
            lines.append(f"Extra: {data['extra']}")
        return "\n".join(lines)


def parse_name_and_nature(text: str):
    name, nature = None, None
    lines = [line for line in text.splitlines() if line.strip()]
    if lines:
        match = _NAME_LINE_PATTERN.search(lines[0])
        if match:
            name = match.group(1)
    nature_match = _NATURE_PATTERN.search(text)
    if nature_match:
        nature = nature_match.group(1).capitalize()
    return name, nature


def parse_ivs_from_text(text: str):
    ivs = []
    for stat in IV_STAT_NAMES:
        match = _IV_STAT_PATTERNS[stat].search(text)
        if not match:
            return None
        ivs.append(int(match.group(1)))
    return ivs


# --------------------------------------------------------------------------
# /add — guided item submission
# --------------------------------------------------------------------------

async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await require_not_banned(update):
        return ConversationHandler.END
    user_doc = await ensure_user(update)
    if not user_doc.get("is_verified"):
        await reply(
            update,
            "🔒 You need to be verified before submitting items. Run "
            "/get_verified in the auction group first.",
        )
        return ConversationHandler.END

    config = await db.get_config()
    if not config.get("submission_enabled", True):
        await reply(update, "🚫 Item submissions are currently closed by an admin.")
        return ConversationHandler.END

    context.user_data["new_item"] = {}
    await reply(update, "🎬 Let's add a new item.\n\nWhat are you submitting?", reply_markup=type_keyboard())
    return CHOOSING_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item_type = query.data.split(":", 1)[1]
    context.user_data["new_item"]["item_type"] = item_type

    if item_type == "pokemon":
        await query.edit_message_text(
            "How do you want to add this Pokémon's details?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Import from PokéNova (forward messages)", callback_data="method:import")],
                [InlineKeyboardButton("✍️ Enter manually", callback_data="method:manual")],
            ]),
        )
        return POKEMON_METHOD
    else:
        await query.edit_message_text("💿 What's the TM/move name?")
        return TM_NAME


async def choose_pokemon_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":", 1)[1]

    if method == "import":
        await query.edit_message_text(
            "📥 <b>Importing from PokéNova</b>\n\n"
            "1️⃣ Run <code>/stats &lt;pokemon&gt;</code> on PokéNova, then forward "
            "me <b>that reply</b> (the one with name, level, and nature).",
            parse_mode=ParseMode.HTML,
        )
        return IMPORT_STATS_MSG
    else:
        await query.edit_message_text("🐉 What's the Pokémon's name?")
        return POKEMON_NAME


async def import_stats_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = get_message_text(update)
    name, nature = parse_name_and_nature(text)
    if not name or not nature:
        await reply(
            update,
            "❗ I couldn't find a name and nature in that message. Forward the "
            "<code>/stats</code> reply itself, or /cancel to try manual entry.",
        )
        return IMPORT_STATS_MSG

    context.user_data["new_item"]["name"] = name
    context.user_data["new_item"]["nature"] = nature
    await reply(
        update,
        f"✅ Got it: <b>{name}</b>, Nature: <b>{nature}</b>\n\n"
        "2️⃣ Now tap the <b>IV/EV</b> button on that PokéNova card, and forward "
        "me <b>that</b> reply too.",
    )
    return IMPORT_IVEV_MSG


async def import_ivev_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = get_message_text(update)
    ivs = parse_ivs_from_text(text)
    if ivs is None:
        await reply(
            update,
            "❗ I couldn't find IV values in that message. Forward the IV/EV "
            "reply, or /cancel to try manual entry.",
        )
        return IMPORT_IVEV_MSG

    context.user_data["new_item"]["ivs"] = ivs
    iv_line = " / ".join(f"{s} {v}" for s, v in zip(IV_STAT_NAMES, ivs))
    await reply(
        update,
        f"✅ IVs imported: {iv_line}\n\n"
        "Any extra notes? (held item, mega stone, EVs, tutor moves, etc.)\n"
        "Send them now, or send <code>skip</code>.",
    )
    return POKEMON_EXTRA


async def pokemon_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await reply(update, "❗ Please send a valid name.")
        return POKEMON_NAME
    context.user_data["new_item"]["name"] = name
    await reply(update, "🎲 Pick the nature:", reply_markup=nature_keyboard())
    return POKEMON_NATURE


async def pokemon_nature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    nature = query.data.split(":", 1)[1]
    context.user_data["new_item"]["nature"] = nature
    await query.edit_message_text(
        f"Nature set to <b>{nature}</b>.\n\nNow send the IVs as "
        "<code>HP/Atk/Def/SpA/SpD/Spe</code>, e.g. <code>31/31/31/31/31/31</code>",
        parse_mode=ParseMode.HTML,
    )
    return POKEMON_IVS


async def pokemon_ivs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    match = IV_PATTERN.match(text)
    if not match:
        await reply(update, "❗ Use the format <code>HP/Atk/Def/SpA/SpD/Spe</code>.")
        return POKEMON_IVS
    ivs = [int(v) for v in match.groups()]
    if any(v < 0 or v > 31 for v in ivs):
        await reply(update, "❗ Each IV must be between 0 and 31.")
        return POKEMON_IVS
    context.user_data["new_item"]["ivs"] = ivs
    await reply(
        update,
        "✅ IVs saved.\n\nAny extra notes? (held item, mega stone, EVs, tutor "
        "moves, etc.) Send them now, or send <code>skip</code>.",
    )
    return POKEMON_EXTRA


async def tm_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await reply(update, "❗ Please send a valid TM/move name.")
        return TM_NAME
    context.user_data["new_item"]["name"] = name
    await reply(update, "Any extra notes about this TM? Send them, or send <code>skip</code>.")
    return POKEMON_EXTRA


async def pokemon_extra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    item_data = context.user_data["new_item"]
    item_data["extra"] = "" if text.lower() == "skip" else text

    user = update.effective_user
    item_data["submitted_by"] = user.id
    item_data["submitted_by_name"] = user.full_name

    item_id = await db.create_item(item_data)
    await db.log_action("item_submitted", user.id, user.full_name, f"item #{item_id}: {item_data['name']}")

    await reply(
        update,
        f"✅ <b>Item #{item_id} submitted!</b>\n\n"
        f"{build_item_summary(item_data)}\n\n"
        "An admin will review it and start the auction with "
        f"<code>/auction {item_id} &lt;starting bid&gt;</code>.",
    )
    context.user_data.pop("new_item", None)
    return ConversationHandler.END


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_item", None)
    await reply(update, "🛑 Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Auction lifecycle: /auction, /bid, /endauction, /delete, /reverse, /reset
# --------------------------------------------------------------------------

async def auction_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    if len(context.args) < 2:
        await reply(update, "❗ Usage: <code>/auction &lt;item_id&gt; &lt;starting_bid&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
        starting_bid = float(context.args[1])
    except ValueError:
        await reply(update, "❗ item_id and starting_bid must be numbers.")
        return

    item = await db.get_item(item_id)
    if item is None:
        await reply(update, f"❗ No item found with ID {item_id}.")
        return
    if item["status"] == "active":
        await reply(update, f"⚠️ Item #{item_id} is already active.")
        return
    if item["status"] in ("sold", "cancelled"):
        await reply(update, f"⚠️ Item #{item_id} is already {item['status']}.")
        return

    await db.update_item(
        item_id,
        status="active",
        starting_bid=starting_bid,
        current_bid=starting_bid,
        current_bidder_id=None,
        current_bidder_name=None,
        chat_id=update.effective_chat.id,
        started_at=db.now(),
    )
    await db.log_action("auction_started", update.effective_user.id, update.effective_user.full_name, f"item #{item_id}")

    await reply(
        update,
        f"🎉 <b>Auction Started — Item #{item_id}</b>\n\n"
        f"{build_item_summary(item)}\n\n"
        f"💰 <b>Starting bid:</b> {format_money(starting_bid)}\n\n"
        f"Bid with <code>/bid {item_id} &lt;amount&gt;</code>",
    )


async def bid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_not_banned(update):
        return
    if not await require_verified(update):
        return

    config = await db.get_config()
    if not config.get("biddings_enabled", True):
        await reply(update, "🚫 Bidding is currently paused by an admin.")
        return

    args = context.args
    item_id = None
    amount = None

    if len(args) >= 2:
        try:
            item_id = int(args[0])
            amount = float(args[1])
        except ValueError:
            await reply(update, "❗ Usage: <code>/bid &lt;item_id&gt; &lt;amount&gt;</code>")
            return
    elif len(args) == 1:
        # Shorthand: /bid <amount> — only works if exactly one auction is
        # active in this chat.
        try:
            amount = float(args[0])
        except ValueError:
            await reply(update, "❗ Usage: <code>/bid &lt;item_id&gt; &lt;amount&gt;</code>")
            return
        active_items = await db.list_active_items_in_chat(update.effective_chat.id)
        if len(active_items) == 1:
            item_id = active_items[0]["_id"]
        elif len(active_items) == 0:
            await reply(update, "ℹ️ There's no active auction in this chat.")
            return
        else:
            await reply(
                update,
                "⚠️ Multiple auctions are active here — specify which one:\n"
                "<code>/bid &lt;item_id&gt; &lt;amount&gt;</code>",
            )
            return
    else:
        await reply(update, "❗ Usage: <code>/bid &lt;item_id&gt; &lt;amount&gt;</code>")
        return

    item = await db.get_item(item_id)
    if item is None or item["status"] != "active":
        await reply(update, f"ℹ️ Item #{item_id} isn't up for auction right now.")
        return
    if update.effective_chat.id != item["chat_id"]:
        await reply(update, "ℹ️ This item's auction isn't running in this chat.")
        return
    if amount <= item["current_bid"]:
        await reply(update, f"❗ Your bid must beat the current bid ({format_money(item['current_bid'])}).")
        return

    user = await ensure_user(update)
    previous_bidder_id = item["current_bidder_id"]
    previous_bidder_name = item["current_bidder_name"]

    await db.update_item(item_id, current_bid=amount, current_bidder_id=user["_id"], current_bidder_name=user["name"])
    await db.push_bid_history(item_id, {
        "user_id": user["_id"], "name": user["name"], "amount": amount, "time": db.now(),
    })

    await reply(
        update,
        f"💸 <b>New highest bid!</b>\n👤 {user['name']} bid <b>{format_money(amount)}</b> "
        f"for <b>{item['name']}</b> (#{item_id})",
    )

    if previous_bidder_id and previous_bidder_id != user["_id"]:
        try:
            await context.bot.send_message(
                chat_id=previous_bidder_id,
                text=(
                    f"⚠️ <b>You've been outbid!</b>\n\nSomeone bid "
                    f"<b>{format_money(amount)}</b> on <b>{item['name']}</b> (#{item_id}).\n"
                    "Head back to the group to bid again if you want it!"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Forbidden:
            await update.message.reply_text(
                f"ℹ️ (Couldn't DM {previous_bidder_name} — they haven't started a "
                f"chat with me. @mention them to let them know they've been outbid!)"
            )
        except TelegramError as exc:
            logger.warning("Failed to send outbid alert: %s", exc)


async def endauction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/endauction &lt;item_id&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ item_id must be a number.")
        return

    item = await db.get_item(item_id)
    if item is None or item["status"] != "active":
        await reply(update, f"ℹ️ Item #{item_id} has no active auction.")
        return

    winner_id = item["current_bidder_id"]
    winner_name = item["current_bidder_name"]
    final_bid = item["current_bid"]

    await db.update_item(item_id, status="sold", ended_at=db.now())

    if winner_id:
        await db.increment_user_fields(winner_id, items_bought=1, total_bought_value=final_bid)
        await db.increment_user_fields(item["submitted_by"], items_sold=1, total_sold_value=final_bid)
        await reply(
            update,
            f"🏆 <b>Auction Ended — Item #{item_id}</b>\n\n"
            f"{build_item_summary(item)}\n\n"
            f"🥇 <b>Winner:</b> {winner_name}\n"
            f"💰 <b>Winning bid:</b> {format_money(final_bid)}\n\n"
            "The seller and winner should now complete the trade.",
        )
    else:
        await reply(update, f"🔚 Auction for item #{item_id} ended with no bids.")

    await db.log_action("auction_ended", update.effective_user.id, update.effective_user.full_name, f"item #{item_id}")


async def delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/delete &lt;item_id&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ item_id must be a number.")
        return
    item = await db.get_item(item_id)
    if item is None:
        await reply(update, f"❗ No item found with ID {item_id}.")
        return
    await db.update_item(item_id, status="cancelled")
    await db.log_action("item_deleted", update.effective_user.id, update.effective_user.full_name, f"item #{item_id}")
    await reply(update, f"🗑️ Item #{item_id} ({item['name']}) has been removed.")


async def reverse_bid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/reverse &lt;item_id&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ item_id must be a number.")
        return
    item = await db.get_item(item_id)
    if item is None or item["status"] != "active":
        await reply(update, f"ℹ️ Item #{item_id} has no active auction.")
        return

    history = item.get("bid_history", [])
    if len(history) < 1:
        await reply(update, "ℹ️ There are no bids to reverse.")
        return

    history = history[:-1]  # drop the most recent bid
    if history:
        last = history[-1]
        await db.update_item(
            item_id, current_bid=last["amount"], current_bidder_id=last["user_id"],
            current_bidder_name=last["name"], bid_history=history,
        )
        await reply(update, f"↩️ Rolled back item #{item_id} to {format_money(last['amount'])} by {last['name']}.")
    else:
        await db.update_item(
            item_id, current_bid=item["starting_bid"], current_bidder_id=None,
            current_bidder_name=None, bid_history=history,
        )
        await reply(update, f"↩️ Rolled back item #{item_id} to the starting bid.")

    await db.log_action("bid_reversed", update.effective_user.id, update.effective_user.full_name, f"item #{item_id}")


async def reset_bid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/reset &lt;item_id&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ item_id must be a number.")
        return
    item = await db.get_item(item_id)
    if item is None or item["status"] != "active":
        await reply(update, f"ℹ️ Item #{item_id} has no active auction.")
        return

    await db.update_item(
        item_id, current_bid=item["starting_bid"], current_bidder_id=None,
        current_bidder_name=None, bid_history=[],
    )
    await db.log_action("bid_reset", update.effective_user.id, update.effective_user.full_name, f"item #{item_id}")
    await reply(update, f"🔄 Item #{item_id} reset to starting bid ({format_money(item['starting_bid'])}).")


async def alist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_filter = context.args[0].lower() if context.args else None
    if status_filter not in (None, "active", "pending", "sold"):
        await reply(update, "❗ Usage: <code>/alist [active|pending|sold]</code>")
        return

    items = await db.list_items(status=status_filter, limit=15)
    if not items:
        await reply(update, "ℹ️ No items found.")
        return

    lines = ["📋 <b>Items</b>\n"]
    for it in items:
        status_emoji = {"pending": "⏳", "active": "🟢", "sold": "✅", "cancelled": "🗑️"}.get(it["status"], "")
        line = f"{status_emoji} #{it['_id']} — {it['name']}"
        if it["status"] == "active":
            line += f" — current bid: {format_money(it['current_bid'])}"
        lines.append(line)
    await reply(update, "\n".join(lines))


async def bidhistory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "❗ Usage: <code>/bidhistory &lt;item_id&gt;</code>")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ item_id must be a number.")
        return
    item = await db.get_item(item_id)
    if item is None:
        await reply(update, f"❗ No item found with ID {item_id}.")
        return

    history = item.get("bid_history", [])
    if not history:
        await reply(update, f"ℹ️ No bids yet on item #{item_id}.")
        return

    lines = [f"📊 <b>Bid history — {item['name']} (#{item_id})</b>\n"]
    for entry in history[-15:]:
        lines.append(f"• {entry['name']}: {format_money(entry['amount'])}")
    await reply(update, "\n".join(lines))


async def mybids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    items = await db.list_user_active_bids(user.id)
    if not items:
        await reply(update, "ℹ️ You have no active bids.")
        return
    lines = ["📌 <b>Your active bids</b>\n"]
    for it in items:
        you_are_winning = it["current_bidder_id"] == user.id
        marker = "🟢 (winning)" if you_are_winning else "🔴 (outbid)"
        lines.append(f"• #{it['_id']} {it['name']} — {format_money(it['current_bid'])} {marker}")
    await reply(update, "\n".join(lines))


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

async def get_verified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if AUCTION_GROUP_ID and update.effective_chat.id != AUCTION_GROUP_ID:
        await reply(update, "❌ This command can only be used in the auction group!")
        return

    user_doc = await ensure_user(update)
    if user_doc.get("is_verified"):
        await reply(update, "✅ You're already verified!")
        return

    await db.set_user_fields(user_doc["_id"], verification_pending=True)
    await reply(update, "📨 Verification requested! An admin will review it shortly.")

    for admin_doc in await db.list_admins():
        try:
            await context.bot.send_message(
                chat_id=admin_doc["_id"],
                text=(
                    f"🔔 {user_display(user_doc)} (ID: {user_doc['_id']}) requested "
                    f"verification.\nRun <code>/verify {user_doc['_id']}</code> to approve."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def verify_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/verify &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return

    target = await db.get_user(target_id)
    if target is None:
        await reply(update, "❗ That user hasn't interacted with the bot yet.")
        return

    await db.set_user_fields(target_id, is_verified=True, verification_pending=False)
    await db.log_action("user_verified", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"✅ {user_display(target)} is now verified.")
    try:
        await context.bot.send_message(chat_id=target_id, text="✅ You've been verified! You can now /add items and /bid.")
    except TelegramError:
        pass


async def remove_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/rverify &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    await db.set_user_fields(target_id, is_verified=False)
    await db.log_action("user_unverified", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"✅ Verification removed for user {target_id}.")


# --------------------------------------------------------------------------
# Admin roles
# --------------------------------------------------------------------------

async def promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/promote &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    target = await db.get_user(target_id)
    if target is None:
        await reply(update, "❗ That user hasn't interacted with the bot yet.")
        return
    await db.set_user_fields(target_id, is_admin=True)
    await db.log_action("user_promoted", update.effective_user.id, update.effective_user.full_name, f"user {target_id} -> admin")
    await reply(update, f"⬆️ {user_display(target)} is now an admin.")


async def demote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/demote &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    await db.set_user_fields(target_id, is_admin=False)
    await db.log_action("user_demoted", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"⬇️ User {target_id} is no longer an admin.")


async def spromote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/spromote &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    target = await db.get_user(target_id)
    if target is None:
        await reply(update, "❗ That user hasn't interacted with the bot yet.")
        return
    await db.set_user_fields(target_id, is_super_admin=True, is_admin=True)
    await db.log_action("user_super_promoted", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"⬆️⬆️ {user_display(target)} is now a super admin.")


async def sdemote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/sdemote &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    await db.set_user_fields(target_id, is_super_admin=False)
    await db.log_action("user_super_demoted", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"⬇️⬇️ User {target_id} is no longer a super admin.")

async def list_admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admins = await db.list_admins()

    lines = ["👮 <b>Bot Admins</b>\n"]

    # Owner
    if OWNER_ID:
        owner = await db.get_user(OWNER_ID)

        if owner:
            owner_name = user_display(owner)
            lines.append(f"👑 Owner: {owner_name}")
        else:
            lines.append(f"👑 Owner: ID {OWNER_ID}")

    # Admins configured through .env
    for username in sorted(SEED_ADMIN_USERNAMES):
        lines.append(f"🛡️ Admin: @{username}")

    # Admins promoted through the database
    configured_usernames = set(SEED_ADMIN_USERNAMES)

    for a in admins:
        username = (a.get("username") or "").lower()

        # Avoid displaying the same person twice
        if username and username in configured_usernames:
            continue

        role = (
            "Super Admin"
            if a.get("is_super_admin")
            else "Admin"
        )

        lines.append(f"• {user_display(a)} — {role}")

    await reply(update, "\n".join(lines))


# --------------------------------------------------------------------------
# Bans
# --------------------------------------------------------------------------

async def hban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/hban &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    await db.set_user_fields(target_id, is_banned=True)
    await db.log_action("user_hbanned", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"🚫 User {target_id} is now banned from using the bot.")


async def hunban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/hunban &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    await db.set_user_fields(target_id, is_banned=False)
    await db.log_action("user_hunbanned", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")
    await reply(update, f"✅ User {target_id} has been unbanned from the bot.")


async def gban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/gban &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return

    await db.set_user_fields(target_id, is_banned=True, is_group_banned=True)
    await db.log_action("user_gbanned", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")

    kicked_from_group = False
    if AUCTION_GROUP_ID:
        try:
            await context.bot.ban_chat_member(chat_id=AUCTION_GROUP_ID, user_id=target_id)
            kicked_from_group = True
        except TelegramError as exc:
            logger.warning("Could not ban %s from group: %s", target_id, exc)

    note = " and removed from the auction group" if kicked_from_group else " (couldn't remove from group — check bot has admin/ban rights there)"
    await reply(update, f"🚫 User {target_id} has been banned from the bot{note}.")


async def g_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/g_unban &lt;user_id&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return

    await db.set_user_fields(target_id, is_banned=False, is_group_banned=False)
    await db.log_action("user_g_unbanned", update.effective_user.id, update.effective_user.full_name, f"user {target_id}")

    unbanned_in_group = False
    if AUCTION_GROUP_ID:
        try:
            await context.bot.unban_chat_member(chat_id=AUCTION_GROUP_ID, user_id=target_id, only_if_banned=True)
            unbanned_in_group = True
        except TelegramError as exc:
            logger.warning("Could not unban %s from group: %s", target_id, exc)

    note = " and can rejoin the auction group" if unbanned_in_group else ""
    await reply(update, f"✅ User {target_id} has been unbanned from the bot{note}.")


# --------------------------------------------------------------------------
# Stats, profile, leaderboards
# --------------------------------------------------------------------------

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc = await ensure_user(update)
    role = "Owner" if is_owner(user_doc["_id"]) else (
        "Super Admin" if user_doc.get("is_super_admin") else (
            "Admin" if user_doc.get("is_admin") else "Member"
        )
    )
    verified = "✅ Verified" if user_doc.get("is_verified") else "❌ Not verified"
    banned = "🚫 Banned" if user_doc.get("is_banned") else "✅ Not banned"

    await reply(
        update,
        f"👤 <b>{user_display(user_doc)}</b>\n\n"
        f"Role: {role}\n"
        f"Status: {verified} | {banned}\n\n"
        f"🛒 Items bought: {user_doc.get('items_bought', 0)}\n"
        f"💰 Total spent: {format_money(user_doc.get('total_bought_value', 0))}\n"
        f"📦 Items sold: {user_doc.get('items_sold', 0)}\n"
        f"💵 Total earned: {format_money(user_doc.get('total_sold_value', 0))}",
    )


async def tseller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sellers = await db.top_sellers()
    if not sellers:
        await reply(update, "ℹ️ No sales recorded yet.")
        return
    lines = ["🏅 <b>Top Sellers</b>\n"]
    for i, s in enumerate(sellers, 1):
        lines.append(f"{i}. {user_display(s)} — {s['items_sold']} sold, {format_money(s['total_sold_value'])} earned")
    await reply(update, "\n".join(lines))


async def tbuyer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buyers = await db.top_buyers()
    if not buyers:
        await reply(update, "ℹ️ No purchases recorded yet.")
        return
    lines = ["🏅 <b>Top Buyers</b>\n"]
    for i, b in enumerate(buyers, 1):
        lines.append(f"{i}. {user_display(b)} — {b['items_bought']} bought, {format_money(b['total_bought_value'])} spent")
    await reply(update, "\n".join(lines))


async def statics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    stats = await db.get_global_stats()
    await reply(
        update,
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Users: {stats['total_users']} (verified: {stats['verified_users']}, banned: {stats['banned_users']})\n"
        f"📦 Items: {stats['total_items']} (pending: {stats['pending_items']}, active: {stats['active_items']}, sold: {stats['sold_items']})\n"
        f"💰 Total sales volume: {format_money(stats['total_volume'])}",
    )


# --------------------------------------------------------------------------
# Toggles
# --------------------------------------------------------------------------

async def toggle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    config = await db.get_config()
    new_value = not config.get("submission_enabled", True)
    await db.set_config_field(submission_enabled=new_value)
    await reply(update, f"📝 Item submissions are now {'✅ ON' if new_value else '🚫 OFF'}.")


async def toggle_biddings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    config = await db.get_config()
    new_value = not config.get("biddings_enabled", True)
    await db.set_config_field(biddings_enabled=new_value)
    await reply(update, f"💸 Bidding is now {'✅ ON' if new_value else '🚫 OFF'}.")


# --------------------------------------------------------------------------
# Owner tools: broadcast, logs, data usage
# --------------------------------------------------------------------------

async def broad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    if not context.args:
        await reply(update, "❗ Usage: <code>/broad &lt;message&gt;</code>")
        return
    text = " ".join(context.args)
    users = await db.list_all_users()
    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["_id"], text=f"📢 {text}")
            sent += 1
        except TelegramError:
            failed += 1
    await db.log_action("broadcast", update.effective_user.id, update.effective_user.full_name, text[:200])
    await reply(update, f"📢 Broadcast sent to {sent} users ({failed} failed/blocked).")


async def fbroad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    if not update.message.reply_to_message:
        await reply(update, "❗ Reply to the message you want to forward with /fbroad.")
        return
    users = await db.list_all_users()
    sent, failed = 0, 0
    for u in users:
        try:
            await update.message.reply_to_message.forward(chat_id=u["_id"])
            sent += 1
        except TelegramError:
            failed += 1
    await db.log_action("forward_broadcast", update.effective_user.id, update.effective_user.full_name, "")
    await reply(update, f"📢 Forwarded to {sent} users ({failed} failed/blocked).")


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    entries = await db.get_recent_logs(20)
    if not entries:
        await reply(update, "ℹ️ No log entries yet.")
        return
    lines = ["🗒️ <b>Recent Logs</b>\n"]
    for e in entries:
        ts = e["timestamp"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {e['action']} by {e['actor_name']}: {e.get('details', '')}")
    await reply(update, "\n".join(lines))


async def hdata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return
    stats = await db.get_global_stats()
    await reply(
        update,
        "💾 <b>Bot Data Usage</b>\n\n"
        f"Users: {stats['total_users']}\n"
        f"Items: {stats['total_items']}\n"
        f"(Full DB size available via your MongoDB provider's dashboard.)",
    )


# --------------------------------------------------------------------------
# Misc: help, brules, report, msg, privacy, cancel
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update)
    await reply(
        update,
        "🎉 <b>Welcome to PokeNova Auction Bot!</b>\n\n"
        "📝 Quick Start:\n"
        "• Get verified with /get_verified\n"
        "• Use /add to submit items\n"
        "• Check /help for more commands",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "📖 <b>Commands</b>\n\n"
        "/add — Add item for auction\n"
        "/bid &lt;item_id&gt; &lt;amount&gt; — Place a bid\n"
        "/alist [active|pending|sold] — View items\n"
        "/bidhistory &lt;item_id&gt; — View bid history for an item\n"
        "/mybids — View your current active bids\n"
        "/profile — View your profile with statistics\n"
        "/brules — Show bidding rules\n"
        "/tseller — Top sellers leaderboard\n"
        "/tbuyer — Top buyers leaderboard\n"
        "/get_verified — Request verification (auction group only)\n"
        "/admins — View list of bot admins\n"
        "/report &lt;message&gt; — Report an issue to admins\n"
        "/privacy — View the privacy policy\n"
        "/cancel — Cancel an in-progress form",
    )


async def brules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, BIDDING_RULES_TEXT)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "❗ Usage: <code>/report &lt;your message&gt;</code>")
        return
    text = " ".join(context.args)
    user = update.effective_user
    admins = await db.list_admins()
    for a in admins:
        try:
            await context.bot.send_message(
                chat_id=a["_id"],
                text=f"🚨 <b>Report from {user.full_name}</b> (ID: {user.id}):\n\n{text}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    await db.log_action("report", user.id, user.full_name, text[:200])
    await reply(update, "✅ Your report has been sent to the admins.")


async def msg_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if len(context.args) < 2:
        await reply(update, "❗ Usage: <code>/msg &lt;user_id&gt; &lt;message&gt;</code>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await reply(update, "❗ user_id must be a number.")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=target_id, text=f"✉️ Message from an admin:\n\n{text}")
        await reply(update, "✅ Message sent.")
    except TelegramError:
        await reply(update, "❗ Couldn't deliver the message (user may have blocked the bot).")


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "🔒 <b>Privacy Policy — PokeNova Auction Bot</b>\n\n"
        "We store your Telegram user ID, name, and username, plus your "
        "auction activity (items listed, bids placed, verification status), "
        "to operate the auction system.\n\n"
        "Your data is used only for bot functionality and is never sold or "
        "shared with third parties. Other users may see information you "
        "make public through normal bot use (e.g. your name next to a bid).\n\n"
        "To request a copy of your data, a correction, or deletion, contact "
        "an admin via /report.\n\n"
        "Full policy: see branding/PRIVACY_POLICY.md, or the pinned message "
        "in the auction group.",
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Create a .env file (see .env.example).")
    if not OWNER_ID:
        logger.warning("OWNER_ID is not set — owner-only commands will be unusable.")

    application = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_entry)],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(choose_type, pattern=r"^type:")],
            POKEMON_METHOD: [CallbackQueryHandler(choose_pokemon_method, pattern=r"^method:")],
            IMPORT_STATS_MSG: [MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, import_stats_msg)],
            IMPORT_IVEV_MSG: [MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, import_ivev_msg)],
            POKEMON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pokemon_name)],
            POKEMON_NATURE: [CallbackQueryHandler(pokemon_nature, pattern=r"^nature:")],
            POKEMON_IVS: [MessageHandler(filters.TEXT & ~filters.COMMAND, pokemon_ivs)],
            POKEMON_EXTRA: [MessageHandler(filters.TEXT & ~filters.COMMAND, pokemon_extra)],
            TM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tm_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(add_conv)
    application.add_handler(CommandHandler("auction", auction_start))
    application.add_handler(CommandHandler("bid", bid))
    application.add_handler(CommandHandler("alist", alist))
    application.add_handler(CommandHandler("bidhistory", bidhistory))
    application.add_handler(CommandHandler("mybids", mybids))
    application.add_handler(CommandHandler("endauction", endauction))
    application.add_handler(CommandHandler("delete", delete_item))
    application.add_handler(CommandHandler("reverse", reverse_bid))
    application.add_handler(CommandHandler("reset", reset_bid))

    application.add_handler(CommandHandler("get_verified", get_verified))
    application.add_handler(CommandHandler("verify", verify_user))
    application.add_handler(CommandHandler("rverify", remove_verification))

    application.add_handler(CommandHandler("promote", promote))
    application.add_handler(CommandHandler("demote", demote))
    application.add_handler(CommandHandler("spromote", spromote))
    application.add_handler(CommandHandler("sdemote", sdemote))
    application.add_handler(CommandHandler("admins", list_admins_cmd))

    application.add_handler(CommandHandler("hban", hban))
    application.add_handler(CommandHandler("hunban", hunban))
    application.add_handler(CommandHandler("gban", gban))
    application.add_handler(CommandHandler("g_unban", g_unban))

    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("tseller", tseller))
    application.add_handler(CommandHandler("tbuyer", tbuyer))
    application.add_handler(CommandHandler("statics", statics))

    application.add_handler(CommandHandler("submission", toggle_submission))
    application.add_handler(CommandHandler("biddings", toggle_biddings))

    application.add_handler(CommandHandler("broad", broad))
    application.add_handler(CommandHandler("fbroad", fbroad))
    application.add_handler(CommandHandler("logs", logs_cmd))
    application.add_handler(CommandHandler("hdata", hdata))

    application.add_handler(CommandHandler("brules", brules))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("msg", msg_user))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(CommandHandler("cancel", cancel_setup))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()