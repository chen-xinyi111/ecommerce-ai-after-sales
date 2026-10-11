CREATE TABLE commerce.applications (
    id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES commerce.users(id),
    order_id text NOT NULL REFERENCES commerce.orders(id),
    preview_id uuid NOT NULL UNIQUE REFERENCES commerce.previews(id),
    preview_version integer NOT NULL CHECK(preview_version>0),
    content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
    status text NOT NULL CHECK(status IN ('pending_approval','decision_recorded','approved','rejected','cancelled','superseded','conditions_changed')),
    workflow_status text NOT NULL CHECK(workflow_status IN ('queued','running','waiting','complete','failed')),
    resume_requested boolean NOT NULL DEFAULT false,
    decision text CHECK(decision IN ('approved','rejected')),
    decision_actor text REFERENCES commerce.users(id),
    decision_note text,
    decision_at timestamptz,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    next_event_id integer NOT NULL DEFAULT 0 CHECK(next_event_id>=0),
    error_code text,
    CHECK((decision IS NULL) = (decision_actor IS NULL)),
    CHECK(status<>'approved' OR decision='approved')
);
CREATE UNIQUE INDEX application_one_active_order ON commerce.applications(user_id,order_id)
    WHERE status IN ('pending_approval','decision_recorded','approved');
CREATE INDEX application_queue ON commerce.applications(confirmed_at) WHERE workflow_status='queued';
CREATE INDEX application_owner ON commerce.applications(user_id,confirmed_at DESC);
CREATE TABLE commerce.approval_commands (
    actor_id text NOT NULL REFERENCES commerce.users(id),
    request_id uuid NOT NULL,
    fingerprint text NOT NULL,
    application_id uuid NOT NULL REFERENCES commerce.applications(id),
    PRIMARY KEY(actor_id,request_id)
);
CREATE TABLE commerce.approval_events (
    application_id uuid NOT NULL REFERENCES commerce.applications(id),
    seq integer NOT NULL CHECK(seq>0),
    event_key text NOT NULL,
    kind text NOT NULL,
    actor_id text REFERENCES commerce.users(id),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(application_id,seq),
    UNIQUE(application_id,event_key)
);
CREATE FUNCTION commerce.keep_approval_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Approval audit is append-only';
END;
$$;
CREATE TRIGGER approval_audit_immutable BEFORE UPDATE OR DELETE ON commerce.approval_events
    FOR EACH ROW EXECUTE FUNCTION commerce.keep_approval_audit();
