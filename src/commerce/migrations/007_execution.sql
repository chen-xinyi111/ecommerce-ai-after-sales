ALTER TABLE commerce.applications ADD COLUMN execution_state text NOT NULL DEFAULT 'not_started'
    CHECK(execution_state IN ('not_started','queued','running','completed','failed','stopped','manual_review'));
DROP INDEX commerce.application_one_active_order;
CREATE UNIQUE INDEX application_one_active_order ON commerce.applications(user_id,order_id)
    WHERE status IN ('pending_approval','decision_recorded','approved') AND execution_state NOT IN ('completed','manual_review');
CREATE TABLE commerce.execution_jobs (
    id uuid PRIMARY KEY,
    application_id uuid NOT NULL UNIQUE REFERENCES commerce.applications(id),
    requested_by text NOT NULL REFERENCES commerce.users(id),
    status text NOT NULL CHECK(status IN ('queued','running','completed','failed','stopped','manual_review')),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    error_code text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX execution_queue ON commerce.execution_jobs(next_attempt_at) WHERE status='queued';
CREATE TABLE commerce.work_orders (
    id text PRIMARY KEY,
    application_id uuid NOT NULL UNIQUE REFERENCES commerce.applications(id),
    job_id uuid NOT NULL UNIQUE REFERENCES commerce.execution_jobs(id),
    order_id text NOT NULL REFERENCES commerce.orders(id),
    order_item_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('refund','exchange','reship','repair')),
    quantity integer NOT NULL CHECK(quantity>0),
    refund_cents integer NOT NULL CHECK(refund_cents>=0),
    policy_id text NOT NULL,
    policy_version integer NOT NULL,
    status text NOT NULL CHECK(status IN ('ready','reserved','completed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(order_item_id,order_id) REFERENCES commerce.order_items(id,order_id),
    FOREIGN KEY(policy_id,policy_version) REFERENCES commerce.policies(id,version),
    CHECK(kind='refund' OR refund_cents=0)
);
CREATE TABLE commerce.inventory_reservations (
    application_id uuid PRIMARY KEY REFERENCES commerce.applications(id),
    work_order_id text NOT NULL UNIQUE REFERENCES commerce.work_orders(id),
    sku_id text NOT NULL REFERENCES commerce.inventory(sku_id),
    quantity integer NOT NULL CHECK(quantity>0),
    status text NOT NULL CHECK(status IN ('held','consumed','released')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE commerce.action_results (
    application_id uuid PRIMARY KEY REFERENCES commerce.applications(id),
    idempotency_key text NOT NULL UNIQUE,
    work_order_id text NOT NULL UNIQUE REFERENCES commerce.work_orders(id),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE commerce.refund_ledger (
    application_id uuid PRIMARY KEY REFERENCES commerce.applications(id),
    idempotency_key text NOT NULL UNIQUE,
    payment_id text NOT NULL REFERENCES commerce.payments(id),
    amount_cents integer NOT NULL CHECK(amount_cents>0),
    created_at timestamptz NOT NULL DEFAULT now(),
    simulated boolean NOT NULL DEFAULT true CHECK(simulated)
);
CREATE TABLE commerce.after_sales_shipments (
    application_id uuid PRIMARY KEY REFERENCES commerce.applications(id),
    idempotency_key text NOT NULL UNIQUE,
    work_order_id text NOT NULL UNIQUE REFERENCES commerce.work_orders(id),
    sku_id text NOT NULL REFERENCES commerce.skus(id),
    quantity integer NOT NULL CHECK(quantity>0),
    tracking_number text NOT NULL UNIQUE,
    status text NOT NULL CHECK(status='dispatched'),
    created_at timestamptz NOT NULL DEFAULT now(),
    simulated boolean NOT NULL DEFAULT true CHECK(simulated)
);
CREATE TABLE commerce.repair_intakes (
    application_id uuid PRIMARY KEY REFERENCES commerce.applications(id),
    work_order_id text NOT NULL UNIQUE REFERENCES commerce.work_orders(id),
    status text NOT NULL CHECK(status='awaiting_inspection'),
    created_at timestamptz NOT NULL DEFAULT now(),
    simulated boolean NOT NULL DEFAULT true CHECK(simulated)
);
CREATE TRIGGER action_results_immutable BEFORE UPDATE OR DELETE ON commerce.action_results
    FOR EACH ROW EXECUTE FUNCTION commerce.keep_approval_audit();
CREATE TRIGGER refund_ledger_immutable BEFORE UPDATE OR DELETE ON commerce.refund_ledger
    FOR EACH ROW EXECUTE FUNCTION commerce.keep_approval_audit();
CREATE TRIGGER dispatch_immutable BEFORE UPDATE OR DELETE ON commerce.after_sales_shipments
    FOR EACH ROW EXECUTE FUNCTION commerce.keep_approval_audit();
