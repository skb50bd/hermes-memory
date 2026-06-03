-- 0003_journal.sql
-- hermes_journal schema: conversation logs, partitioned by month.

CREATE SCHEMA IF NOT EXISTS hermes_journal;

CREATE TABLE IF NOT EXISTS hermes_journal.sessions (
    id         bigserial PRIMARY KEY,
    profile    text NOT NULL,
    started_at timestamptz DEFAULT now(),
    ended_at   timestamptz,
    metadata   jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_journal_sessions_profile ON hermes_journal.sessions (profile, started_at DESC);

CREATE TABLE IF NOT EXISTS hermes_journal.messages (
    id          bigserial,
    session_id  bigint REFERENCES hermes_journal.sessions(id) ON DELETE CASCADE,
    ts          timestamptz DEFAULT now() NOT NULL,
    role        text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content     text NOT NULL,
    tool_calls  jsonb,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_journal_messages_session ON hermes_journal.messages (session_id, ts);
CREATE INDEX IF NOT EXISTS idx_journal_messages_tsv     ON hermes_journal.messages USING GIN (content_tsv);

CREATE TABLE IF NOT EXISTS hermes_journal.messages_default PARTITION OF hermes_journal.messages DEFAULT;

CREATE OR REPLACE FUNCTION hermes_journal.ensure_monthly_partition(p_year int, p_month int)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    partition_name text;
    start_date     date;
    end_date       date;
BEGIN
    partition_name := format('hermes_journal.messages_y%sm%s', p_year, lpad(p_month::text, 2, '0'));
    start_date     := make_date(p_year, p_month, 1);
    end_date       := start_date + interval '1 month';
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF hermes_journal.messages FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_date, end_date
    );
END;
$$;

DO $$
DECLARE d date;
BEGIN
    FOR d IN
        SELECT (current_date + (n || ' months')::interval)::date
        FROM generate_series(0, 2) AS n
    LOOP
        PERFORM hermes_journal.ensure_monthly_partition(extract(year from d)::int, extract(month from d)::int);
    END LOOP;
END;
$$;
