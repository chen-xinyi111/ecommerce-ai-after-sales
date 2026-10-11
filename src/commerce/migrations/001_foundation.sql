CREATE SCHEMA IF NOT EXISTS commerce;
CREATE TABLE commerce.demo_marker (
    id integer PRIMARY KEY CHECK(id=1),
    marker text NOT NULL CHECK(marker='agent-commerce-demo-v1'),
    business_date date NOT NULL,
    seed_version integer NOT NULL
);
CREATE TABLE commerce.users (
    id text PRIMARY KEY,
    username text NOT NULL UNIQUE,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL CHECK(role IN ('user','staff')),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE commerce.sessions (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES commerce.users(id),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_expiry_idx ON commerce.sessions(expires_at);
CREATE TABLE commerce.products (
    id text PRIMARY KEY,
    name text NOT NULL,
    simulated boolean NOT NULL DEFAULT true CHECK(simulated)
);
CREATE TABLE commerce.skus (
    id text PRIMARY KEY,
    product_id text NOT NULL REFERENCES commerce.products(id),
    price_cents integer NOT NULL CHECK(price_cents>0)
);
CREATE TABLE commerce.inventory (
    sku_id text PRIMARY KEY REFERENCES commerce.skus(id),
    on_hand integer NOT NULL CHECK(on_hand>=0),
    reserved integer NOT NULL DEFAULT 0 CHECK(reserved>=0 AND reserved<=on_hand)
);
CREATE TABLE commerce.orders (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES commerce.users(id),
    status text NOT NULL CHECK(status IN ('paid','shipped','delivered','cancelled','partially_refunded')),
    paid_cents integer NOT NULL CHECK(paid_cents>=0),
    refunded_cents integer NOT NULL DEFAULT 0 CHECK(refunded_cents>=0 AND refunded_cents<=paid_cents),
    purchased_on date NOT NULL,
    scenario text NOT NULL,
    simulated boolean NOT NULL DEFAULT true CHECK(simulated)
);
CREATE INDEX orders_user_idx ON commerce.orders(user_id,id);
CREATE TABLE commerce.order_items (
    id text PRIMARY KEY,
    order_id text NOT NULL REFERENCES commerce.orders(id),
    sku_id text NOT NULL REFERENCES commerce.skus(id),
    product_name text NOT NULL,
    quantity integer NOT NULL CHECK(quantity>0),
    unit_price_cents integer NOT NULL CHECK(unit_price_cents>0),
    after_sales_quantity integer NOT NULL DEFAULT 0 CHECK(after_sales_quantity>=0 AND after_sales_quantity<=quantity),
    UNIQUE(id,order_id)
);
CREATE INDEX order_items_order_idx ON commerce.order_items(order_id);
CREATE TABLE commerce.payments (
    id text PRIMARY KEY,
    order_id text NOT NULL UNIQUE REFERENCES commerce.orders(id),
    paid_cents integer NOT NULL CHECK(paid_cents>=0),
    refunded_cents integer NOT NULL DEFAULT 0 CHECK(refunded_cents>=0 AND refunded_cents<=paid_cents),
    status text NOT NULL CHECK(status IN ('paid','cancelled','partially_refunded'))
);
CREATE TABLE commerce.shipments (
    id text PRIMARY KEY,
    order_id text NOT NULL UNIQUE REFERENCES commerce.orders(id),
    tracking_number text NOT NULL UNIQUE,
    status text NOT NULL CHECK(status IN ('shipped','delivered','lost'))
);
CREATE TABLE commerce.logistics_events (
    id text PRIMARY KEY,
    shipment_id text NOT NULL REFERENCES commerce.shipments(id),
    occurred_at timestamptz NOT NULL,
    status text NOT NULL,
    description text NOT NULL
);
CREATE INDEX logistics_shipment_idx ON commerce.logistics_events(shipment_id,occurred_at);
CREATE TABLE commerce.policies (
    id text NOT NULL,
    version integer NOT NULL CHECK(version>0),
    kind text NOT NULL CHECK(kind IN ('refund','exchange','reship','repair')),
    valid_from date NOT NULL,
    valid_until date NOT NULL CHECK(valid_until>=valid_from),
    conditions jsonb NOT NULL,
    explanation text NOT NULL,
    simulated boolean NOT NULL DEFAULT true CHECK(simulated),
    PRIMARY KEY(id,version)
);
CREATE TABLE commerce.after_sales_history (
    id text PRIMARY KEY,
    order_id text NOT NULL REFERENCES commerce.orders(id),
    order_item_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('refund','exchange','reship','repair')),
    quantity integer NOT NULL CHECK(quantity>0),
    refund_cents integer NOT NULL DEFAULT 0 CHECK(refund_cents>=0),
    status text NOT NULL CHECK(status='completed'),
    policy_id text NOT NULL,
    policy_version integer NOT NULL,
    FOREIGN KEY(order_item_id,order_id) REFERENCES commerce.order_items(id,order_id),
    FOREIGN KEY(policy_id,policy_version) REFERENCES commerce.policies(id,version),
    CHECK(kind='refund' OR refund_cents=0)
);
CREATE INDEX history_order_idx ON commerce.after_sales_history(order_id);

CREATE FUNCTION commerce.validate_order_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target text;
    ord commerce.orders%ROWTYPE;
    item_total bigint;
    payment_total bigint;
    payment_refunds bigint;
    history_refunds bigint;
BEGIN
    IF TG_OP='UPDATE' AND TG_TABLE_NAME<>'orders' AND OLD.order_id<>NEW.order_id THEN
        RAISE EXCEPTION 'Moving business records between orders is forbidden' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='orders' THEN
        target := COALESCE(NEW.id,OLD.id);
    ELSE
        target := COALESCE(NEW.order_id,OLD.order_id);
    END IF;
    SELECT * INTO ord FROM commerce.orders WHERE id=target FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT COALESCE(sum(quantity::bigint*unit_price_cents),0) INTO item_total
        FROM commerce.order_items WHERE order_id=target;
    SELECT COALESCE(sum(paid_cents),0), COALESCE(sum(refunded_cents),0)
        INTO payment_total,payment_refunds FROM commerce.payments WHERE order_id=target;
    SELECT COALESCE(sum(refund_cents),0) INTO history_refunds
        FROM commerce.after_sales_history WHERE order_id=target;
    IF (ord.status='cancelled' AND ord.paid_cents<>0)
        OR (ord.status<>'cancelled' AND item_total<>ord.paid_cents)
        OR ord.paid_cents<>payment_total
        OR ord.refunded_cents<>payment_refunds
        OR ord.refunded_cents<>history_refunds
        OR EXISTS (
            SELECT 1 FROM commerce.order_items i
            LEFT JOIN (
                SELECT order_item_id,sum(quantity) AS n,sum(refund_cents) AS cents
                FROM commerce.after_sales_history WHERE order_id=target GROUP BY order_item_id
            ) h ON h.order_item_id=i.id
            WHERE i.order_id=target AND
            (i.after_sales_quantity<>COALESCE(h.n,0)
             OR COALESCE(h.cents,0)>i.quantity::bigint*i.unit_price_cents)
        )
    THEN
        RAISE EXCEPTION 'Inconsistent order amounts or after-sales quantities: %',target
            USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER orders_consistency AFTER INSERT OR UPDATE OR DELETE ON commerce.orders
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION commerce.validate_order_consistency();
CREATE CONSTRAINT TRIGGER items_consistency AFTER INSERT OR UPDATE OR DELETE ON commerce.order_items
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION commerce.validate_order_consistency();
CREATE CONSTRAINT TRIGGER payments_consistency AFTER INSERT OR UPDATE OR DELETE ON commerce.payments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION commerce.validate_order_consistency();
CREATE CONSTRAINT TRIGGER history_consistency AFTER INSERT OR UPDATE OR DELETE ON commerce.after_sales_history
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION commerce.validate_order_consistency();
