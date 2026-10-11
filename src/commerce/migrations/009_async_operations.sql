CREATE TABLE commerce.async_operations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    letter_id uuid NOT NULL REFERENCES commerce.dead_letters(id),
    actor_id text NOT NULL REFERENCES commerce.users(id),
    note text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE commerce.dead_letters ADD COLUMN delivered_at timestamptz;
