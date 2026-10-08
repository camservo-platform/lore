-- Quest log, NPC details and histories, where unusual items came from, and story memory.

-- Quests the party has taken on, per world. Notes record progress, clues and leads.
CREATE TABLE quests (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id   bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    title         text NOT NULL,
    summary       text NOT NULL DEFAULT '',
    giver         text NOT NULL DEFAULT '',
    reward        text NOT NULL DEFAULT '',
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'failed', 'abandoned')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE UNIQUE INDEX quests_title ON quests (campaign_id, lower(title));

CREATE TABLE quest_notes (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quest_id    bigint NOT NULL REFERENCES quests ON DELETE CASCADE,
    note        text NOT NULL,
    actor       text NOT NULL DEFAULT 'gm',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX quest_notes_quest ON quest_notes (quest_id, id);

-- Worlds forged before the quest log kept their starter quest as a lore storyline
-- ("Given by X. Goal: ... Reward: ..."): give each its quest.
INSERT INTO quests (campaign_id, title, summary, giver, reward, created_at)
SELECT campaign_id, substring(title FROM '^Starter quest: (.*)$'), content,
       coalesce(substring(content FROM '^Given by (.*?)\. Goal:'), ''),
       coalesce(substring(content FROM 'Reward: (.*)$'), ''), created_at
FROM lore_entries
WHERE tags @> ARRAY['starter-quest'] AND title LIKE 'Starter quest: %'
ON CONFLICT DO NOTHING;

-- The living side of an NPC lore entry: where they're found, whether they're alive,
-- how they feel about the party, and how often they've turned up.
CREATE TABLE npc_details (
    lore_id      bigint PRIMARY KEY REFERENCES lore_entries ON DELETE CASCADE,
    location     text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'alive' CHECK (status IN ('alive', 'dead', 'missing', 'unknown')),
    disposition  text NOT NULL DEFAULT 'unknown'
                 CHECK (disposition IN ('friendly', 'neutral', 'wary', 'hostile', 'unknown')),
    voice        text NOT NULL DEFAULT '' CHECK (voice IN ('', 'feminine', 'masculine')),
    appearances  integer NOT NULL DEFAULT 0 CHECK (appearances >= 0),
    first_seen   timestamptz,
    last_seen    timestamptz,
    stub         boolean NOT NULL DEFAULT false  -- made automatically; the GM should fill it in
);
INSERT INTO npc_details (lore_id) SELECT id FROM lore_entries WHERE kind = 'npc';

-- A history for any lore entry, NPCs above all: what happened with them, in order.
CREATE TABLE lore_notes (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lore_id     bigint NOT NULL REFERENCES lore_entries ON DELETE CASCADE,
    note        text NOT NULL,
    session_id  bigint REFERENCES game_sessions ON DELETE SET NULL,
    actor       text NOT NULL DEFAULT 'gm',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lore_notes_lore ON lore_notes (lore_id, id);

-- Where an item came from, for anything that isn't ordinary gear ('' for ordinary).
ALTER TABLE inventory_items ADD COLUMN origin text NOT NULL DEFAULT '';

-- Everything that happened at the table, turn by turn, searchable by meaning: what a
-- player did and how the GM narrated it. Recalled into later turns (for any player at
-- the table) so choices keep their consequences.
CREATE TABLE story_moments (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    session_id  bigint REFERENCES game_sessions ON DELETE SET NULL,
    player      text NOT NULL DEFAULT '',
    said        text NOT NULL DEFAULT '',
    narration   text NOT NULL,
    embedding   vector(768) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX story_moments_embedding ON story_moments USING hnsw (embedding vector_cosine_ops);
CREATE INDEX story_moments_campaign ON story_moments (campaign_id, id DESC);
