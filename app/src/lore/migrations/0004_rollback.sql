-- Rolling a world back to before a turn (a mistake, or speech heard wrong).

-- The world's changeable state, saved before each GM turn (and before a character is made
-- from the picker). Rows are kept as JSON with their ids, so a restore puts back exactly
-- what was there; lore embeddings are left out and recomputed only for entries whose
-- text changed. Only the latest snapshots per world are kept (see game.SNAPSHOTS_KEPT).
CREATE TABLE snapshots (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id     bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    turn_id         text NOT NULL,
    -- Everything after these was done after the snapshot.
    last_event_id   bigint NOT NULL DEFAULT 0,
    last_story_id   bigint NOT NULL DEFAULT 0,
    state           jsonb NOT NULL,
    -- The web table's own state at that point (the GM conversation's length).
    extra           jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX snapshots_campaign ON snapshots (campaign_id, id DESC);

-- The event log stays append-only: a rollback records which events it undid, and reads
-- leave those out (events.UNDONE).
CREATE TABLE rollbacks (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id     bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    kept_event_id   bigint NOT NULL,   -- the last event still in effect
    last_event_id   bigint NOT NULL,   -- the newest event undone
    actor           text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rollbacks_campaign ON rollbacks (campaign_id);
