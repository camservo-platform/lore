"""The append-only event log (Postgres) and its live fan-out (Redis streams).

Write an event with `record` inside the same transaction as the state change it
describes, then `EventBus.publish` it after commit so other players' clients see it.
"""

import json
import logging
from typing import Any

import asyncpg
from redis.asyncio import Redis

log = logging.getLogger(__name__)

EVENT_COLUMNS = "id, campaign_id, session_id, character_id, occurred_at, actor, type, summary, data"


def event_dict(row: asyncpg.Record) -> dict[str, Any]:
    event = dict(row)
    event["occurred_at"] = event["occurred_at"].isoformat()
    return event


async def record(
    conn: asyncpg.Connection,
    *,
    campaign_id: int,
    actor: str,
    type: str,
    summary: str,
    character_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Appends an event, attached to the campaign's open session if there is one."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO events (campaign_id, session_id, character_id, actor, type, summary, data)
        VALUES ($1, (SELECT id FROM game_sessions WHERE campaign_id = $1 AND ended_at IS NULL), $2, $3, $4, $5, $6)
        RETURNING {EVENT_COLUMNS}
        """,
        campaign_id,
        character_id,
        actor,
        type,
        summary,
        data or {},
    )
    return event_dict(row)


def stream_key(campaign_id: int) -> str:
    return f"lore:campaign:{campaign_id}:events"


class EventBus:
    """Publishes committed events to a per-campaign Redis stream.

    Consumers (the voice app, one connection per player) read with XREAD from the
    last id they saw. The stream is a capped live feed; Postgres is the record.
    """

    def __init__(self, redis: Redis, maxlen: int = 1000):
        self._redis = redis
        self._maxlen = maxlen

    async def publish(self, *events: dict[str, Any]) -> None:
        for event in events:
            try:
                await self._redis.xadd(
                    stream_key(event["campaign_id"]),
                    {"event": json.dumps(event)},
                    maxlen=self._maxlen,
                    approximate=True,
                )
            except Exception:
                # The event is already committed; a missed live update must not fail the tool call.
                log.exception("failed to publish event %s", event["id"])
