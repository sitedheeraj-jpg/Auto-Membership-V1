from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient

from config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex[:16]


class MongoDatabase:
    """Small async repository layer used by the bot.

    Keeping Mongo calls here makes the handlers easy to read and avoids the
    broken mixture of SQLAlchemy/Tortoise repositories present in the source.
    """

    def __init__(self) -> None:
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(
            settings.database_url,
            serverSelectionTimeoutMS=10_000,
            tz_aware=True,
        )
        await self.client.admin.command("ping")
        self.db = self.client[settings.database_name]
        await self.db.users.create_index("telegram_id", unique=True)
        await self.db.channels.create_index([("active", 1), ("title", 1)])
        await self.db.plans.create_index([("channel_id", 1), ("active", 1)])
        await self.db.orders.create_index("order_id", unique=True)
        await self.db.orders.create_index([("user_id", 1), ("status", 1)])
        await self.db.subscriptions.create_index(
            [("user_id", 1), ("channel_id", 1), ("status", 1)]
        )
        await self.db.subscriptions.create_index(
            [("invite_link", 1)], sparse=True
        )

    async def close(self) -> None:
        if self.client:
            self.client.close()

    async def upsert_user(self, user: Any) -> None:
        await self.db.users.update_one(
            {"telegram_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name or "",
                    "last_name": user.last_name,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )

    async def get_user(self, telegram_id: int) -> Optional[dict]:
        return await self.db.users.find_one({"telegram_id": telegram_id})

    async def list_premium_users(self) -> list[dict]:
        user_ids = await self.db.subscriptions.distinct(
            "user_id",
            {
                "status": {
                    "$in": ["active", "pending_join", "expired", "terminated"]
                }
            },
        )
        if not user_ids:
            return []
        return await self.db.users.find(
            {"telegram_id": {"$in": user_ids}}
        ).sort([("first_name", 1), ("telegram_id", 1)]).to_list(500)

    async def list_user_subscriptions(self, telegram_id: int) -> list[dict]:
        return await self.db.subscriptions.find(
            {"user_id": telegram_id}
        ).sort("created_at", -1).to_list(200)

    async def get_subscription(self, sub_id: str) -> Optional[dict]:
        return await self.db.subscriptions.find_one({"_id": sub_id})

    async def get_setting(self, key: str, default: Any = None) -> Any:
        document = await self.db.settings.find_one({"_id": "global"})
        return document.get(key, default) if document else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.settings.update_one(
            {"_id": "global"},
            {"$set": {key: value, "updated_at": utcnow()}},
            upsert=True,
        )

    async def list_channels(self, active_only: bool = True) -> list[dict]:
        query = {"active": True} if active_only else {}
        return await self.db.channels.find(query).sort("title", 1).to_list(200)

    async def get_channel(self, channel_id: str) -> Optional[dict]:
        return await self.db.channels.find_one({"_id": channel_id})

    async def get_channel_by_telegram_id(self, channel_id: int) -> Optional[dict]:
        return await self.db.channels.find_one({"channel_id": channel_id})

    async def save_channel(
        self, channel_id: int, title: str, username: str, description: str
    ) -> str:
        existing = await self.db.channels.find_one({"channel_id": channel_id})
        doc_id = existing["_id"] if existing else new_id()
        await self.db.channels.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "title": title,
                    "username": username or "",
                    "description": description,
                    "active": True,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"_id": doc_id, "created_at": utcnow()},
            },
            upsert=True,
        )
        return doc_id

    async def update_channel(self, doc_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        await self.db.channels.update_one({"_id": doc_id}, {"$set": values})

    async def list_plans(self, channel_id: str, active_only: bool = True) -> list[dict]:
        query: dict[str, Any] = {"channel_id": channel_id}
        if active_only:
            query["active"] = True
        return await self.db.plans.find(query).sort("duration_days", 1).to_list(100)

    async def get_plan(self, plan_id: str) -> Optional[dict]:
        return await self.db.plans.find_one({"_id": plan_id})

    async def save_plan(
        self,
        channel_id: str,
        name: str,
        duration_days: int,
        price: float,
        description: str,
        plan_id: Optional[str] = None,
    ) -> str:
        plan_id = plan_id or new_id()
        await self.db.plans.update_one(
            {"_id": plan_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "name": name,
                    "duration_days": duration_days,
                    "price": round(price, 2),
                    "description": description,
                    "active": True,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"_id": plan_id, "created_at": utcnow()},
            },
            upsert=True,
        )
        return plan_id

    async def deactivate_plan(self, plan_id: str) -> None:
        await self.db.plans.update_one(
            {"_id": plan_id}, {"$set": {"active": False, "updated_at": utcnow()}}
        )

    async def create_order(self, values: dict[str, Any]) -> None:
        await self.db.orders.insert_one(values)

    async def get_order(self, order_id: str) -> Optional[dict]:
        return await self.db.orders.find_one({"order_id": order_id})

    async def set_order_message(self, order_id: str, message_id: int) -> None:
        await self.db.orders.update_one(
            {"order_id": order_id},
            {"$set": {"payment_message_id": message_id}},
        )

    async def update_order(self, order_id: str, values: dict[str, Any]) -> None:
        values["updated_at"] = utcnow()
        await self.db.orders.update_one({"order_id": order_id}, {"$set": values})

    async def claim_success(self, order_id: str, txn: dict) -> bool:
        result = await self.db.orders.update_one(
            {"order_id": order_id, "status": "pending"},
            {
                "$set": {
                    "status": "paid",
                    "transaction": txn,
                    "verified_at": utcnow(),
                    "updated_at": utcnow(),
                }
            },
        )
        return result.modified_count == 1

    async def is_txn_used(self, txn_id: str) -> bool:
        if not txn_id:
            return False
        return await self.db.orders.find_one(
            {"transaction.txn_id": txn_id, "status": "paid"},
            {"_id": 1},
        ) is not None

    async def find_pending_subscription(
        self, user_id: int, channel_id: int
    ) -> Optional[dict]:
        return await self.db.subscriptions.find_one(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "status": "pending_join",
            },
            sort=[("created_at", -1)],
        )

    async def find_subscription(
        self, user_id: int, channel_id: int, active_only: bool = False
    ) -> Optional[dict]:
        query: dict[str, Any] = {"user_id": user_id, "channel_id": channel_id}
        if active_only:
            query["status"] = "active"
        return await self.db.subscriptions.find_one(query, sort=[("created_at", -1)])

    async def create_subscription(self, values: dict[str, Any]) -> str:
        values.setdefault("_id", new_id())
        values.setdefault("created_at", utcnow())
        await self.db.subscriptions.insert_one(values)
        return values["_id"]

    async def update_subscription(self, sub_id: str, values: dict[str, Any]) -> None:
        values["updated_at"] = utcnow()
        await self.db.subscriptions.update_one({"_id": sub_id}, {"$set": values})

    async def terminate_user_subscriptions(
        self, user_id: int, channel_id: Optional[int] = None
    ) -> list[dict]:
        query: dict[str, Any] = {
            "user_id": user_id,
            "status": {"$in": ["active", "pending_join"]},
        }
        if channel_id is not None:
            query["channel_id"] = channel_id
        records = await self.db.subscriptions.find(query).to_list(200)
        if records:
            await self.db.subscriptions.update_many(
                {"_id": {"$in": [record["_id"] for record in records]}},
                {
                    "$set": {
                        "status": "terminated",
                        "terminated_at": utcnow(),
                        "updated_at": utcnow(),
                    }
                },
            )
        return records

    async def active_expiring(self, before: datetime) -> list[dict]:
        return await self.db.subscriptions.find(
            {
                "status": "active",
                "ends_at": {"$ne": None, "$lte": before},
            }
        ).to_list(500)

    async def due_reminders(self, before: datetime) -> list[dict]:
        return await self.db.subscriptions.find(
            {
                "status": "active",
                "ends_at": {"$ne": None, "$lte": before},
                "reminder_sent": {"$ne": True},
            }
        ).to_list(500)

    async def count(self, collection: str, query: dict | None = None) -> int:
        return await self.db[collection].count_documents(query or {})
