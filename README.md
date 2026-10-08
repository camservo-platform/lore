# Lore

A voice-driven Game Master for tabletop role-playing games. Players talk to it through Deepgram (speech-to-text
and text-to-speech); an LLM runs the game; hard game state, an event log and world
lore live in PostgreSQL (lore is searched by meaning with pgvector); Redis carries
live updates between players.

```
            hosted                                    kubernetes (namespace: lore)
 ┌──────────────────────────┐      ┌──────────────────────────────────────────────────────────┐
 │ Deepgram  (STT / TTS)    │◄────►│  voice app  (not built yet)                               │
 │ LLM API   (world gen, GM)│◄────►│     │ MCP                                                 │
 └──────────────────────────┘      │     ▼                                                     │
   players ──HTTPS + basic auth───►│  mcp-game  mcp-lore ──► Ollama (CPU, nomic-embed-text)     │
           (Traefik, X-Lore-User)  │     │         │                                           │
                                   │     ▼         ▼                                           │
                                   │  PostgreSQL 18 + pgvector          Redis 8                 │
                                   │  characters, HP, inventory,        per-campaign event      │
                                   │  sessions, event log, lore         streams, session data   │
                                   └──────────────────────────────────────────────────────────┘
```

## Layout

```
app/                    Python package `lore` (one image for every process)
├── src/lore/
│   ├── migrations/     SQL schema, applied in order by `python -m lore.migrate`
│   ├── mcp/game.py     MCP server: campaigns, characters, HP, inventory, sessions, event log
│   ├── mcp/lore.py     MCP server: world knowledge with semantic search
│   ├── events.py       event log writes + Redis stream fan-out
│   └── embeddings.py   Ollama embedding client
├── tests/              integration tests (real Postgres + Redis)
└── Dockerfile
infra/
├── deploy.sh           the only thing you run
├── values.yaml         site config: images, sizes, models, MCP servers, ingress
├── secrets.env         (gitignored) your API keys; copy from secrets.env.example
├── values.local.yaml   (gitignored) your hostname and ingress annotations
└── chart/              local Helm chart
.github/workflows/app.yml   tests, then builds multi-arch images to ghcr.io/camservo-platform/lore
```

## Deploy / upgrade

```sh
cd infra
cp secrets.env.example secrets.env     # then fill in DEEPGRAM_API_KEY and LLM_API_KEY
$EDITOR values.local.yaml              # ingress host + annotations (see below)
./deploy.sh                            # idempotent install or upgrade
./deploy.sh add-user alice             # create a login; prints the password once
./deploy.sh status
```

`deploy.sh` deploys the image built from the newest commit that touched `app/`, so push
first and wait for the `app` workflow to finish. Override with `APP_TAG=<sha>`.

Example `values.local.yaml`:

```yaml
ingress:
  host: lore.example.com
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
```

Without a host there is no ingress; everything stays cluster-internal.

## What's in the cluster

| Component  | Service (in `lore`) | Port  | Storage         | Notes |
|------------|---------------------|-------|-----------------|-------|
| PostgreSQL | `lore-postgres`     | 5432  | 5Gi local-path  | pgvector image; db `lore`, user `lore` |
| Redis      | `lore-redis`        | 6379  | 1Gi local-path  | password auth, append-only file |
| Embeddings | `lore-embeddings`   | 11434 | 2Gi model cache | Ollama API, `POST /api/embed`, 768 dims |
| MCP game   | `lore-mcp-game`     | 8000  | -               | `/mcp/game`, streamable HTTP (stateless) |
| MCP lore   | `lore-mcp-lore`     | 8000  | -               | `/mcp/lore`, streamable HTTP (stateless) |

Images are multi-arch (amd64/arm64) and sized for small nodes (e.g. Raspberry Pis).
Network policies admit only pods in the `lore` namespace to the data services; the
MCP servers additionally accept the ingress controller.

## Authentication

The ingress puts Traefik basic auth in front of `https://<host>/mcp/game` and
`/mcp/lore`. Logins live in the `lore-users` secret (bcrypt htpasswd), managed with
`./deploy.sh add-user | remove-user | users`. Traefik passes the authenticated name
to the servers as `X-Lore-User` (overwriting anything the client sent), and every
event records it as `actor`. In-cluster callers skip the ingress and are recorded as `gm`.

Connect an MCP client such as Claude Code:

```sh
claude mcp add --transport http lore-game https://<host>/mcp/game \
  --header "Authorization: Basic $(printf 'alice:<password>' | base64)"
```

## MCP servers

**game**: the only place numbers change. Each change writes its event in the same
transaction and then publishes it to Redis.

| Tools | |
|---|---|
| `list_campaigns`, `create_campaign` | campaigns |
| `start_session`, `end_session` | play sessions; events attach to the open one |
| `create_character`, `get_character`, `list_characters`, `update_character` | sheets (PCs have a `player`, NPCs don't) |
| `apply_damage`, `heal`, `grant_temp_hp`, `set_status` | HP with temporary HP; unconscious at 0, or dead if the overflow reaches max HP |
| `add_condition`, `remove_condition` | conditions |
| `add_item`, `remove_item`, `adjust_gold` | inventory (stacking, no overdraw) and gold |
| `move_character` | location |
| `log_event`, `recent_events` | story beats and the log itself |

**lore**: `add_lore` (upsert by kind + title), `search_lore` (semantic, filter by
kind/tags), `get_lore`, `list_lore`, `delete_lore`. Writes are also logged as events.

Names (campaigns, characters, items, lore titles) match case-insensitively, since
they come from speech.

## Event log and Redis

`events` is append-only: who (`actor`), what (`type`, `summary`, `data`), which
character and session. After commit each event is added to the Redis stream
`lore:campaign:<id>:events` (capped at ~1000 entries). A multi-player app reads it
with `XREAD` from the last id each client saw; Postgres stays the source of truth.
Redis is also the place for per-player session data (give those keys a TTL: only
keys with a TTL are evicted under memory pressure).

## Wiring the app

```yaml
envFrom:
  - configMapRef: { name: lore-config }   # PG*, REDIS_HOST/PORT, EMBEDDINGS_*, MCP_GAME_URL,
                                          # MCP_LORE_URL, LLM_PROVIDER/MODEL, DEEPGRAM_*_MODEL
env:
  - { name: PGPASSWORD,       valueFrom: { secretKeyRef: { name: lore-credentials, key: postgres-password } } }
  - { name: REDIS_PASSWORD,   valueFrom: { secretKeyRef: { name: lore-credentials, key: redis-password } } }
  - { name: DEEPGRAM_API_KEY, valueFrom: { secretKeyRef: { name: lore-credentials, key: deepgram-api-key } } }
  - { name: LLM_API_KEY,      valueFrom: { secretKeyRef: { name: lore-credentials, key: llm-api-key } } }
```

## Development

```sh
cd app
docker run -d --name lore-test-pg -e POSTGRES_PASSWORD=lore -p 55432:5432 pgvector/pgvector:0.8.6-pg18
docker run -d --name lore-test-redis -p 56379:6379 redis:8.8.3
uv run pytest
```

Schema changes go in a new `app/src/lore/migrations/NNNN_name.sql`; never edit an
applied one. Against the cluster: `./deploy.sh forward` exposes Postgres :5432,
Redis :6379, embeddings :11434 and the MCP servers on :8001 and :8002.

## Ops

```sh
./deploy.sh psql                     # psql shell (or: ./deploy.sh psql -c 'select 1')
./deploy.sh redis xlen lore:campaign:1:events
./deploy.sh embed "some text"        # smoke-test the embedding model
./deploy.sh logs mcp-game            # postgres | redis | embeddings | mcp-game | mcp-lore
./deploy.sh diff                     # what an upgrade would change
./deploy.sh uninstall                # keeps PVCs and secrets
```

## Caveats

- **No backups yet.** local-path volumes live on a single node's disk; losing that
  disk loses the campaign. Add a `pg_dump` CronJob to off-node storage before relying on it.
- Pods with volumes are pinned to whichever node their volume was created on.
- New pods may get "connection refused" for their first second or two while the
  network policy is applied, so connect with retries on startup.
