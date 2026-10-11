CREATE OR REPLACE FUNCTION commerce.validate_order_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target text;
    ord commerce.orders%ROWTYPE;
    item_total bigint;
    payment_total bigint;
    payment_refunds bigint;
    history_refunds bigint;
BEGIN
    IF TG_TABLE_NAME<>'orders' THEN
        IF TG_OP='UPDATE' THEN
            IF OLD.order_id<>NEW.order_id THEN
                RAISE EXCEPTION 'Moving business records between orders is forbidden' USING ERRCODE='23514';
            END IF;
        END IF;
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
