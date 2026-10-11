CREATE TABLE commerce.preview_seed_marker (
    id integer PRIMARY KEY CHECK(id=1),
    version integer NOT NULL CHECK(version=1301)
);
CREATE TABLE commerce.previews (
    id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES commerce.users(id),
    order_id text NOT NULL REFERENCES commerce.orders(id),
    thread_id uuid REFERENCES commerce.chat_threads(id),
    run_id uuid REFERENCES commerce.chat_runs(id),
    request_id uuid NOT NULL,
    version integer NOT NULL CHECK(version>0),
    input jsonb NOT NULL,
    result jsonb NOT NULL,
    content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,request_id),
    CHECK((thread_id IS NULL) = (run_id IS NULL)),
    CHECK(result->>'execution_state'='not_executed'),
    CHECK(result->>'status' IN ('eligible','needs_info','ineligible'))
);
CREATE INDEX previews_owner_order_idx ON commerce.previews(user_id,order_id,created_at DESC);
CREATE UNIQUE INDEX preview_one_per_run ON commerce.previews(run_id) WHERE run_id IS NOT NULL;
ALTER TABLE commerce.chat_messages ADD COLUMN preview_id uuid REFERENCES commerce.previews(id);
ALTER TABLE commerce.chat_events DROP CONSTRAINT chat_events_kind_check;
ALTER TABLE commerce.chat_events ADD CONSTRAINT chat_events_kind_check
    CHECK(kind IN ('status','token','tool','preview','done','error'));
