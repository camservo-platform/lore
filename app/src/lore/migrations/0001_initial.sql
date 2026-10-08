-- Game state (hard values), the append-only event log, and vector lore.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE campaigns (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    setting     text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Names come from speech-to-text, so lookups ignore case.
CREATE UNIQUE INDEX campaigns_name ON campaigns (lower(name));

-- One row per authenticated user (the X-Lore-User header set by the ingress).
CREATE TABLE players (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username    text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Player characters and NPCs (player_id IS NULL).
CREATE TABLE characters (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id  bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    player_id    bigint REFERENCES players ON DELETE SET NULL,
    name         text NOT NULL,
    race         text NOT NULL DEFAULT '',
    class        text NOT NULL DEFAULT '',
    level        integer NOT NULL DEFAULT 1 CHECK (level >= 1),
    max_hp       integer NOT NULL CHECK (max_hp > 0),
    hp           integer NOT NULL,
    temp_hp      integer NOT NULL DEFAULT 0 CHECK (temp_hp >= 0),
    defense      integer NOT NULL DEFAULT 10,
    -- Free-form stat scores, e.g. {"strength": 12, "agility": 14}.
    attributes   jsonb NOT NULL DEFAULT '{}',
    conditions   text[] NOT NULL DEFAULT '{}',
    gold         integer NOT NULL DEFAULT 0 CHECK (gold >= 0),
    location     text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'alive' CHECK (status IN ('alive', 'unconscious', 'dead')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (hp BETWEEN 0 AND max_hp)
);
CREATE UNIQUE INDEX characters_name ON characters (campaign_id, lower(name));

CREATE TABLE inventory_items (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    character_id  bigint NOT NULL REFERENCES characters ON DELETE CASCADE,
    name          text NOT NULL,
    quantity      integer NOT NULL CHECK (quantity > 0),
    description   text NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX inventory_items_name ON inventory_items (character_id, lower(name));

-- A sitting at the table. At most one open session per campaign.
CREATE TABLE game_sessions (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id  bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    summary      text NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX game_sessions_one_open ON game_sessions (campaign_id) WHERE ended_at IS NULL;

-- Append-only record of everything that happened. State-changing tools write
-- their event in the same transaction as the change.
CREATE TABLE events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id   bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    session_id    bigint REFERENCES game_sessions ON DELETE SET NULL,
    character_id  bigint REFERENCES characters ON DELETE SET NULL,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    -- Who made the call: the authenticated user, or "gm" for in-cluster services.
    actor         text NOT NULL,
    type          text NOT NULL,
    summary       text NOT NULL,
    data          jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_campaign ON events (campaign_id, id DESC);
CREATE INDEX events_character ON events (character_id, id DESC) WHERE character_id IS NOT NULL;

-- World knowledge, searched by meaning. Dimension must match the embedding model.
CREATE TABLE lore_entries (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id  bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    kind         text NOT NULL,
    title        text NOT NULL,
    content      text NOT NULL,
    tags         text[] NOT NULL DEFAULT '{}',
    embedding    vector(768) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX lore_entries_title ON lore_entries (campaign_id, kind, lower(title));
CREATE INDEX lore_entries_embedding ON lore_entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX lore_entries_campaign_kind ON lore_entries (campaign_id, kind);
