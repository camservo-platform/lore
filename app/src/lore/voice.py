"""Deepgram text-to-speech, proxied so the API key stays server-side. Speech-to-text is
live, over Flux: see lore.web.conversation."""

from collections.abc import AsyncIterator

import httpx

API = "https://api.deepgram.com/v1"
# Deepgram's per-request text limit for /speak.
MAX_TTS_CHARS = 2000


class Voice:
    def __init__(self, api_key: str, tts_model: str):
        self._client = httpx.AsyncClient(
            base_url=API, headers={"Authorization": f"Token {api_key}"}, timeout=httpx.Timeout(60, connect=10)
        )
        self._tts_model = tts_model

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        """MP3 audio for `text`, streamed as Deepgram produces it."""
        async with self._client.stream(
            "POST", "/speak", params={"model": self._tts_model}, json={"text": text[:MAX_TTS_CHARS]}
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()
