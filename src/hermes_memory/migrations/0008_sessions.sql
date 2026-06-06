-- 0008_sessions.sql
-- Extended session/conversation store to match Hermes Agent SessionDB shape.

CREATE SCHEMA IF NOT EXISTS hermes_sessions;

CREATE TABLE IF NOT EXISTS hermes_sessions.sessions (
    id                text PRIMARY KEY,
    profile           text NOT NULL,
    source            text,
    parent_session_id text,
    title             text,
    model             text,
    system_prompt     text,
    cwd               text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    ended_at          timestamptz,
    end_reason        text,
    archived          boolean NOT NULL DEFAULT false,
    token_count       integer NOT NULL DEFAULT 0,
    message_count     integer NOT NULL DEFAULT 0,
    metadata          jsonb DEFAULT '{}'::jsonb,
    platform          text,
    chat_id           text,
    thread_id         text,
    user_id           text,
    user_name         text,
    gateway_session_key text
);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON hermes_sessions.sessions (profile, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON hermes_sessions.sessions (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON hermes_sessions.sessions (source);
CREATE INDEX IF NOT EXISTS idx_sessions_archived ON hermes_sessions.sessions (archived, started_at DESC);

CREATE TABLE IF NOT EXISTS hermes_sessions.messages (
    id              bigserial PRIMARY KEY,
    session_id      text NOT NULL REFERENCES hermes_sessions.sessions(id) ON DELETE CASCADE,
    timestamp       timestamptz NOT NULL DEFAULT now(),
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content         text NOT NULL,
    tool_calls      jsonb,
    tool_call_id    text,
    model           text,
    token_count     integer,
    metadata        jsonb DEFAULT '{}'::jsonb,
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON hermes_sessions.messages (session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_tsv ON hermes_sessions.messages USING GIN (content_tsv);

-- Compression locks (same semantics as SQLite)
CREATE TABLE IF NOT EXISTS hermes_sessions.compression_locks (
    session_id  text PRIMARY KEY REFERENCES hermes_sessions.sessions(id) ON DELETE CASCADE,
    holder      text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compression_locks_expires ON hermes_sessions.compression_locks (expires_at);
