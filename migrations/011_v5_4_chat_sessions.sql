-- Migration 011: V5.4 hierarchical per-provider chat session storage
-- Spec: V5_4_ADDENDUM.md §2.1

CREATE TABLE chat_sessions (
    id                  TEXT PRIMARY KEY,                  -- ulid
    provider_id         TEXT NOT NULL,                     -- 'openai' | 'anthropic' | 'google' | ...
    external_session_id TEXT NOT NULL,                     -- AI provider's URL session hash
    title               TEXT,                              -- inferred from first user prompt
    first_seen          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    turn_count          INTEGER DEFAULT 0,

    UNIQUE (provider_id, external_session_id)
);

CREATE INDEX idx_chat_sessions_provider ON chat_sessions(provider_id);
CREATE INDEX idx_chat_sessions_last_seen ON chat_sessions(last_seen DESC);

-- Scope existing tables to chat sessions.
-- rationale_log lives in the memory_layer core (V3); apply when present.
ALTER TABLE rationale_log ADD COLUMN chat_session_id TEXT REFERENCES chat_sessions(id);
ALTER TABLE telemetry_session_timeline ADD COLUMN chat_session_id TEXT REFERENCES chat_sessions(id);

CREATE INDEX idx_rationale_log_chat ON rationale_log(chat_session_id);
CREATE INDEX idx_telemetry_chat ON telemetry_session_timeline(chat_session_id);
