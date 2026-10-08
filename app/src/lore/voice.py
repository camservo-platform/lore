"""Deepgram text-to-speech, proxied so the API key stays server-side. Speech-to-text is
live, over Flux: see lore.web.conversation."""

import hashlib
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

API = "https://api.deepgram.com/v1"
# Deepgram's per-request text limit for /speak.
MAX_TTS_CHARS = 2000
VOICES_TTL = 3600


def english_voices(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """The English Aura-2 voices from Deepgram's /v1/models catalogue, for a voice picker."""
    voices = []
    for model in catalogue.get("tts", []):
        if model.get("architecture") != "aura-2" or "en" not in model.get("languages", []):
            continue
        meta = model.get("metadata") or {}
        tags = meta.get("tags") or []
        voices.append({
            "id": model["canonical_name"],
            "name": meta.get("display_name") or model.get("name", model["canonical_name"]).title(),
            "accent": meta.get("accent", ""),
            "gender": next((t for t in tags if t in ("feminine", "masculine")), ""),
            "traits": [t for t in tags if t not in ("feminine", "masculine")][:3],
        })
    return sorted(voices, key=lambda v: v["name"])


class Voice:
    def __init__(self, api_key: str, tts_model: str):
        self._client = httpx.AsyncClient(
            base_url=API, headers={"Authorization": f"Token {api_key}"}, timeout=httpx.Timeout(60, connect=10)
        )
        self._tts_model = tts_model
        self._voices: list[dict[str, Any]] = []
        self._voices_at = 0.0

    @property
    def default_voice(self) -> str:
        return self._tts_model

    async def voices(self) -> list[dict[str, Any]]:
        if not self._voices or time.monotonic() - self._voices_at > VOICES_TTL:
            resp = await self._client.get("/models")
            resp.raise_for_status()
            self._voices, self._voices_at = english_voices(resp.json()), time.monotonic()
        return self._voices

    async def resolve(self, voice: str | None) -> str:
        """`voice` if it's a known English voice, else the configured default."""
        if voice and voice != self._tts_model:
            try:
                if any(v["id"] == voice for v in await self.voices()):
                    return voice
            except httpx.HTTPError:
                pass
        return self._tts_model

    def pick_npc_voice(self, voices: list[dict[str, Any]], name: str, gender: str, taken: set[str],
                       avoid: set[str]) -> str:
        """A voice for a new NPC: matching `gender` when given, not one in `avoid` (the narrator's),
        preferring one no other NPC in the world has; stable for a given name."""
        pool = [v for v in voices if v["id"] not in avoid]
        if gender in ("feminine", "masculine"):
            pool = [v for v in pool if v["gender"] == gender] or pool
        fresh = [v for v in pool if v["id"] not in taken] or pool
        if not fresh:
            return self._tts_model
        digest = int(hashlib.sha256(name.lower().encode()).hexdigest(), 16)
        return fresh[digest % len(fresh)]["id"]

    async def speak(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """MP3 audio for `text`, streamed as Deepgram produces it."""
        model = await self.resolve(voice)
        async with self._client.stream(
            "POST", "/speak", params={"model": model}, json={"text": text[:MAX_TTS_CHARS]}
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()
