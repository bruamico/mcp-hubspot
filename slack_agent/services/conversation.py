"""
Conversational memory using Redis.
Each Slack thread gets its own message history with a 24h TTL.
Key format: conv:{channel}:{thread_ts}
"""
import json
import logging
import os
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

HISTORY_TTL = int(os.getenv("CONVERSATION_TTL_SECONDS", "86400"))  # 24 hours
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "40"))


class ConversationMemory:
    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._client: Optional[aioredis.Redis] = None

    async def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._client

    def _key(self, channel: str, thread_ts: str) -> str:
        return f"conv:{channel}:{thread_ts}"

    async def get_history(self, channel: str, thread_ts: str) -> list[dict]:
        """Return stored messages for this thread (oldest first)."""
        client = await self._get_client()
        raw = await client.get(self._key(channel, thread_ts))
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt history for %s/%s — resetting", channel, thread_ts)
            return []

    async def add_message(
        self, channel: str, thread_ts: str, role: str, content: str
    ) -> None:
        """Append a message and refresh TTL. Trims to MAX_HISTORY_MESSAGES."""
        client = await self._get_client()
        key = self._key(channel, thread_ts)
        history = await self.get_history(channel, thread_ts)
        history.append({"role": role, "content": content})

        # Keep only recent messages to avoid huge context windows
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        await client.setex(key, HISTORY_TTL, json.dumps(history))

    async def clear_history(self, channel: str, thread_ts: str) -> None:
        client = await self._get_client()
        await client.delete(self._key(channel, thread_ts))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
