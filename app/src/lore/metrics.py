"""Prometheus metrics for the web app (served on LORE_METRICS_PORT, not through the
ingress), and classification of model errors for metrics and the admin health banner."""

import anthropic
from prometheus_client import Counter, Gauge, Histogram, start_http_server

TURNS = Counter("lore_turns_total", "Game Master turns", ["mode"])
TURN_SECONDS = Histogram(
    "lore_turn_seconds", "Whole GM turn, player message to done", ["mode"],
    buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180),
)
FIRST_NARRATION = Histogram(
    "lore_first_narration_seconds", "Player message to the first narration (when speech can start)", ["mode"],
    buckets=(0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60),
)
# ok: worked; refused: the game said no (normal play, e.g. not enough gold); failed: the
# MCP server couldn't be reached or crashed.
TOOL_CALLS = Counter("lore_tool_calls_total", "MCP tool calls made by the GM", ["tool", "outcome"])
LLM_TOKENS = Counter("lore_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_ERRORS = Counter("lore_llm_errors_total", "Failed LLM requests", ["kind"])
TTS_CHARACTERS = Counter("lore_tts_characters_total", "Characters sent to text-to-speech")
VOICE_SESSIONS = Gauge("lore_voice_sessions", "Open voice conversations")
WORLDS = Counter("lore_worlds_forged_total", "World generations", ["outcome"])
SIGNINS = Counter("lore_signins_total", "Sign-ins", ["provider"])


def serve(port: int) -> None:
    start_http_server(port)


def record_tokens(model: str, usage) -> None:
    for kind, value in (("input", usage.input_tokens), ("output", usage.output_tokens),
                        ("cache_read", getattr(usage, "cache_read_input_tokens", 0)),
                        ("cache_write", getattr(usage, "cache_creation_input_tokens", 0))):
        if value:
            LLM_TOKENS.labels(model, kind).inc(value)


def classify(error: BaseException) -> str:
    """billing | auth | rate_limit | overloaded | server | connection | other."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    if isinstance(error, anthropic.APIConnectionError):
        return "connection"
    if isinstance(error, anthropic.APIStatusError):
        message = str(error.message).lower()
        if "credit balance" in message or "billing" in message:
            return "billing"
        if error.status_code in (401, 403):
            return "auth"
        if error.status_code == 429:
            return "rate_limit"
        if error.status_code in (503, 529):
            return "overloaded"
        if error.status_code >= 500:
            return "server"
    return "other"
