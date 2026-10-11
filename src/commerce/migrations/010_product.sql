ALTER TABLE commerce.users ADD COLUMN email text;
ALTER TABLE commerce.users ADD COLUMN email_verified boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX users_email_unique ON commerce.users(lower(email)) WHERE email IS NOT NULL;
CREATE TABLE commerce.account_tokens (
 token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES commerce.users(id),
 purpose text NOT NULL CHECK(purpose IN ('verify','reset')), expires_at timestamptz NOT NULL,
 used_at timestamptz
);
CREATE TABLE commerce.auth_limits (bucket text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), attempts integer NOT NULL);
CREATE TABLE commerce.support_sessions (
 thread_id uuid PRIMARY KEY REFERENCES commerce.chat_threads(id),
 state text NOT NULL CHECK(state IN ('queued','active','closed')),
 staff_id text REFERENCES commerce.users(id), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE commerce.support_messages (
 id bigserial PRIMARY KEY, thread_id uuid NOT NULL REFERENCES commerce.chat_threads(id),
 actor_id text NOT NULL REFERENCES commerce.users(id), role text NOT NULL CHECK(role IN ('user','staff')),
 content text NOT NULL CHECK(length(content) BETWEEN 1 AND 2000), request_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(thread_id,actor_id,request_id)
);
CREATE TABLE commerce.materials (
 id uuid PRIMARY KEY, preview_id uuid NOT NULL REFERENCES commerce.previews(id),
 application_id uuid REFERENCES commerce.applications(id), actor_id text NOT NULL REFERENCES commerce.users(id),
 note text NOT NULL DEFAULT '', image bytea, mime text, name text,
 created_at timestamptz NOT NULL DEFAULT now(), CHECK(length(note)<=2000),
 CHECK(image IS NULL OR octet_length(image)<=2097152)
);
CREATE TABLE commerce.material_requests (
 id uuid PRIMARY KEY, application_id uuid NOT NULL REFERENCES commerce.applications(id),
 staff_id text NOT NULL REFERENCES commerce.users(id), note text NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','answered')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE commerce.notifications (
 id bigserial PRIMARY KEY, user_id text NOT NULL REFERENCES commerce.users(id),
 application_id uuid NOT NULL REFERENCES commerce.applications(id),
 event_key text NOT NULL, title text NOT NULL, read_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(user_id,application_id,event_key)
);
CREATE TABLE commerce.provider_events (
 event_id uuid PRIMARY KEY, application_id uuid NOT NULL REFERENCES commerce.applications(id),
 status text NOT NULL CHECK(status IN ('dispatched','delivered')), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION commerce.notify_application() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE title text;
BEGIN
 IF TG_OP='UPDATE' AND NEW.status=OLD.status AND NEW.execution_state=OLD.execution_state THEN RETURN NEW; END IF;
 title := CASE
 WHEN NEW.execution_state='completed' THEN '售后办理结果已更新'
 WHEN NEW.execution_state IN ('failed','manual_review','stopped') THEN '售后办理需要进一步处理'
 WHEN NEW.status='rejected' THEN '售后申请未通过审批'
 WHEN NEW.status IN ('decision_recorded','approved') THEN '售后审批结果已更新'
 WHEN NEW.status IN ('conditions_changed','superseded') THEN '售后方案需要重新确认'
 WHEN NEW.status='cancelled' THEN '售后申请已取消'
 ELSE '售后申请已提交' END;
 IF NEW.execution_state IN ('queued','running') THEN RETURN NEW; END IF;
 INSERT INTO commerce.notifications(user_id,application_id,event_key,title)
 VALUES(NEW.user_id,NEW.id,'status:'||NEW.status||':'||NEW.execution_state,title)
 ON CONFLICT DO NOTHING;
 RETURN NEW;
END $$;
CREATE TRIGGER product_status_notifications AFTER INSERT OR UPDATE OF status,execution_state
 ON commerce.applications FOR EACH ROW EXECUTE FUNCTION commerce.notify_application();
