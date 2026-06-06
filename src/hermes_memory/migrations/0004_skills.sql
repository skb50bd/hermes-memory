-- 0004_skills.sql
-- hermes_skills schema: catalog index for installed skills. Skills-as-code
-- (the actual SKILL.md files) stay in git; this table is the searchable
-- index (skills-as-data).

CREATE SCHEMA IF NOT EXISTS hermes_skills;

CREATE TABLE IF NOT EXISTS hermes_skills.skills (
    id          bigserial PRIMARY KEY,
    name        text UNIQUE NOT NULL,
    version     text NOT NULL,
    owner       text,
    description text,
    body_tsv    tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED,
    tags        text[] DEFAULT '{}',
    metadata    jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON hermes_skills.skills (name);
CREATE INDEX IF NOT EXISTS idx_skills_tsv  ON hermes_skills.skills USING GIN (body_tsv);
CREATE INDEX IF NOT EXISTS idx_skills_tags ON hermes_skills.skills USING GIN (tags);

CREATE TABLE IF NOT EXISTS hermes_skills.skill_links (
    source_id  bigint REFERENCES hermes_skills.skills(id) ON DELETE CASCADE,
    target_id  bigint REFERENCES hermes_skills.skills(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('depends_on', 'supersedes', 'related', 'see_also')),
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_skill_links_target ON hermes_skills.skill_links (target_id);
