"""Deepgram speech-to-text and text-to-speech, proxied so the API key stays server-side."""

from collections.abc import AsyncIterator

import httpx

API = "https://api.deepgram.com/v1"
# Deepgram's per-request text limit for /speak.
MAX_TTS_CHARS = 2000


class Voice:
    def __init__(self, api_key: str, stt_model: str, tts_model: str):
        self._client = httpx.AsyncClient(
            base_url=API, headers={"Authorization": f"Token {api_key}"}, timeout=httpx.Timeout(60, connect=10)
        )
        self._stt_model = stt_model
        self._tts_model = tts_model

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        resp = await self._client.post(
            "/listen",
            params={"model": self._stt_model, "smart_format": "true"},
            headers={"Content-Type": content_type},
            content=audio,
        )
        resp.raise_for_status()
        return resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"]

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
