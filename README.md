# Lore

A voice-driven Game Master for tabletop role-playing games. Players sit at a web table
and type or talk (Deepgram speech-to-text and text-to-speech); Claude runs the game
through two MCP servers; hard game state, an event log and world lore live in
PostgreSQL (lore is searched by meaning with pgvector); Redis carries the shared
conversation and live updates between players.

```
            hosted                                    kubernetes (namespace: lore)
 ┌──────────────────────────┐      ┌──────────────────────────────────────────────────────────┐
 │ Deepgram  (STT / TTS)    │◄────►│  web: UI + Game Master (Claude tool loop)                 │
 │ Claude    (world gen, GM)│◄────►│     │ MCP, as the signed-in player                        │
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
│   ├── mcp/game.py     MCP server: campaigns, characters, HP, inventory, dice, sessions, event log
│   ├── mcp/lore.py     MCP server: world knowledge with semantic search
│   ├── gm.py           the Game Master: Claude conversation using the MCP tools
│   ├── worldgen.py     new-world generation
│   ├── voice.py        Deepgram STT/TTS
│   ├── web/            web table (Starlette API + static HTML/JS UI, no build step)
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

`deploy.sh` deploys the image built from the newest commit that touched `app/` (or the
`app` workflow), so push first and wait for the workflow to finish. Override with
`APP_TAG=<sha>`; rebuild any commit with `gh workflow run app`.

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
| Web        | `lore-web`          | 8000  | -               | `/`, the UI and its API |

Images are multi-arch (amd64/arm64) and sized for small nodes (e.g. Raspberry Pis).
Network policies admit only pods in the `lore` namespace to the data services; the
MCP servers additionally accept the ingress controller.

## Authentication

The ingress puts Traefik basic auth in front of everything: the web UI at
`https://<host>/` and the MCP servers at `/mcp/game` and `/mcp/lore`. Logins live in the `lore-users` secret (bcrypt htpasswd), managed with
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
| `roll_dice` | dice notation like `d20`, `2d6+1d4-1`; logged when given a campaign |
| `log_event`, `recent_events` | story beats and the log itself |

**lore**: `add_lore` (upsert by kind + title), `search_lore` (semantic, filter by
kind/tags), `get_lore`, `list_lore`, `delete_lore`. Writes are also logged as events.

Names (campaigns, characters, items, lore titles) match case-insensitively, since
they come from speech.

## Web table

Open `https://<host>/` and sign in.

- **Your worlds** lists existing campaigns; **Forge a new world** takes a theme (and an
  optional name), has Claude write the setting, an opening scene and 12-16 linked lore
  entries, records them through the MCP servers, and drops you into the first scene
  (about a minute).
- **Text / Speech** (top right, remembered per browser). Speech mode records with the
  mic button or by holding the space bar, transcribes with Deepgram, and reads the Game
  Master's reply aloud sentence by sentence as it streams. The GM is told which mode the
  table is in and writes for the ear in speech mode.
- The **Party** and **Chronicle** panels update live from the event stream. Other
  players at the same campaign see each other's turns as they finish.
- **New conversation** clears the GM's conversation memory for that campaign (the world,
  characters and chronicle stay); the GM then recaps from the chronicle and lore.

One conversation per campaign is shared by the whole table and kept in Redis; a Redis
lock makes players take turns (a second player gets "the GM is answering another
player"). The conversation is append-only, as Claude requires, and server-side
compaction keeps long sessions within the context window. Requests opt into Claude's
server-side `fallbacks: "default"`, so a declined request is retried on another model.

The GM's thinking depth is `llm.effort` in `values.yaml` (`medium`); lower it for
snappier voice play.

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

### Run the game locally against the cluster

```sh
cd infra
./deploy.sh dev                 # http://localhost:8080, signed in as your local username
```

This runs the web UI and both MCP servers from your working tree (the UI reloads on
Python changes; HTML/JS/CSS need only a browser refresh) and port-forwards the cluster's
Postgres, Redis and embeddings, so no image build is involved. Configuration and API
keys come from the cluster (or `secrets.env`). `DEV_MCP=cluster ./deploy.sh dev` uses
the cluster's MCP servers instead; `LORE_DEV_USER=dana` plays as someone else;
`DEV_WEB=9000` changes the port.

It's the real campaign data. Migrations are not run automatically; apply a new one
deliberately with `uv run python -m lore.migrate` (with the same env) once it's ready.

### Tests

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
./deploy.sh logs web                 # postgres | redis | embeddings | mcp-game | mcp-lore | web
./deploy.sh diff                     # what an upgrade would change
./deploy.sh uninstall                # keeps PVCs and secrets
```

## Caveats

- **No backups yet.** local-path volumes live on a single node's disk; losing that
  disk loses the campaign. Add a `pg_dump` CronJob to off-node storage before relying on it.
- Pods with volumes are pinned to whichever node their volume was created on.
- New pods may get "connection refused" for their first second or two while the
  network policy is applied, so connect with retries on startup.

## License

Copyright (c) 2026 Cameron Jeffries.

Lore is free software under the [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). You may use, modify and redistribute it, including commercially,
provided that you release the source of any modified version you distribute or run as
a network service, under the same license.

Commercial licenses without the AGPL's obligations are available from the author.
