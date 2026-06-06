-- 0007_wiki_chunks.sql
-- Large-document support for hermes_wiki: chunks, versions, auto-links.

CREATE SCHEMA IF NOT EXISTS hermes_wiki;

-- ── Document versions ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_wiki.document_versions (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    version     integer NOT NULL,
    body_md     text NOT NULL,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    metadata    jsonb DEFAULT '{}'::jsonb,
    UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_wiki_versions_doc ON hermes_wiki.document_versions (document_id, version DESC);

-- ── Document chunks ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_wiki.document_chunks (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    version_id  bigint REFERENCES hermes_wiki.document_versions(id) ON DELETE CASCADE,
    ordinal     integer NOT NULL,
    heading_path text,
    anchor      text,
    char_start  integer NOT NULL,
    char_end    integer NOT NULL,
    content     text NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    vector_768  vector(768),
    vector_1024 vector(1024),
    vector_1536 vector(1536),
    token_count integer,
    metadata    jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_doc ON hermes_wiki.document_chunks (document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_tsv ON hermes_wiki.document_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_vector_768  ON hermes_wiki.document_chunks USING HNSW (vector_768  vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_vector_1024 ON hermes_wiki.document_chunks USING HNSW (vector_1024 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_vector_1536 ON hermes_wiki.document_chunks USING HNSW (vector_1536 vector_cosine_ops);

-- ── Link candidates (auto-suggested links, pending confirmation) ──────
CREATE TABLE IF NOT EXISTS hermes_wiki.link_candidates (
    id          bigserial PRIMARY KEY,
    source_doc_id bigint NOT NULL REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    target_doc_id bigint NOT NULL REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    kind        text NOT NULL DEFAULT 'related',
    confidence  double precision NOT NULL DEFAULT 0.0,
    context     text,
    source_span jsonb,  -- {chunk_id, char_start, char_end}
    target_span jsonb,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_doc_id, target_doc_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_wiki_candidates_source ON hermes_wiki.link_candidates (source_doc_id, status);
CREATE INDEX IF NOT EXISTS idx_wiki_candidates_target ON hermes_wiki.link_candidates (target_doc_id, status);
CREATE INDEX IF NOT EXISTS idx_wiki_candidates_conf ON hermes_wiki.link_candidates (confidence DESC) WHERE status = 'pending';

-- ── Document source provenance ────────────────────────────────────────
-- Add source columns to documents if not present
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hermes_wiki' AND table_name = 'documents' AND column_name = 'source_uri'
    ) THEN
        ALTER TABLE hermes_wiki.documents ADD COLUMN source_uri text;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hermes_wiki' AND table_name = 'documents' AND column_name = 'source_mime'
    ) THEN
        ALTER TABLE hermes_wiki.documents ADD COLUMN source_mime text;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hermes_wiki' AND table_name = 'documents' AND column_name = 'source_checksum'
    ) THEN
        ALTER TABLE hermes_wiki.documents ADD COLUMN source_checksum text;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hermes_wiki' AND table_name = 'documents' AND column_name = 'imported_at'
    ) THEN
        ALTER TABLE hermes_wiki.documents ADD COLUMN imported_at timestamptz;
    END IF;
END $$;
