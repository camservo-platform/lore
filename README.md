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
│   ├── catalog.py      core races and classes, their abilities and limits, per-world theming
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
./deploy.sh deploy --now               # same, without waiting for GM turns in progress
./deploy.sh add-user alice             # create a login; prints the password once
./deploy.sh status
```

### Stable and dev

There are two environments, each a full copy of the stack with its own data:

| | Namespace | Host (in the gitignored local values) | App version |
|---|---|---|---|
| **stable** (default) | `lore` | `values.local.yaml` | pinned in `values-stable.yaml` |
| **dev** | `lore-dev` | `values.dev.local.yaml` | newest build of `main` |

Every `deploy.sh` command works on either: stable by default, dev with `LORE_ENV=dev` (or
a leading `--env dev`). Changes go to dev first, then you promote that exact image:

```sh
git push                                # wait for the app workflow to build the image
LORE_ENV=dev ./deploy.sh                # dev runs the newest build; try it there
./deploy.sh promote                     # pin stable to dev's image, show the diff, deploy on "y"
git commit -m "Promote ..." values-stable.yaml
```

Dev starts with an empty database and a copy of stable's password logins (managed
separately after that). It has no monitoring (stable owns the dashboard and alerts) and
no GitHub sign-in unless you create a second GitHub OAuth app for the dev host and put
its keys in `secrets.env` as `DEV_GITHUB_CLIENT_ID` / `DEV_GITHUB_CLIENT_SECRET`.
`APP_TAG=<sha>` overrides the version for one deploy; rebuild any commit with
`gh workflow run app`.

### Deploys and live sessions

Upgrades don't cut players off. Deployments roll one pod at a time (`maxSurge: 1`,
`maxUnavailable: 0`), so the new pod is ready before the old one stops. Game state, the
GM conversation and the turn lock live in Postgres and Redis, so any pod can serve any
player. The old web pod then **drains** (`app/src/lore/web/drain.py`):

1. `preStop` sleeps `web.drain.preStopSeconds` (5s) while Traefik stops routing to it.
2. On SIGTERM it stops taking new work (new turns and world forging get a 503, and the
   browser retries) and ends live feeds. Browsers reconnect to the new pod and resume
   from the feed's last SSE id (`<events id>,<chat id>`), so no events or turns are
   missed. Turns missed while away appear but aren't read aloud again.
3. Voice sessions stay open until the player isn't mid-sentence and no reply is
   running. Then the pod closes them with code 1012 and the browser reconnects with a
   fresh ticket, keeping the mic open.
4. GM turns and world forging in progress run to completion, for up to
   `web.drain.seconds` (240s). Anything still running then is cancelled cleanly: the
   conversation is saved, the table unlocked and the player told to repeat themselves.
   Game-state changes already made stand.

To skip the wait, use `./deploy.sh deploy --now`, or `./deploy.sh hurry` while an old pod
is still draining. Either one sends the old pods SIGUSR1, which cancels their running
turns as in step 4 and exits right away. (A second SIGTERM does the same.)

The MCP servers are stateless and their requests are short, so a preStop pause plus
uvicorn's own graceful shutdown is enough for them. Migrations run in the new pods' init
containers **while old pods are still serving**, so they must be backward-compatible:
add columns or tables in one release, and drop or rename them in a later one.

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
MCP servers additionally accept the ingress controller. Dev is the same in `lore-dev`.

## Signing in

The web app signs players in itself (`lore/web/auth.py`); both methods end in a session
cookie, sessions live in Redis, and admins can list and revoke them (Admin > Players).

- **GitHub.** Create an OAuth App (GitHub > Settings > Developer settings > OAuth Apps,
  or the org's settings) with homepage `https://<host>` and callback URL
  `https://<host>/auth/github/callback`. Put its `GITHUB_CLIENT_ID` and
  `GITHUB_CLIENT_SECRET` in `infra/secrets.env`, map allowed GitHub logins to Lore
  names in `values.local.yaml`, and redeploy:
  ```yaml
  web:
    githubUsers:
      octocat: alice      # GitHub login -> Lore name
  ```
  Unlisted GitHub accounts are refused.
- **Passwords.** The logins in the `lore-users` secret (bcrypt htpasswd), managed from
  the admin page's **Users** tab or with `./deploy.sh add-user | remove-user | users`
  (both edit the same secret). Choose "Sign in with a password" and the browser asks for
  them; scripts can send Basic credentials on every request. For the Users tab, the web
  pod runs as the `lore-web` service account, whose Role may only `get` and `patch`
  that one secret (`web.manageUsers: false` removes it and makes the tab read-only).

Players sign out by clicking their name. The web route has no proxy auth, and the app
ignores any client-sent `X-Lore-User` (outside local dev).

The MCP servers keep Traefik basic auth on their own ingress, with the same password
logins: Traefik passes the authenticated name as `X-Lore-User` (overwriting anything the
client sent) and every event records it as `actor`. In-cluster callers skip the ingress;
the web app passes the signed-in player's name, and anything else is recorded as `gm`.

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
| `create_character`, `get_character`, `list_characters`, `update_character` | sheets (PCs have a `player` and must pick a race and class; NPCs don't); a level change unlocks abilities |
| `list_character_options`, `theme_core_options`, `add_world_option`, `choose_race_and_class` | races and classes: the core set as this world names it, plus its own |
| `use_ability`, `recover`, `grant_ability` | abilities: spend a use or pool points (refused when spent), restore after a proper rest, grant from the story |
| `apply_damage`, `heal`, `grant_temp_hp`, `set_status` | HP with temporary HP; unconscious at 0, or dead if the overflow reaches max HP |
| `add_condition`, `remove_condition` | conditions |
| `add_item`, `remove_item`, `describe_item`, `adjust_gold` | inventory (stacking, no overdraw; a description, and an origin for anything that isn't ordinary gear) and gold |
| `add_quest`, `update_quest`, `list_quests` | the party's quest log: summary, giver, reward, status and notes |
| `move_character` | location |
| `roll_dice` | dice notation like `d20`, `2d6+1d4-1`; logged when given a campaign |
| `log_event`, `recent_events` | story beats and the log itself |

**lore**: `add_lore` (upsert by kind + title), `search_lore` (semantic, filter by
kind/tags), `get_lore`, `list_lore`, `delete_lore`. Writes are also logged as events.
NPCs are lore entries of kind `npc` with living details (where last seen, alive / dead /
missing, how they feel about the party, appearances) and a history of notes:
`record_npc`, `update_npc`, `get_npc`, `list_npcs`, and `npc_appeared`, which the web
table calls for every named character who speaks (a stub entry for new ones). Story
memory: `record_story` (the web table records every turn) and `recall_story` (by
meaning, across all players and sessions of a world).

Names (campaigns, characters, items, lore titles) match case-insensitively, since
they come from speech.

## Web table

Open `https://<host>/` and sign in.

- **Your worlds** lists existing campaigns; **Forge a new world** takes an optional theme
  and name (leave both blank and the Game Master invents an original world), has Claude
  write the setting, an opening scene and 12-16 linked lore entries, names the core races
  and classes in the world's own terms (a warrior in one world is a gunner in another)
  and adds one or two of its own, records it all through the MCP servers, and drops you
  into the first scene (about a minute).
- **Making a character**: a player with no character at a table gets a picker at the
  bottom of the chat: a name, a race and a class, each with what it means and the
  abilities it gives (players can also just tell the GM). Race and class set HP,
  defense, attributes and abilities. A character made before races and classes gave
  abilities gets the same picker to choose them (its HP and stats stay).
- **Abilities**: the core set is five races and six classes, the same mechanics in every
  world (`lore/catalog.py`). Each class decides how its abilities are limited: either
  each has its own uses (shown as pips), or they share a pool of points (Aether, Grit,
  ...) and each has a cost; and whether they come back when the GM calls a proper rest
  (`recover`) or at the start of each session. Minor abilities are unlimited; race
  abilities come back on rest. The game server refuses an ability that's spent, so the
  GM narrates that it falters. New abilities unlock at higher levels.
- **Text / Speech** (top right, remembered per browser). Speech mode is a hands-free
  conversation (**Start conversation**): Deepgram Flux transcribes as you talk, the GM
  answers when you pause, and its reply is read aloud sentence by sentence as it
  streams. Talking over the GM stops its voice and your words become the next turn. A
  newly forged world starts the conversation by itself, so you can interrupt the
  how-to-play and opening scene too; if the mic can't start (permission denied, or the
  browser wants a click first), the opening plays without it. The GM is told which mode
  the table is in and writes for the ear in speech mode.
- **Act / Ask the GM** (above the text box and the conversation button). Act is the
  story: what your character does or says. Ask the GM steps outside it: rules, options,
  a recap, what your character knows. The GM answers plainly, no time passes and
  nothing changes in the game. It works for typed and spoken turns (a spoken turn uses
  the setting it had when you stopped talking). Each table starts on Act, and
  out-of-character exchanges are shown in a plainer style in the transcript.
- **Full screen** (top bar, where the browser supports it). The screen, and so the
  computer, is kept awake (Screen Wake Lock) while in full screen or in a voice
  conversation, so a long scene isn't cut off by sleep.
- The **Party** and **Chronicle** panels update live from the event stream. Other
  players at the same campaign see each other's turns as they finish.
- **New conversation** clears the GM's conversation memory for that campaign (the world,
  characters and chronicle stay); the GM then recaps from the chronicle and lore.

- **Dice**: the sidebar's dice tray rolls for you (quick dice or notation like `2d6+3`,
  for one of your characters); every roll at the table, the GM's included, appears in the
  story as a card and goes in the chronicle under the roller's name.
- **Quest log**: the sidebar lists the party's quests, active first (finished ones
  folded away). Click one for its summary, who gave it, the reward and its notes: the
  GM's clues and progress, and players' own notes (add one from the same dialog). A new
  world's starter quest starts the log.
- **Items**: click any item in a sheet for its description; anything that isn't
  ordinary gear (marked ✦) also says where it came from.
- **NPCs**: every named character the GM voices gets a record in that world: the web
  table notes each appearance after a turn and makes a stub for anyone new, which the
  GM is asked to fill in. NPCs keep a history (deals, debts, betrayals), and each turn
  the GM sees who's known at the party's location and who could turn up again.
- **Story memory**: every turn (what the player did, and what happened) is embedded
  into the world's story memory. Each turn, the moments most related to what the player
  just said are recalled for the GM, from any player or session, so decisions carry
  consequences for the whole table later on.
- **Character sheets**: the Party panel shows each character's stats, abilities (with
  uses or pool left; click one for what it does and how often), conditions and
  inventory, yours first and expanded.
- **Voices**: pick the GM's voice in speech mode. Non-player characters' speech is
  tagged by the GM (`<say who="..." voice="feminine|masculine">`) and read in a voice of
  their own, assigned once per world (admins can change it under Worlds > NPC voices).
- **Session recaps**: when the GM ends a session, its "previously on..." recap is saved
  as a `history` lore entry, and a new conversation starts from the latest recap.

One conversation per campaign is shared by the whole table and kept in Redis; a Redis
lock makes players take turns (a second player gets "the GM is answering another
player"). The conversation is append-only, as Claude requires, and server-side
compaction keeps long sessions within the context window. Requests opt into Claude's
server-side `fallbacks: "default"`, so a declined request is retried on another model.

The GM's thinking depth is `llm.effort` in `values.yaml` (`medium`); lower it for
snappier voice play.

## Admin view

Users listed in `web.admins` (set it in `values.local.yaml`, e.g. `admins: [alice]`) get
an **Admin** button with:

- **SQL**: runs one statement against the game database. Read-only unless **Allow
  writes** is ticked; 15 s statement timeout; the first 500 rows are shown; embeddings
  display as a short preview. Example queries and a schema browser (double-click a table
  to select from it) are built in. Every query is logged with the admin's username
  (`./deploy.sh logs web`).
- **Vector search**: semantic search over all lore (or one campaign / kind), with cosine
  similarity scores, using the same embedding model as the lore server.

- **Worlds**: rename or delete a world (deleting asks for its exact name and also clears
  its GM conversation and feeds), and correct character sheets: name, player (or none
  for an NPC), level, HP, max/temporary HP, defense, gold, status. A character can also
  be deleted from its edit dialog, which removes it and its inventory; its past events stay
  in the chronicle. Each change goes in the world's chronicle with exactly what changed,
  and players at that table see it live (a renamed world's title updates; a deleted
  character leaves the party; a deleted world sends them back to the lobby).
- **Players**: who has Lore open now and when everyone was last seen, where they are
  (lobby, admin, which world) and whether a voice conversation is on. There are no
  login sessions with basic auth, so "online" means the page checked in within 90 s.
- **Users**: everyone who can sign in, how (password, GitHub), whether they're an admin
  and how many sessions they have. Add password users, set or generate a new password
  (a generated one is shown once), delete a login, or sign someone out everywhere.
  Changing a password or deleting a user signs out their password sessions; their
  characters stay. Admins and GitHub sign-ins stay in values (`web.admins`,
  `web.githubUsers`). The pod that made a change sees it at once; other replicas, and
  Traefik for the MCP ingress, within about a minute.
- **Tables**: each world's GM conversation (messages, size, mode, last turn). Clear a
  stuck turn lock, or reset the conversation.
- **Usage**: per day, LLM tokens by model with an estimated cost at list prices (see
  `lore/usage.py`), GM turns per player, speech characters and voice minutes. Kept for
  120 days in Redis.

Non-admins get 403 from the admin API. The web pod connects to Postgres directly for
this, as the app's database user, and every admin action is logged with the admin's
name (`./deploy.sh logs web`).

- **Lore** (in Vector search): leave the query empty and pick a world to browse its lore;
  edit entries (re-embedded for search), merge one into another, or delete them.
- **Health banner**: model request failures in the last 24 h by kind (billing, auth,
  rate limits, ...) with the latest message; admins also get a toast on sign-in if
  anything failed in the last hour.

## Monitoring

With `monitoring.enabled` (default) the chart adds, for kube-prometheus-stack:

- a ServiceMonitor for the web app's metrics port (9100, not exposed through the
  ingress): turns and their duration, time until speech starts, tool calls by outcome
  (`ok`, `refused` = normal play, `failed` = a game server unreachable), LLM tokens by
  model and errors by kind, speech characters, open voice conversations, sign-ins,
  worlds forged;
- alert rules: billing/auth errors from the model (play has stopped), repeated model
  errors, failing game servers, slow spoken turns, and the web app down. There is no
  Alertmanager in the cluster yet, so these show as firing in Prometheus/Grafana rather
  than notifying anyone; the Admin health banner covers model errors in the app itself;
- a "Lore" Grafana dashboard (loaded by Grafana's dashboard sidecar).

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
./deploy.sh hurry                    # stop old web pods still draining after a deploy
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

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Contributors sign a
[Contributor License Agreement](CLA.md) once, via a comment on their first pull request.
