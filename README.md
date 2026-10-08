# Lore

A voice-driven Game Master for tabletop role-playing games. Players talk to it through Deepgram (speech-to-text
and text-to-speech); an LLM runs the game; hard game state lives in PostgreSQL and
world lore lives in a vector database.

```
             hosted                               kubernetes (namespace: lore)
  ┌──────────────────────────┐     ┌──────────────────────────────────────────────────┐
  │ Deepgram  (STT / TTS)    │◄───►│  app + MCP servers  (not built yet)               │
  │ LLM API   (world gen, GM)│◄───►│      │                 │                 │         │
  └──────────────────────────┘     │      ▼                 ▼                 ▼         │
                                   │  PostgreSQL 18      Qdrant 1.19     Ollama (CPU)   │
                                   │  HP, inventory,     lore, story,    nomic-embed-   │
                                   │  characters, …      NPCs (vectors)  text, 768-dim  │
                                   └──────────────────────────────────────────────────┘
```

## Layout

```
infra/
├── deploy.sh      # the only thing you run
├── values.yaml    # site config: images, sizes, models, scheduling
└── chart/         # local Helm chart (Postgres, Qdrant, embeddings, config, network policy)
```

## Deploy / upgrade

```sh
cd infra
./deploy.sh                    # idempotent install or upgrade
./deploy.sh set-key deepgram   # paste your Deepgram key (stored in lore-credentials)
./deploy.sh set-key llm        # paste your LLM provider key
./deploy.sh status
```

Keys can also be passed at deploy time: `DEEPGRAM_API_KEY=… LLM_API_KEY=… ./deploy.sh`.

## What's in the cluster

| Component  | Service (in `lore`)       | Port        | Storage            | Notes |
|------------|---------------------------|-------------|--------------------|-------|
| PostgreSQL | `lore-postgres`           | 5432        | 5Gi local-path     | db `lore`, user `lore` |
| Qdrant     | `lore-qdrant`             | 6333 / 6334 | 5Gi local-path     | REST / gRPC, `api-key` header required |
| Embeddings | `lore-embeddings`         | 11434       | 2Gi model cache    | Ollama API, `POST /api/embed` |

Images are multi-arch (amd64/arm64) and sized for small nodes (e.g. Raspberry Pis).
A NetworkPolicy only admits traffic from pods in the `lore` namespace.
Nothing is exposed through an ingress yet; the app will add one when it exists.

## Wiring the app

Everything an app or MCP server needs is in two objects:

```yaml
envFrom:
  - configMapRef: { name: lore-config }   # PGHOST, PGPORT, PGDATABASE, PGUSER, QDRANT_URL,
                                          # EMBEDDINGS_URL/MODEL/DIMENSIONS, LLM_PROVIDER/MODEL,
                                          # DEEPGRAM_STT_MODEL/TTS_MODEL
env:
  - { name: PGPASSWORD,       valueFrom: { secretKeyRef: { name: lore-credentials, key: postgres-password } } }
  - { name: QDRANT_API_KEY,   valueFrom: { secretKeyRef: { name: lore-credentials, key: qdrant-api-key } } }
  - { name: DEEPGRAM_API_KEY, valueFrom: { secretKeyRef: { name: lore-credentials, key: deepgram-api-key } } }
  - { name: LLM_API_KEY,      valueFrom: { secretKeyRef: { name: lore-credentials, key: llm-api-key } } }
```

Qdrant collections should be created with `size` = `EMBEDDINGS_DIMENSIONS` (768)
and cosine distance. Changing the embedding model means re-embedding every collection.

## Local development

```sh
./deploy.sh forward    # localhost:5432 Postgres, :6333 Qdrant, :11434 embeddings
./deploy.sh secret postgres-password
./deploy.sh secret qdrant-api-key
```

## Ops

```sh
./deploy.sh psql                     # psql shell (or: ./deploy.sh psql -c 'select 1')
./deploy.sh qdrant /collections      # Qdrant REST with the API key; extra args go to curl
./deploy.sh embed "some text"        # smoke-test the embedding model
./deploy.sh logs qdrant              # postgres | qdrant | embeddings
./deploy.sh diff                     # what an upgrade would change
./deploy.sh uninstall                # keeps PVCs and the credentials secret
```

## Caveats

- **No backups yet.** local-path volumes live on a single node's disk;
  losing that disk loses the campaign. Add `pg_dump` and Qdrant
  snapshot CronJobs to off-node storage before relying on it.
- Pods are pinned to whichever node their volume was created on.
- New pods may get "connection refused" for their first second or two while the
  network policy is applied, so connect with retries on startup.
