"""Usage and presence, kept in Redis for the admin view.

Usage is counted per day: LLM tokens per model, GM turns per player, text-to-speech
characters and voice-conversation seconds. Cost is estimated from list prices; it's a
guide, not a bill (check the provider consoles for that).

Presence records when each player was last seen and where (lobby, admin, or a world),
and whether they have a voice conversation open.
"""

import datetime
import time
from typing import Any

from redis.asyncio import Redis

USAGE_TTL = 120 * 24 * 3600
ONLINE_SECONDS = 90  # the live feed checks in every 15 s, so this means "has the page open"

# USD per million tokens: input, output, cache read, cache write (5-minute). List prices,
# October 2026; models not listed are counted but not priced.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")


def _day(ts: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(ts or time.time(), datetime.UTC).date().isoformat()


def estimate_cost(model: str, tokens: dict[str, int]) -> float | None:
    prices = PRICES.get(model)
    if prices is None:
        return None
    return sum(tokens.get(kind, 0) * price for kind, price in zip(TOKEN_KINDS, prices)) / 1_000_000


class Usage:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def _add(self, **fields: int) -> None:
        key = f"lore:usage:{_day()}"
        async with self._redis.pipeline() as pipe:
            for field, amount in fields.items():
                if amount:
                    pipe.hincrby(key, field, amount)
            pipe.expire(key, USAGE_TTL)
            await pipe.execute()

    async def llm(self, model: str, usage: Any) -> None:
        """Counts one model response's tokens (an Anthropic `usage` object)."""
        await self._add(**{
            f"llm|{model}|input": usage.input_tokens or 0,
            f"llm|{model}|output": usage.output_tokens or 0,
            f"llm|{model}|cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            f"llm|{model}|cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        })

    async def turn(self, user: str) -> None:
        await self._add(**{f"turns|{user}": 1})

    async def tts(self, characters: int) -> None:
        await self._add(**{"tts|characters": characters})

    async def voice(self, seconds: float) -> None:
        await self._add(**{"voice|seconds": round(seconds)})

    async def summary(self, days: int) -> dict[str, Any]:
        today = datetime.datetime.now(datetime.UTC).date()
        dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days)]
        async with self._redis.pipeline() as pipe:
            for date in dates:
                pipe.hgetall(f"lore:usage:{date}")
            raw = await pipe.execute()
        out, totals = [], {"cost": 0.0, "turns": 0, "tts_characters": 0, "voice_seconds": 0, "models": {}}
        for date, fields in zip(dates, raw):
            day = {"date": date, "models": {}, "turns": {}, "tts_characters": 0, "voice_seconds": 0, "cost": 0.0}
            for field, value in fields.items():
                kind, *rest = field.split("|")
                value = int(value)
                if kind == "llm":
                    model, token_kind = rest
                    day["models"].setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))[token_kind] += value
                elif kind == "turns":
                    day["turns"][rest[0]] = value
                elif field == "tts|characters":
                    day["tts_characters"] = value
                elif field == "voice|seconds":
                    day["voice_seconds"] = value
            for model, tokens in day["models"].items():
                day["cost"] += estimate_cost(model, tokens) or 0
                total = totals["models"].setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
                for kind in TOKEN_KINDS:
                    total[kind] += tokens[kind]
            totals["cost"] += day["cost"]
            totals["turns"] += sum(day["turns"].values())
            totals["tts_characters"] += day["tts_characters"]
            totals["voice_seconds"] += day["voice_seconds"]
            out.append(day)
        unpriced = sorted(m for m in totals["models"] if m not in PRICES)
        return {"days": out, "totals": totals, "unpriced_models": unpriced}


class Presence:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def seen(self, user: str, where: str, campaign_id: int | None = None) -> None:
        now = time.time()
        key = f"lore:presence:{user}"
        async with self._redis.pipeline() as pipe:
            pipe.zadd("lore:presence", {user: now})
            pipe.hset(key, mapping={"where": where, "campaign_id": campaign_id or "", "last_seen": now})
            pipe.expire(key, USAGE_TTL)
            await pipe.execute()

    async def voice(self, user: str, active: bool) -> None:
        await self._redis.hset(f"lore:presence:{user}", "voice", "1" if active else "")

    async def everyone(self) -> list[dict[str, Any]]:
        now = time.time()
        users = await self._redis.zrevrange("lore:presence", 0, -1, withscores=True)
        async with self._redis.pipeline() as pipe:
            for user, _ in users:
                pipe.hgetall(f"lore:presence:{user}")
            details = await pipe.execute()
        return [
            {
                "user": user,
                "last_seen": seen,
                "online": now - seen < ONLINE_SECONDS,
                "where": info.get("where", ""),
                "campaign_id": int(info["campaign_id"]) if info.get("campaign_id") else None,
                "voice": bool(info.get("voice")) and now - seen < ONLINE_SECONDS,
            }
            for (user, seen), info in zip(users, details)
        ]
