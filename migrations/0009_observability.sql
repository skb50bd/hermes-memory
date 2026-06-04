-- 0009_observability.sql
-- Structured logs, traces, spans, LLM calls, tool calls in Timescale.

CREATE SCHEMA IF NOT EXISTS hermes_observability;

-- ── Logs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_observability.logs (
    ts          timestamptz NOT NULL,
    level       text NOT NULL,
    logger      text NOT NULL,
    message     text NOT NULL,
    exception   text,
    profile     text,
    session_id  text,
    task_id     text,
    platform    text,
    metadata    jsonb DEFAULT '{}'::jsonb
);
SELECT create_hypertable('hermes_observability.logs', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_logs_level ON hermes_observability.logs (level, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_logger ON hermes_observability.logs (logger, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_session ON hermes_observability.logs (session_id, ts DESC);

-- ── Traces ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_observability.traces (
    ts          timestamptz NOT NULL,
    trace_id    text NOT NULL,
    profile     text NOT NULL,
    session_id  text,
    task_id     text,
    name        text NOT NULL,
    metadata    jsonb DEFAULT '{}'::jsonb
);
SELECT create_hypertable('hermes_observability.traces', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_traces_id ON hermes_observability.traces (trace_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_traces_session ON hermes_observability.traces (session_id, ts DESC);

-- ── Spans ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_observability.spans (
    ts          timestamptz NOT NULL,
    trace_id    text NOT NULL,
    span_id     text NOT NULL,
    parent_id   text,
    name        text NOT NULL,
    start_ts    timestamptz NOT NULL,
    end_ts      timestamptz,
    duration_ms double precision,
    metadata    jsonb DEFAULT '{}'::jsonb
);
SELECT create_hypertable('hermes_observability.spans', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON hermes_observability.spans (trace_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_spans_id ON hermes_observability.spans (span_id);

-- ── LLM calls ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_observability.llm_calls (
    ts          timestamptz NOT NULL,
    trace_id    text,
    span_id     text,
    profile     text NOT NULL,
    session_id  text,
    model       text,
    provider    text,
    prompt_tokens    integer,
    completion_tokens integer,
    total_tokens     integer,
    latency_ms       double precision,
    cost_usd         double precision,
    metadata    jsonb DEFAULT '{}'::jsonb
);
SELECT create_hypertable('hermes_observability.llm_calls', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_llm_session ON hermes_observability.llm_calls (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_model ON hermes_observability.llm_calls (model, ts DESC);

-- ── Tool calls ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_observability.tool_calls (
    ts          timestamptz NOT NULL,
    trace_id    text,
    span_id     text,
    profile     text NOT NULL,
    session_id  text,
    tool_name   text NOT NULL,
    tool_call_id text,
    latency_ms   double precision,
    success      boolean,
    error        text,
    metadata    jsonb DEFAULT '{}'::jsonb
);
SELECT create_hypertable('hermes_observability.tool_calls', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_tool_session ON hermes_observability.tool_calls (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tool_name ON hermes_observability.tool_calls (tool_name, ts DESC);
