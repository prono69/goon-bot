import os
import re
from datetime import UTC, datetime
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from bot.config import MONGO_URI, DB_NAME


class AsyncMongoManager:
    """Asynchronous MongoDB manager for generic plugin data storage."""

    def __init__(self) -> None:
        self._client: AsyncMongoClient[Any] | None = None
        self._db: Any = None

    async def connect(self) -> None:
        """Initialize MongoDB client."""
        try:
            self._client = AsyncMongoClient(MONGO_URI)
            self._db = self._client[DB_NAME]

            # Add indexes here as each plugin's query pattern stabilizes
            await self._db.users.create_index("user_id", unique=True, background=True)

            print(f"[MongoDB] Connected successfully to '{DB_NAME}'")
        except Exception as e:
            print(f"[MongoDB] Failed to connect: {e}")
            raise

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
            print("[MongoDB] Client disconnected")

    async def add_item(
        self,
        collection_name: str,
        data: dict[str, Any],
        unique_query: dict[str, Any] | None = None,
    ) -> bool:
        """
        Universal upsert for any plugin's data.

        collection_name: e.g. "lastfm_tracks", "rule34_posts"
        data: full document to store
        unique_query: fields that identify uniqueness (e.g. {"track_id": "..."}).
                      If omitted, plain insert (no dedup).
        """
        if self._db is None:
            return False
        try:
            now = datetime.now(UTC)
            col = self._db[collection_name]

            if unique_query:
                await col.update_one(
                    unique_query,
                    {
                        "$set": {**data, "updated_at": now},
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            else:
                data.setdefault("created_at", now)
                await col.insert_one(data)

            return True
        except PyMongoError as e:
            print(f"[MongoDB] Failed to add item in '{collection_name}': {e}")
            return False

    async def item_exists(self, collection_name: str, query: dict[str, Any]) -> bool:
        """Check if any document matches the query in the given collection."""
        if self._db is None:
            return False
        try:
            col = self._db[collection_name]
            count = await col.count_documents(query, limit=1)
            return count > 0
        except PyMongoError as e:
            print(f"[MongoDB] Failed to check existence in '{collection_name}': {e}")
            return False

    async def get_item(
        self, collection_name: str, query: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fetch a single document from any collection."""
        if self._db is None:
            return None
        try:
            col = self._db[collection_name]
            return await col.find_one(query, {"_id": 0})
        except PyMongoError as e:
            print(f"[MongoDB] Failed to get item from '{collection_name}': {e}")
            return None

    async def get_items(
        self,
        collection_name: str,
        query: dict[str, Any] | None = None,
        limit: int = 0,
        sort_field: str | None = None,
        sort_desc: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents from any collection, optionally sorted."""
        if self._db is None:
            return []
        try:
            col = self._db[collection_name]
            cursor = col.find(query or {}, {"_id": 0})
            if sort_field:
                cursor = cursor.sort(sort_field, -1 if sort_desc else 1)
            if limit:
                cursor = cursor.limit(limit)

            results: list[dict[str, Any]] = []
            async for doc in cursor:
                results.append(doc)
            return results
        except PyMongoError as e:
            print(f"[MongoDB] Failed to get items from '{collection_name}': {e}")
            return []

    async def delete_item(self, collection_name: str, query: dict[str, Any]) -> int:
        """Delete document(s) matching query. Returns count deleted."""
        if self._db is None:
            return 0
        try:
            col = self._db[collection_name]
            result = await col.delete_many(query)
            return result.deleted_count
        except PyMongoError as e:
            print(f"[MongoDB] Failed to delete item in '{collection_name}': {e}")
            return 0


# Single shared instance — import this wherever you need DB access
mongo = AsyncMongoManager()