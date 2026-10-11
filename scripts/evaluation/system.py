"""Real HTTP admission, Kafka replay and scoped failure injection."""

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition

from commerce.db import connect_sync
from evaluation.cases import KINDS, LABELS
from evaluation.run import append, business_snapshot, chat_turn, login, ms, write


def binding(value):
    return dict(
        preview_id=value.get("preview_id", value["id"]),
        preview_version=value.get("preview_version", value.get("version")),
        content_sha256=value["content_sha256"],
        request_id=str(uuid4()),
    )


async def wait(client, path, predicate, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(path)
        response.raise_for_status()
        data = response.json()
        if predicate(data):
            return data
        await asyncio.sleep(0.2)
    raise TimeoutError(path)


def fixture(index, kind, sku=None):
    oid = f"ORD-{940000 + index}"
    sku = sku or f"SKU-{1 + index % 59:03}"
    with connect_sync() as conn:
        conn.execute(
            "INSERT INTO commerce.orders(id,user_id,status,paid_cents,purchased_on,scenario) VALUES (%s,'USR-001','delivered',40000,%s,'evaluation-system-3.2')",
            (oid, datetime(2026, 10, 9).date()),
        )
        conn.execute(
            "INSERT INTO commerce.order_items(id,order_id,sku_id,product_name,quantity,unit_price_cents) VALUES (%s,%s,%s,'系统评测商品',4,10000)",
            (oid + "-I1", oid, sku),
        )
        conn.execute(
            "INSERT INTO commerce.payments(id,order_id,paid_cents,status) VALUES (%s,%s,40000,'paid')",
            ("PAY-" + oid, oid),
        )
        conn.execute(
            "INSERT INTO commerce.shipments(id,order_id,tracking_number,status) VALUES (%s,%s,%s,%s)",
            ("SHP-" + oid, oid, "SIM-E32-" + oid, "lost" if kind == "reship" else "delivered"),
        )
    return dict(order_id=oid, kind=kind)


async def prepare(user, staff, case, *, approve=True):
    oid, kind = case["order_id"], case["kind"]
    response = await user.post(
        f"/api/v1/orders/{oid}/previews",
        json=dict(
            kind=kind,
            order_item_id=oid + "-I1",
            quantity=1,
            reason="not_received" if kind == "reship" else "quality",
            received=kind != "reship",
            request_id=str(uuid4()),
        ),
    )
    response.raise_for_status()
    preview = response.json()
    assert preview["status"] == "eligible", preview
    response = await user.post(f"/api/v1/previews/{preview['id']}/confirm", json=binding(preview))
    response.raise_for_status()
    app = response.json()
    path = "/api/v1/applications/" + app["id"]
    data = await wait(user, path, lambda d: d["application"]["workflow_status"] == "waiting")
    if approve:
        response = await staff.post(
            path + "/decision",
            json=binding(app)
            | dict(decision="approved", note="3.2 独立评测订单，人工审批接口演练"),
        )
        response.raise_for_status()
        response = await staff.post(path + "/resume", json=binding(app))
        response.raise_for_status()
        data = await wait(staff, path, lambda d: d["application"]["workflow_status"] == "complete")
        assert data["application"]["status"] == "approved", data
    return case | dict(application=data["application"], preview=preview)


async def execute(staff, case):
    started = time.monotonic()
    app = case["application"]
    response = await staff.post(f"/api/v1/applications/{app['id']}/execute", json=binding(app))
    accepted = ms(started)
    response.raise_for_status()
    job = response.json()
    result = await wait(
        staff,
        f"/api/v1/applications/{app['id']}/execution",
        lambda d: d["job"]["status"] in ("completed", "failed", "stopped", "manual_review"),
    )
    return case | dict(
        job_id=job["id"], admission_ms=accepted, full_task_ms=ms(started), result=result
    )


async def kafka_replay(host, cases, output):
    ids = [c["application"]["id"] for c in cases]
    before = business_snapshot(ids)
    with connect_sync() as conn:
        events = [
            conn.execute(
                "SELECT id,task_id FROM commerce.task_outbox WHERE task_type='execution' AND task_id=%s ORDER BY generation DESC LIMIT 1",
                (c["job_id"],),
            ).fetchone()
            for c in cases
        ]
    producer = AIOKafkaProducer(
        bootstrap_servers=host.env.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:59092"),
        enable_idempotence=True,
    )
    await producer.start()
    targets = {}
    started = time.monotonic()
    try:
        for index in range(2000):
            event = events[index % len(events)]
            metadata = await producer.send_and_wait(
                host.env["KAFKA_TOPIC"],
                json.dumps({"event_id": str(event["id"])}).encode(),
                key=str(event["task_id"]).encode(),
            )
            targets[metadata.partition] = max(
                targets.get(metadata.partition, 0), metadata.offset + 1
            )
            append(
                output / "replay.jsonl",
                dict(
                    sequence=index + 1,
                    event_id=str(event["id"]),
                    task_id=str(event["task_id"]),
                    partition=metadata.partition,
                    offset=metadata.offset,
                    acknowledged_at=datetime.now(UTC).isoformat(),
                ),
            )
        observer = AIOKafkaConsumer(
            bootstrap_servers=host.env.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:59092"),
            group_id=host.env["KAFKA_GROUP"],
        )
        await observer.start()
        try:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                committed = {
                    p: await observer.committed(TopicPartition(host.env["KAFKA_TOPIC"], p))
                    for p in targets
                }
                if all((committed[p] or 0) >= target for p, target in targets.items()):
                    break
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError("Replay consumer offset acknowledgements")
        finally:
            await observer.stop()
    finally:
        await producer.stop()
    after = business_snapshot(ids)
    assert before == after, "Replay changed a committed business record"
    result = dict(
        messages=2000,
        distinct_tasks=len(cases),
        before=before,
        after=after,
        duplicate_side_effects=0,
        consumer_targets=targets,
        consumer_committed=committed,
        all_offsets_acknowledged=True,
        elapsed_ms=ms(started),
    )
    write(output / "replay-summary.json", result)
    print("2000 actual Kafka replays fully acknowledged; business snapshots unchanged", flush=True)


async def faults(host, user, staff, output):
    evidence = []
    base = dict(
        tool_failure_orders=[c["order_id"] for c in host.cases if c["category"] == "tool_failure"]
    )
    # Persisted user confirmation and staff decision survive real evaluation worker kills.
    pending = await prepare(user, staff, fixture(100, "refund", "SKU-051"), approve=False)
    host.stop()
    host.start()
    detail = (await staff.get("/api/v1/applications/" + pending["application"]["id"])).json()
    assert (
        detail["application"]["status"] == "pending_approval"
        and detail["application"]["workflow_status"] == "waiting"
    )
    evidence.append(
        dict(
            scenario="restart_before_staff_approval", passed=True, application=detail["application"]
        )
    )
    app = pending["application"]
    path = "/api/v1/applications/" + app["id"]
    (
        await staff.post(
            path + "/decision",
            json=binding(app) | dict(decision="approved", note="3.2 审批后重启演练"),
        )
    ).raise_for_status()
    host.stop()
    host.start()
    detail = (await staff.get(path)).json()
    assert detail["application"]["decision"] == "approved"
    (await staff.post(path + "/resume", json=binding(app))).raise_for_status()
    detail = await wait(staff, path, lambda d: d["application"]["workflow_status"] == "complete")
    assert detail["application"]["status"] == "approved"
    evidence.append(
        dict(
            scenario="restart_after_staff_decision", passed=True, application=detail["application"]
        )
    )
    # Redis connection failure: PG remains authoritative.
    prepared = await prepare(user, staff, fixture(101, "exchange", "SKU-052"))
    host.set_fault(base | dict(mode="redis"))
    redis_case = await execute(staff, prepared)
    assert redis_case["result"]["job"]["status"] == "completed"
    host.set_fault(base)
    evidence.append(dict(scenario="redis_connection_exception", passed=True, case=redis_case))
    # Before irreversible action: three failures release any held reservation; explicit retry.
    prepared = await prepare(user, staff, fixture(102, "reship", "SKU-053"))
    host.set_fault(base | dict(mode="write_before", order_id=prepared["order_id"]))
    before_case = await execute(staff, prepared)
    assert before_case["result"]["job"]["status"] == "failed"
    assert before_case["result"]["shipment"] is None
    with connect_sync() as conn:
        reservation = conn.execute(
            "SELECT * FROM commerce.inventory_reservations WHERE application_id=%s",
            (prepared["application"]["id"],),
        ).fetchone()
    assert reservation["status"] == "released"
    host.set_fault(base)
    path = "/api/v1/applications/" + prepared["application"]["id"]
    (
        await staff.post(
            path + "/retry-execution",
            json=binding(prepared["application"])
            | {"note": "3.2 注入异常已解除，核对持久结果后重试"},
        )
    ).raise_for_status()
    recovered = await wait(staff, path + "/execution", lambda d: d["job"]["status"] == "completed")
    evidence.append(
        dict(
            scenario="before_write_failure_and_explicit_retry",
            passed=True,
            failed=before_case,
            reservation_after_failure=reservation,
            recovered=recovered,
        )
    )
    # Committed refund with response lost: settles from ledger, never repeats.
    prepared = await prepare(user, staff, fixture(103, "refund", "SKU-054"))
    host.set_fault(base | dict(mode="write_after", order_id=prepared["order_id"]))
    after_case = await execute(staff, prepared)
    assert after_case["result"]["job"]["status"] == "completed"
    snapshot = business_snapshot([prepared["application"]["id"]])
    assert snapshot["refund_ledger"]["count"] == 1
    host.set_fault(base)
    evidence.append(
        dict(
            scenario="lost_response_after_committed_refund",
            passed=True,
            case=after_case,
            snapshot=snapshot,
        )
    )
    # Actual process kill after work-order / reservation commit, before shipment.
    prepared = await prepare(user, staff, fixture(104, "exchange", "SKU-055"))
    host.set_fault(base | dict(mode="hold_after_work_order", order_id=prepared["order_id"]))
    app = prepared["application"]
    response = await staff.post(f"/api/v1/applications/{app['id']}/execute", json=binding(app))
    response.raise_for_status()
    await wait(
        staff,
        f"/api/v1/applications/{app['id']}/execution",
        lambda d: bool(d.get("reservation") and d["reservation"]["status"] == "held"),
    )
    started = time.monotonic()
    old_pid = host.process.pid
    host.stop()
    host.set_fault(base)
    host.start()
    recovered = await wait(
        staff,
        f"/api/v1/applications/{app['id']}/execution",
        lambda d: d["job"]["status"] == "completed",
    )
    assert recovered["reservation"]["status"] == "consumed"
    evidence.append(
        dict(
            scenario="actual_worker_kill_after_reservation",
            passed=True,
            killed_pid=old_pid,
            recovered_pid=host.process.pid,
            recovery_ms=ms(started),
            result=recovered,
        )
    )
    # Deterministic broker exception at actual producer boundary. Accelerate only backoff due times;
    # do not present its duration as physical broker outage recovery latency.
    prepared = await prepare(user, staff, fixture(105, "repair", "SKU-056"))
    host.set_fault(base | dict(mode="broker"))
    response = await staff.post(
        f"/api/v1/applications/{prepared['application']['id']}/execute",
        json=binding(prepared["application"]),
    )
    response.raise_for_status()
    job = response.json()
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        with connect_sync() as conn:
            event = conn.execute(
                "SELECT * FROM commerce.task_outbox WHERE task_type='execution' AND task_id=%s ORDER BY generation DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
            if event["status"] != "dead":
                conn.execute(
                    "UPDATE commerce.task_outbox SET available_at=now() WHERE id=%s AND status='pending'",
                    (event["id"],),
                )
        if event["status"] == "dead":
            break
        await asyncio.sleep(0.3)
    assert event["status"] == "dead" and event["attempts"] == 5
    host.set_fault(base)
    operations = (await staff.get("/api/v1/operations")).json()
    letter = next(d for d in operations["dead_letters"] if d["event_id"] == str(event["id"]))
    response = await staff.post(
        f"/api/v1/operations/dead-letters/{letter['id']}/redrive",
        json={"note": "3.2 已解除注入的 broker 异常，核对后重新投递"},
    )
    response.raise_for_status()
    result = await wait(
        staff,
        f"/api/v1/applications/{prepared['application']['id']}/execution",
        lambda d: d["job"]["status"] == "completed",
    )
    evidence.append(
        dict(
            scenario="producer_failure_exhaustion_and_audited_redrive",
            passed=True,
            accelerated_backoff=True,
            failed_event=event,
            dead_letter=letter,
            recovered=result,
        )
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=host.env.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:59092")
    )
    await producer.start()
    try:
        metadata = await producer.send_and_wait(
            host.env["KAFKA_TOPIC"], b'{"event_id":"forged","kind":"refund"}'
        )
    finally:
        await producer.stop()
    ops = await wait(
        staff,
        "/api/v1/operations",
        lambda d: any(letter["error_code"] == "invalid_message" for letter in d["dead_letters"]),
    )
    invalid = next(
        letter for letter in ops["dead_letters"] if letter["error_code"] == "invalid_message"
    )
    denied = await staff.post(
        f"/api/v1/operations/dead-letters/{invalid['id']}/redrive",
        json={"note": "无效消息不能成为申请"},
    )
    assert denied.status_code == 409
    evidence.append(
        dict(
            scenario="invalid_kafka_message_cannot_redrive",
            passed=True,
            partition=metadata.partition,
            offset=metadata.offset,
            dead_letter=invalid,
            redrive_status=denied.status_code,
        )
    )
    write(output / "faults.json", evidence)
    print(
        f"{len(evidence)} fault scenarios passed (actual process kills + explicit boundary injections)",
        flush=True,
    )


async def system_checks(host, output):
    limits = httpx.Limits(max_connections=60, max_keepalive_connections=60)
    async with (
        httpx.AsyncClient(base_url=host.base, trust_env=False, timeout=40, limits=limits) as user,
        httpx.AsyncClient(base_url=host.base, trust_env=False, timeout=40, limits=limits) as staff,
    ):
        await login(user, "user001")
        await login(staff, "staff001")
        cases = [fixture(i, KINDS[i % 4]) for i in range(50)]
        threads = []
        for case in cases:
            response = await user.post("/api/v1/threads", json={"order_id": case["order_id"]})
            response.raise_for_status()
            threads.append(response.json()["id"])
        barrier = asyncio.Event()

        async def one_chat(index):
            await barrier.wait()
            case = cases[index]
            text = f"我要{LABELS[index % 4]}，商品 {case['order_id']}-I1，1件，" + (
                "未收到。" if case["kind"] == "reship" else "质量问题，已收到。"
            )
            record = await chat_turn(user, threads[index], text)
            record.update(index=index, order_id=case["order_id"], scope="50_concurrent_chat")
            assert record.get("run", {}).get("status") == "completed", record
            append(output / "load-chat.jsonl", record)
            return record

        tasks = [asyncio.create_task(one_chat(i)) for i in range(50)]
        started = time.monotonic()
        barrier.set()
        await asyncio.gather(*tasks)
        chat_elapsed = ms(started)
        # Prepare approval serially to keep this outside the execution-load measurement.
        prepared = []
        for case in cases:
            prepared.append(await prepare(user, staff, case))
        barrier = asyncio.Event()

        async def one_execution(case):
            await barrier.wait()
            result = await execute(staff, case)
            assert result["result"]["job"]["status"] == "completed"
            append(output / "load-execution.jsonl", result)
            return result

        tasks = [asyncio.create_task(one_execution(c)) for c in prepared]
        started = time.monotonic()
        barrier.set()
        executed = await asyncio.gather(*tasks)
        execution_elapsed = ms(started)
        write(
            output / "load-summary.json",
            dict(
                concurrency=50,
                chat_tasks=50,
                execution_tasks=50,
                chat_elapsed_ms=chat_elapsed,
                execution_elapsed_ms=execution_elapsed,
                worker_concurrency=4,
                model="offline deterministic",
                all_completed=True,
            ),
        )
        print("50 concurrent chat and 50 concurrent execution tasks completed", flush=True)
        from evaluation.bottleneck import analyze

        analyze(output)
        await kafka_replay(host, executed, output)
        await faults(host, user, staff, output)
