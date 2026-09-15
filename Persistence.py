"""
persistence.py — MongoDB-backed persistence for python-telegram-bot.

Why this exists: on Vercel, every incoming update runs in a fresh,
stateless serverless function. python-telegram-bot's default behavior
keeps conversation state (like the multi-step /add flow) in plain Python
memory, which would be wiped out between invocations. This class tells
python-telegram-bot to read/write that state to MongoDB instead, so a
conversation started in one function invocation can be continued in the
next one.

Only chat_data, user_data, and conversation states are persisted here —
bot_data and callback_data aren't used by this bot, so they're no-ops.
"""

import json
from typing import Any, DefaultDict, Dict, Optional, Tuple

from telegram.ext import BasePersistence, PersistenceInput

import db


def _key_to_str(key: Tuple) -> str:
    return json.dumps(list(key))


def _str_to_key(key_str: str) -> Tuple:
    return tuple(json.loads(key_str))


class MongoPersistence(BasePersistence):
    def __init__(self):
        super().__init__(
            store_data=PersistenceInput(
                bot_data=False, chat_data=True, user_data=True, callback_data=False
            )
        )

    # ---- bot_data: unused ----
    async def get_bot_data(self) -> Dict[Any, Any]:
        return {}

    async def update_bot_data(self, data: Dict[Any, Any]) -> None:
        pass

    async def refresh_bot_data(self, bot_data: Dict[Any, Any]) -> None:
        pass

    # ---- user_data ----
    async def get_user_data(self) -> DefaultDict[int, Dict[Any, Any]]:
        from collections import defaultdict
        result: DefaultDict[int, Dict[Any, Any]] = defaultdict(dict)
        cursor = db.get_db().persistence_user_data.find({})
        async for doc in cursor:
            result[doc["_id"]] = doc.get("data", {})
        return result

    async def update_user_data(self, user_id: int, data: Dict[Any, Any]) -> None:
        await db.get_db().persistence_user_data.update_one(
            {"_id": user_id}, {"$set": {"data": data}}, upsert=True
        )

    async def refresh_user_data(self, user_id: int, user_data: Dict[Any, Any]) -> None:
        pass

    async def drop_user_data(self, user_id: int) -> None:
        await db.get_db().persistence_user_data.delete_one({"_id": user_id})

    # ---- chat_data ----
    async def get_chat_data(self) -> DefaultDict[int, Dict[Any, Any]]:
        from collections import defaultdict
        result: DefaultDict[int, Dict[Any, Any]] = defaultdict(dict)
        cursor = db.get_db().persistence_chat_data.find({})
        async for doc in cursor:
            result[doc["_id"]] = doc.get("data", {})
        return result

    async def update_chat_data(self, chat_id: int, data: Dict[Any, Any]) -> None:
        await db.get_db().persistence_chat_data.update_one(
            {"_id": chat_id}, {"$set": {"data": data}}, upsert=True
        )

    async def refresh_chat_data(self, chat_id: int, chat_data: Dict[Any, Any]) -> None:
        pass

    async def drop_chat_data(self, chat_id: int) -> None:
        await db.get_db().persistence_chat_data.delete_one({"_id": chat_id})

    # ---- conversations (this is what makes /add's multi-step flow work) ----
    async def get_conversations(self, name: str) -> Dict[Tuple, object]:
        doc = await db.get_db().persistence_conversations.find_one({"_id": name})
        if not doc:
            return {}
        return {_str_to_key(k): v for k, v in doc.get("data", {}).items()}

    async def update_conversation(self, name: str, key: Tuple, new_state: Optional[object]) -> None:
        key_str = _key_to_str(key)
        if new_state is None:
            await db.get_db().persistence_conversations.update_one(
                {"_id": name}, {"$unset": {f"data.{key_str}": ""}}, upsert=True
            )
        else:
            await db.get_db().persistence_conversations.update_one(
                {"_id": name}, {"$set": {f"data.{key_str}": new_state}}, upsert=True
            )

    # ---- callback_data: unused ----
    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data) -> None:
        pass

    async def flush(self) -> None:
        pass