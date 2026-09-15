"""
db.py — MongoDB data layer for PokeNova Auction Bot.

Collections:
  users     — one doc per Telegram user (roles, verification, stats, bans)
  items     — one doc per auctionable item (Pokémon or TM)
  counters  — auto-increment counters (used for human-friendly item IDs)
  config    — a single global settings doc (_id="global")
  logs      — an append-only action log for /logs and moderation history
"""

import os
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGO_DB_NAME", "pokenova_auction_bot")

_client: Optional[AsyncIOMotorClient] = None


def get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)
    return _client[DB_NAME]


def now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

DEFAULT_USER_FIELDS = {
    "is_admin": False,
    "is_super_admin": False,
    "is_banned": False,          # /hban — banned from using the bot
    "is_group_banned": False,    # /gban — banned from bot + group
    "is_verified": False,
    "verification_pending": False,
    "items_sold": 0,
    "items_bought": 0,
    "total_sold_value": 0.0,
    "total_bought_value": 0.0,
}


async def get_or_create_user(user_id: int, name: str, username: Optional[str]) -> dict:
    db = get_db()
    doc = await db.users.find_one({"_id": user_id})
    if doc is None:
        doc = {"_id": user_id, "name": name, "username": username, "created_at": now()}
        doc.update(DEFAULT_USER_FIELDS)
        await db.users.insert_one(doc)
    else:
        # Keep name/username fresh in case they changed.
        await db.users.update_one(
            {"_id": user_id}, {"$set": {"name": name, "username": username}}
        )
        doc["name"] = name
        doc["username"] = username
    return doc


async def get_user(user_id: int) -> Optional[dict]:
    return await get_db().users.find_one({"_id": user_id})


async def set_user_fields(user_id: int, **fields) -> None:
    await get_db().users.update_one({"_id": user_id}, {"$set": fields})


async def increment_user_fields(user_id: int, **fields) -> None:
    await get_db().users.update_one({"_id": user_id}, {"$inc": fields})


async def list_admins() -> list:
    cursor = get_db().users.find({"$or": [{"is_admin": True}, {"is_super_admin": True}]})
    return [doc async for doc in cursor]


async def list_all_users() -> list:
    cursor = get_db().users.find({})
    return [doc async for doc in cursor]


async def top_sellers(limit: int = 10) -> list:
    cursor = get_db().users.find({"items_sold": {"$gt": 0}}).sort("items_sold", -1).limit(limit)
    return [doc async for doc in cursor]


async def top_buyers(limit: int = 10) -> list:
    cursor = get_db().users.find({"items_bought": {"$gt": 0}}).sort("items_bought", -1).limit(limit)
    return [doc async for doc in cursor]


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

async def _next_item_id() -> int:
    db = get_db()
    result = await db.counters.find_one_and_update(
        {"_id": "item_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return result["seq"]


async def create_item(data: dict) -> int:
    item_id = await _next_item_id()
    doc = {
        "_id": item_id,
        "status": "pending",  # pending -> active -> sold / cancelled
        "starting_bid": None,
        "current_bid": None,
        "current_bidder_id": None,
        "current_bidder_name": None,
        "bid_history": [],
        "chat_id": None,
        "created_at": now(),
        "started_at": None,
        "ended_at": None,
    }
    doc.update(data)
    doc["_id"] = item_id
    await get_db().items.insert_one(doc)
    return item_id


async def get_item(item_id: int) -> Optional[dict]:
    return await get_db().items.find_one({"_id": item_id})


async def update_item(item_id: int, **fields) -> None:
    await get_db().items.update_one({"_id": item_id}, {"$set": fields})


async def push_bid_history(item_id: int, entry: dict) -> None:
    await get_db().items.update_one(
        {"_id": item_id}, {"$push": {"bid_history": entry}}
    )


async def list_items(status: Optional[str] = None, limit: int = 20) -> list:
    query = {"status": status} if status else {"status": {"$ne": "cancelled"}}
    cursor = get_db().items.find(query).sort("_id", -1).limit(limit)
    return [doc async for doc in cursor]


async def list_active_items_in_chat(chat_id: int) -> list:
    cursor = get_db().items.find({"status": "active", "chat_id": chat_id})
    return [doc async for doc in cursor]


async def list_user_active_bids(user_id: int) -> list:
    cursor = get_db().items.find(
        {"status": "active", "bid_history.user_id": user_id}
    )
    return [doc async for doc in cursor]


# --------------------------------------------------------------------------
# Config (global toggles)
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "_id": "global",
    "submission_enabled": True,
    "biddings_enabled": True,
    "auction_group_id": None,
}


async def get_config() -> dict:
    db = get_db()
    doc = await db.config.find_one({"_id": "global"})
    if doc is None:
        doc = dict(DEFAULT_CONFIG)
        await db.config.insert_one(doc)
    return doc


async def set_config_field(**fields) -> None:
    await get_db().config.update_one(
        {"_id": "global"}, {"$set": fields}, upsert=True
    )


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

async def log_action(action: str, actor_id: int, actor_name: str, details: str = "") -> None:
    await get_db().logs.insert_one(
        {
            "action": action,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "details": details,
            "timestamp": now(),
        }
    )


async def get_recent_logs(limit: int = 20) -> list:
    cursor = get_db().logs.find({}).sort("timestamp", -1).limit(limit)
    return [doc async for doc in cursor]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

async def get_global_stats() -> dict:
    db = get_db()
    total_users = await db.users.count_documents({})
    verified_users = await db.users.count_documents({"is_verified": True})
    banned_users = await db.users.count_documents({"is_banned": True})
    total_items = await db.items.count_documents({})
    active_items = await db.items.count_documents({"status": "active"})
    sold_items = await db.items.count_documents({"status": "sold"})
    pending_items = await db.items.count_documents({"status": "pending"})

    volume_cursor = db.items.aggregate(
        [
            {"$match": {"status": "sold"}},
            {"$group": {"_id": None, "total": {"$sum": "$current_bid"}}},
        ]
    )
    volume_doc = await volume_cursor.to_list(length=1)
    total_volume = volume_doc[0]["total"] if volume_doc else 0

    return {
        "total_users": total_users,
        "verified_users": verified_users,
        "banned_users": banned_users,
        "total_items": total_items,
        "active_items": active_items,
        "sold_items": sold_items,
        "pending_items": pending_items,
        "total_volume": total_volume,
    }