-- 0002_wiki.sql
-- hermes_wiki schema: documents, links, categories, tags.
-- Same as 01-template-bootstrap.sh; exposed as a migration for DBs
-- that were cloned before wiki support was added.

CREATE SCHEMA IF NOT EXISTS hermes_wiki;

CREATE TABLE IF NOT EXISTS hermes_wiki.documents (
    id          bigserial PRIMARY KEY,
    slug        text UNIQUE NOT NULL,
    title       text NOT NULL,
    body_md     text NOT NULL,
    body_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('english', body_md)) STORED,
    vector_1024 vector(1024),
    category    ltree,
    metadata    jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wiki_tsv         ON hermes_wiki.documents USING GIN (body_tsv);
CREATE INDEX IF NOT EXISTS idx_wiki_vector_1024 ON hermes_wiki.documents USING HNSW (vector_1024 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_wiki_category    ON hermes_wiki.documents USING GIST (category);
CREATE INDEX IF NOT EXISTS idx_wiki_slug        ON hermes_wiki.documents (slug);

CREATE TABLE IF NOT EXISTS hermes_wiki.document_links (
    source_id  bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    target_id  bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    context    text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target ON hermes_wiki.document_links (target_id);

CREATE TABLE IF NOT EXISTS hermes_wiki.tags (
    id   serial PRIMARY KEY,
    name text UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS hermes_wiki.document_tags (
    document_id bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    tag_id      int  REFERENCES hermes_wiki.tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);
