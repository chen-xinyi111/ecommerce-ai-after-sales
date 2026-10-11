CREATE TABLE commerce.chat_threads (
    id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES commerce.users(id),
    order_id text NOT NULL REFERENCES commerce.orders(id),
    title text NOT NULL CHECK(length(title) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_threads_owner_idx ON commerce.chat_threads(user_id, updated_at DESC);
CREATE TABLE commerce.chat_runs (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES commerce.chat_threads(id),
    request_id uuid NOT NULL,
    session_hash text NOT NULL,
    status text NOT NULL CHECK(status IN ('queued','running','completed','failed')),
    next_event_id integer NOT NULL DEFAULT 0 CHECK(next_event_id>=0),
    error_code text,
    usage jsonb NOT NULL DEFAULT '{}',
    model text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(thread_id, request_id)
);
CREATE UNIQUE INDEX chat_one_active_run ON commerce.chat_runs(thread_id)
    WHERE status IN ('queued','running');
CREATE INDEX chat_queue_idx ON commerce.chat_runs(created_at) WHERE status='queued';
CREATE TABLE commerce.chat_messages (
    id bigserial PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES commerce.chat_threads(id),
    run_id uuid NOT NULL REFERENCES commerce.chat_runs(id),
    role text NOT NULL CHECK(role IN ('user','assistant')),
    content text NOT NULL DEFAULT '',
    status text NOT NULL CHECK(status IN ('complete','partial','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id, role)
);
CREATE TABLE commerce.chat_events (
    run_id uuid NOT NULL REFERENCES commerce.chat_runs(id),
    seq integer NOT NULL CHECK(seq>0),
    kind text NOT NULL CHECK(kind IN ('status','token','tool','done','error')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id, seq)
);
