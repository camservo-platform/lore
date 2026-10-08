"""Runtime configuration, read from the environment (the lore-config ConfigMap and
lore-credentials Secret in the cluster).

Postgres uses libpq's standard variables (PGHOST, PGPORT, PGUSER, PGPASSWORD,
PGDATABASE), which asyncpg reads on its own.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    redis_host: str
    redis_port: int
    redis_password: str | None
    embeddings_url: str
    embeddings_model: str
    embeddings_dimensions: int
    # nomic-embed-text is trained with task prefixes; other models want "".
    embeddings_document_prefix: str
    embeddings_query_prefix: str

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            redis_host=env.get("REDIS_HOST", "localhost"),
            redis_port=int(env.get("REDIS_PORT", "6379")),
            redis_password=env.get("REDIS_PASSWORD") or None,
            embeddings_url=env.get("EMBEDDINGS_URL", "http://localhost:11434"),
            embeddings_model=env.get("EMBEDDINGS_MODEL", "nomic-embed-text"),
            embeddings_dimensions=int(env.get("EMBEDDINGS_DIMENSIONS", "768")),
            embeddings_document_prefix=env.get("EMBEDDINGS_DOCUMENT_PREFIX", "search_document: "),
            embeddings_query_prefix=env.get("EMBEDDINGS_QUERY_PREFIX", "search_query: "),
        )
