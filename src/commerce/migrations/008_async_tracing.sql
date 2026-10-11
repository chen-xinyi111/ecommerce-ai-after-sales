ALTER TABLE commerce.execution_jobs ADD COLUMN dispatch_generation integer NOT NULL DEFAULT 0;
ALTER TABLE commerce.execution_jobs ADD COLUMN traceparent text NOT NULL DEFAULT '';
ALTER TABLE commerce.chat_runs ADD COLUMN dispatch_generation integer NOT NULL DEFAULT 0;
ALTER TABLE commerce.chat_runs ADD COLUMN traceparent text NOT NULL DEFAULT '';
ALTER TABLE commerce.applications ADD COLUMN dispatch_generation integer NOT NULL DEFAULT 0;
ALTER TABLE commerce.applications ADD COLUMN traceparent text NOT NULL DEFAULT '';
CREATE TABLE commerce.task_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type text NOT NULL CHECK(task_type IN ('execution','chat','approval')),
    task_id uuid NOT NULL,
    generation integer NOT NULL,
    traceparent text NOT NULL,
    available_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','published','dead')),
    attempts integer NOT NULL DEFAULT 0,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    UNIQUE(task_type,task_id,generation)
);
CREATE INDEX task_outbox_pending ON commerce.task_outbox(available_at) WHERE status='pending';
CREATE TABLE commerce.task_inbox (
    event_id uuid PRIMARY KEY REFERENCES commerce.task_outbox(id),
    processed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE commerce.dead_letters (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key text NOT NULL UNIQUE,
    event_id uuid REFERENCES commerce.task_outbox(id),
    task_type text,
    task_id uuid,
    error_code text NOT NULL,
    payload_sha256 text,
    status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE TABLE commerce.trace_spans (
    trace_id text NOT NULL,
    span_id text NOT NULL,
    parent_span_id text,
    name text NOT NULL,
    started_ns bigint NOT NULL,
    ended_ns bigint NOT NULL,
    status text NOT NULL,
    attributes jsonb NOT NULL,
    PRIMARY KEY(trace_id,span_id)
);
CREATE INDEX trace_spans_task ON commerce.trace_spans((attributes->>'task_id'));
CREATE FUNCTION commerce.queue_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text := TG_ARGV[0]; queued boolean; changed boolean;
BEGIN
    IF kind='approval' THEN
        queued := NEW.workflow_status='queued';
        changed := TG_OP='INSERT' OR OLD.workflow_status IS DISTINCT FROM NEW.workflow_status;
    ELSE
        queued := NEW.status='queued';
        changed := TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status;
    END IF;
    IF queued AND changed THEN
        NEW.dispatch_generation := NEW.dispatch_generation+1;
        INSERT INTO commerce.task_outbox(task_type,task_id,generation,traceparent,available_at)
        VALUES(kind,NEW.id,NEW.dispatch_generation,NEW.traceparent,
            CASE WHEN kind='execution' THEN (to_jsonb(NEW)->>'next_attempt_at')::timestamptz ELSE now() END);
    END IF;
    IF kind='execution' AND NEW.status='failed' AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
        INSERT INTO commerce.dead_letters(source_key,task_type,task_id,error_code)
        VALUES('execution:'||NEW.id||':'||NEW.dispatch_generation,kind,NEW.id,coalesce(NEW.error_code,'execution_unavailable'))
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER execution_outbox BEFORE INSERT OR UPDATE ON commerce.execution_jobs FOR EACH ROW EXECUTE FUNCTION commerce.queue_outbox('execution');
CREATE TRIGGER chat_outbox BEFORE INSERT OR UPDATE ON commerce.chat_runs FOR EACH ROW EXECUTE FUNCTION commerce.queue_outbox('chat');
CREATE TRIGGER approval_outbox BEFORE INSERT OR UPDATE ON commerce.applications FOR EACH ROW EXECUTE FUNCTION commerce.queue_outbox('approval');
INSERT INTO commerce.task_outbox(task_type,task_id,generation,traceparent,available_at)
SELECT 'execution',id,0,traceparent,next_attempt_at FROM commerce.execution_jobs WHERE status IN ('queued','running')
UNION ALL SELECT 'chat',id,0,traceparent,now() FROM commerce.chat_runs WHERE status IN ('queued','running')
UNION ALL SELECT 'approval',id,0,traceparent,now() FROM commerce.applications WHERE workflow_status IN ('queued','running');
