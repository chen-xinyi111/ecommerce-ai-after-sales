"""At-least-once Kafka dispatch; PostgreSQL owns task state and business correctness."""

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError

from commerce.config import CommerceSettings
from commerce.db import connect
from commerce.execution_repository import row
from commerce.telemetry import span

logger = logging.getLogger(__name__)
TABLES = {
    "execution": ("execution_jobs", "status"),
    "chat": ("chat_runs", "status"),
    "approval": ("applications", "workflow_status"),
}
RELEASE = (
    "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID


@asynccontextmanager
async def redis_lock(client, task_type, task_id):
    token = uuid4().hex
    key = f"commerce:lease:{task_type}:{task_id}"
    acquired = False
    try:
        try:
            acquired = bool(await client.set(key, token, nx=True, ex=15))
            available = True
        except RedisError:
            available = False
        with span(
            "redis.lease",
            **{"redis.available": available, "task_id": str(task_id), "task_type": task_type},
        ):
            # Redis is an optimization. The session advisory lock remains authoritative.
            yield acquired or not available
    finally:
        if acquired:
            try:
                await client.eval(RELEASE, 1, key, token)
            except RedisError:
                pass


async def dead_letter(conn, key, *, event=None, code, digest=None):
    await conn.execute(
        "INSERT INTO commerce.dead_letters(source_key,event_id,task_type,task_id,error_code,payload_sha256) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(source_key) DO UPDATE SET status='open',resolved_at=NULL,delivered_at=NULL",
        (
            key,
            event["id"] if event else None,
            event["task_type"] if event else None,
            event["task_id"] if event else None,
            code,
            digest,
        ),
    )


async def publish_one(producer):
    config = CommerceSettings()
    async with connect() as conn:
        event = await row(
            conn,
            "SELECT * FROM commerce.task_outbox WHERE status='pending' AND available_at<=now() ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1",
        )
        if not event:
            return False
        try:
            with span(
                "outbox.publish",
                parent=event["traceparent"],
                task_id=str(event["task_id"]),
                task_type=event["task_type"],
                event_id=str(event["id"]),
                transport="kafka",
            ):
                await asyncio.wait_for(
                    producer.send_and_wait(
                        config.KAFKA_TOPIC,
                        json.dumps({"event_id": str(event["id"])}).encode(),
                        key=str(event["task_id"]).encode(),
                    ),
                    timeout=8,
                )
            await conn.execute(
                "UPDATE commerce.task_outbox SET status='published',published_at=now(),attempts=attempts+1,error_code=NULL WHERE id=%s",
                (event["id"],),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            attempts = event["attempts"] + 1
            await conn.execute(
                "UPDATE commerce.task_outbox SET attempts=%s,status=%s,error_code='broker_unavailable',available_at=now()+(%s * interval '1 second') WHERE id=%s",
                (
                    attempts,
                    "dead" if attempts >= 5 else "pending",
                    min(2**attempts, 30),
                    event["id"],
                ),
            )
            if attempts >= 5:
                await dead_letter(
                    conn, "outbox:" + str(event["id"]), event=event, code="publish_exhausted"
                )
        return True


async def handle(event_id, redis, *, processors=None):
    async with connect() as conn:
        event = await row(conn, "SELECT * FROM commerce.task_outbox WHERE id=%s", (event_id,))
    if not event:
        raise ValueError("unknown_event")
    task_type, task_id = event["task_type"], event["task_id"]
    async with connect() as lease:
        await lease.set_autocommit(True)
        locked = await row(
            lease,
            "SELECT pg_try_advisory_lock(hashtextextended(%s,3101)) AS acquired",
            (task_type + ":" + str(task_id),),
        )
        if not locked["acquired"]:
            return False
        try:
            async with redis_lock(redis, task_type, task_id) as allowed:
                if not allowed:
                    return False
                async with connect() as conn:
                    if await row(
                        conn, "SELECT 1 FROM commerce.task_inbox WHERE event_id=%s", (event_id,)
                    ):
                        return True
                    table, status_column = TABLES[task_type]
                    task = await row(
                        conn, f"SELECT * FROM commerce.{table} WHERE id=%s FOR UPDATE", (task_id,)
                    )
                    if (
                        not task
                        or task["dispatch_generation"] != event["generation"]
                        or task[status_column] not in ("queued", "running")
                    ):
                        await conn.execute(
                            "INSERT INTO commerce.task_inbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                            (event_id,),
                        )
                        return True
                    if (
                        task_type == "execution"
                        and task["next_attempt_at"]
                        > (await row(conn, "SELECT now() AS value"))["value"]
                    ):
                        return False
                    if task_type == "execution":
                        task = await row(
                            conn,
                            "UPDATE commerce.execution_jobs SET status='running',attempts=attempts+1,updated_at=now() WHERE id=%s RETURNING *",
                            (task_id,),
                        )
                        await conn.execute(
                            "UPDATE commerce.applications SET execution_state='running' WHERE id=%s",
                            (task["application_id"],),
                        )
                    elif task_type == "chat":
                        if task["status"] == "running":
                            from commerce.chat_repository import append_event

                            await conn.execute(
                                "UPDATE commerce.chat_messages SET status='failed' WHERE run_id=%s AND role='assistant'",
                                (task_id,),
                            )
                            await conn.execute(
                                "UPDATE commerce.chat_runs SET status='failed',error_code='service_restarted',finished_at=now() WHERE id=%s",
                                (task_id,),
                            )
                            await append_event(
                                conn,
                                task_id,
                                "error",
                                {
                                    "status": "failed",
                                    "error_code": "service_restarted",
                                    "message": "服务重启，已接收的内容保留，请重新发送。",
                                },
                            )
                            await conn.execute(
                                "INSERT INTO commerce.task_inbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                                (event_id,),
                            )
                            return True
                        task = await row(
                            conn,
                            "UPDATE commerce.chat_runs SET status='running' WHERE id=%s RETURNING *",
                            (task_id,),
                        )
                        task["order_id"] = (
                            await row(
                                conn,
                                "SELECT order_id FROM commerce.chat_threads WHERE id=%s",
                                (task["thread_id"],),
                            )
                        )["order_id"]
                        from commerce.chat_repository import append_event

                        await append_event(
                            conn,
                            task_id,
                            "status",
                            {"status": "running", "label": "正在核验订单并生成回复"},
                        )
                    else:
                        task = await row(
                            conn,
                            "UPDATE commerce.applications SET workflow_status='running' WHERE id=%s RETURNING *",
                            (task_id,),
                        )
                if processors is None:
                    from commerce import approval_workflow, chat_worker, execution_worker

                    processors = {
                        "execution": execution_worker.process,
                        "chat": chat_worker.process,
                        "approval": approval_workflow.process,
                    }
                with span(
                    "worker." + task_type,
                    parent=task["traceparent"] or event["traceparent"],
                    task_id=str(task_id),
                    task_type=task_type,
                    event_id=str(event_id),
                    generation=event["generation"],
                ):
                    try:
                        await asyncio.wait_for(
                            processors[task_type](task), 200 if task_type == "chat" else 50
                        )
                    except TimeoutError:
                        if task_type == "execution":
                            from commerce.execution_worker import settle

                            await settle(task_id, failure=True)
                        else:
                            raise
                async with connect() as conn:
                    current = await row(
                        conn, f"SELECT * FROM commerce.{table} WHERE id=%s", (task_id,)
                    )
                    if current[status_column] == "running":
                        return False
                    await conn.execute(
                        "INSERT INTO commerce.task_inbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        (event_id,),
                    )
                return True
        finally:
            await lease.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,3101))",
                (task_type + ":" + str(task_id),),
            )


async def consume_one(consumer, redis, producer):
    message = await consumer.getone()
    try:
        event_id = Envelope.model_validate_json(message.value).event_id
        completed = await handle(event_id, redis)
    except (ValueError, TypeError):
        digest = hashlib.sha256(message.value or b"").hexdigest()
        key = f"{message.topic}:{message.partition}:{message.offset}"
        async with connect() as conn:
            await dead_letter(conn, key, code="invalid_message", digest=digest)
        completed = True
    partition = TopicPartition(message.topic, message.partition)
    if completed:
        await consumer.commit({partition: message.offset + 1})
    else:
        consumer.seek(partition, message.offset)
        await asyncio.sleep(0.3)


async def recover():
    # Republish unacknowledged events at process startup; never reset a live worker's state.
    async with connect() as conn:
        await conn.execute(
            "UPDATE commerce.task_outbox o SET status='pending',available_at=now() WHERE status='published' AND NOT EXISTS (SELECT 1 FROM commerce.task_inbox i WHERE i.event_id=o.id)"
        )


async def publish_dead_one(producer):
    async with connect() as conn:
        letter = await row(
            conn,
            "SELECT * FROM commerce.dead_letters WHERE delivered_at IS NULL AND status='open' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
        )
        if not letter:
            return False
        payload = {
            "dead_letter_id": str(letter["id"]),
            "error_code": letter["error_code"],
            "task_id": str(letter["task_id"]) if letter["task_id"] else None,
        }
        try:
            await asyncio.wait_for(
                producer.send_and_wait(
                    CommerceSettings().KAFKA_TOPIC + ".dead", json.dumps(payload).encode()
                ),
                timeout=8,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        await conn.execute(
            "UPDATE commerce.dead_letters SET delivered_at=now() WHERE id=%s", (letter["id"],)
        )
        return True


async def publisher(producer):
    while True:
        published = await publish_one(producer)
        await publish_dead_one(producer)
        if not published:
            await asyncio.sleep(0.3)


async def consumer_loop(producer, redis, index):
    config = CommerceSettings()
    consumer = AIOKafkaConsumer(
        config.KAFKA_TOPIC,
        bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS,
        group_id=config.KAFKA_GROUP,
        client_id=f"commerce-worker-{index}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_interval_ms=300000,
    )
    try:
        await consumer.start()
        while True:
            await consume_one(consumer, redis, producer)
    finally:
        await consumer.stop()


async def runtime():
    config = CommerceSettings()
    while True:
        producer = AIOKafkaProducer(
            bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS,
            enable_idempotence=True,
            request_timeout_ms=5000,
        )
        redis = Redis.from_url(config.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        tasks = []
        try:
            await producer.start()
            await recover()
            tasks = [asyncio.create_task(publisher(producer))] + [
                asyncio.create_task(consumer_loop(producer, redis, i)) for i in range(4)
            ]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Async transport unavailable (%s); durable tasks retained", type(exc).__name__
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await producer.stop()
            await redis.aclose()
        await asyncio.sleep(2)
