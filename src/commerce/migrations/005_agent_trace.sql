ALTER TABLE commerce.chat_events DROP CONSTRAINT chat_events_kind_check;
ALTER TABLE commerce.chat_events ADD CONSTRAINT chat_events_kind_check
    CHECK(kind IN ('status','token','tool','preview','route','skill','agent','handoff','done','error'));
