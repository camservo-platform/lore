-- Races, classes and the abilities they give (see lore/catalog.py).

-- Options a forged world adds to the core catalog (which lives in code), same shape.
CREATE TABLE world_options (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id  bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('race', 'class')),
    name         text NOT NULL,
    option       jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX world_options_name ON world_options (campaign_id, kind, lower(name));

-- How a world names and describes the core options (catalog.apply_theme), keyed by the
-- core option's key: same mechanics, words that fit the setting.
CREATE TABLE world_themes (
    campaign_id  bigint NOT NULL REFERENCES campaigns ON DELETE CASCADE,
    key          text NOT NULL,
    theme        jsonb NOT NULL,
    PRIMARY KEY (campaign_id, key)
);

-- A class may give a shared pool of points that abilities spend (pool_name '' = none).
-- pool_refresh says when it fills again: on recovery or at the start of a session.
ALTER TABLE characters
    ADD COLUMN pool_name    text NOT NULL DEFAULT '',
    ADD COLUMN pool_max     integer NOT NULL DEFAULT 0 CHECK (pool_max >= 0),
    ADD COLUMN pool         integer NOT NULL DEFAULT 0,
    ADD COLUMN pool_refresh text NOT NULL DEFAULT 'recovery' CHECK (pool_refresh IN ('recovery', 'session')),
    ADD CONSTRAINT characters_pool_range CHECK (pool BETWEEN 0 AND pool_max);

-- Abilities copied onto a character when it's made (or granted later), so catalog edits
-- never change a character in play. Either max_uses (NULL = unlimited) limits it, or
-- cost (points from the character's pool, 0 = free) does.
CREATE TABLE character_abilities (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    character_id  bigint NOT NULL REFERENCES characters ON DELETE CASCADE,
    name          text NOT NULL,
    source        text NOT NULL DEFAULT '',
    summary       text NOT NULL DEFAULT '',
    description   text NOT NULL DEFAULT '',
    effect        text NOT NULL DEFAULT '',
    level         integer NOT NULL DEFAULT 1 CHECK (level >= 1),
    max_uses      integer CHECK (max_uses > 0),
    uses_left     integer,
    cost          integer CHECK (cost >= 0),
    refresh       text NOT NULL DEFAULT 'recovery' CHECK (refresh IN ('recovery', 'session')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK ((max_uses IS NULL AND uses_left IS NULL) OR uses_left BETWEEN 0 AND max_uses),
    CHECK (max_uses IS NULL OR cost IS NULL)
);
CREATE UNIQUE INDEX character_abilities_name ON character_abilities (character_id, lower(name));
