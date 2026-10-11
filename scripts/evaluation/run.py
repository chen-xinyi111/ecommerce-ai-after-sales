"""Run honest case, replay, load and recovery evidence in newly created databases."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg import Connection, sql

from commerce.config import CommerceSettings
from commerce.db import connect_sync
from commerce.migrate import migrate
from commerce.seed import BUSINESS_DATE, seed
from commerce.seed_1301 import seed_preview_policies
from evaluation.cases import freeze

PROJECT = Path(__file__).resolve().parents[2]
DELIVERY = PROJECT / ".runtime/evaluation"
RUNTIME = PROJECT / ".runtime/evaluation"
GIT = ["git", "-c", "safe.directory=" + PROJECT.parent.as_posix()]
BASELINE_REV = "58ee53b"
BASELINE_PATH = "agent-service-toolkit/src/agents/after_sales/agent.py"
TABLES = (
    "work_orders",
    "action_results",
    "refund_ledger",
    "after_sales_shipments",
    "repair_intakes",
    "after_sales_history",
)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def append(path, data):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


def git(*args):
    return subprocess.check_output([*GIT, *args], cwd=PROJECT, text=True, encoding="utf-8").strip()


def seed_fixtures(cases):
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.inventory SET on_hand=10000,reserved=0 WHERE sku_id<>'SKU-060'"
        )
        conn.execute("UPDATE commerce.inventory SET on_hand=0,reserved=0 WHERE sku_id='SKU-060'")
        for case in cases:
            oid, f = case["order_id"], case["fixture"]
            conn.execute(
                "INSERT INTO commerce.orders(id,user_id,status,paid_cents,purchased_on,scenario) VALUES (%s,'USR-001','delivered',%s,%s,'evaluation-3.2')",
                (
                    oid,
                    f["price_cents"] * f["quantity"],
                    BUSINESS_DATE - timedelta(days=f["age_days"]),
                ),
            )
            conn.execute(
                "INSERT INTO commerce.order_items(id,order_id,sku_id,product_name,quantity,unit_price_cents) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    oid + "-I1",
                    oid,
                    f["sku_id"],
                    "评测商品" + case["id"],
                    f["quantity"],
                    f["price_cents"],
                ),
            )
            conn.execute(
                "INSERT INTO commerce.payments(id,order_id,paid_cents,status) VALUES (%s,%s,%s,'paid')",
                ("PAY-" + oid, oid, f["price_cents"] * f["quantity"]),
            )
            conn.execute(
                "INSERT INTO commerce.shipments(id,order_id,tracking_number,status) VALUES (%s,%s,%s,%s)",
                ("SHP-" + oid, oid, "SIM-E32-" + oid, f["shipment_status"]),
            )


def business_snapshot(ids=None):
    with connect_sync() as conn:
        result = {}
        for table in TABLES:
            order_column = (
                "id" if table in ("work_orders", "after_sales_history") else "application_id"
            )
            query = sql.SQL("SELECT * FROM commerce.{} ORDER BY {}").format(
                sql.Identifier(table), sql.Identifier(order_column)
            )
            if ids is not None:
                query = (
                    sql.SQL(
                        "SELECT * FROM commerce.{} WHERE application_id=ANY(%s::uuid[]) ORDER BY {}"
                    ).format(sql.Identifier(table), sql.Identifier(order_column))
                    if table != "after_sales_history"
                    else sql.SQL(
                        "SELECT * FROM commerce.after_sales_history WHERE id=ANY(%s) ORDER BY id"
                    )
                )
                rows = conn.execute(
                    query, (ids if table != "after_sales_history" else ["AS-" + i for i in ids],)
                ).fetchall()
            else:
                rows = conn.execute(query).fetchall()
            result[table] = dict(
                count=len(rows),
                sha256=hashlib.sha256(
                    json.dumps(rows, sort_keys=True, default=str).encode()
                ).hexdigest(),
            )
        for table, order_column in (
            ("orders", "id"),
            ("order_items", "id"),
            ("payments", "id"),
            ("inventory", "sku_id"),
        ):
            rows = conn.execute(
                sql.SQL("SELECT * FROM commerce.{} ORDER BY {}").format(
                    sql.Identifier(table), sql.Identifier(order_column)
                )
            ).fetchall()
            result[table] = dict(
                count=len(rows),
                sha256=hashlib.sha256(
                    json.dumps(rows, sort_keys=True, default=str).encode()
                ).hexdigest(),
            )
        return result


class Host:
    def __init__(self, run_id, variant, real, cases, port=8082):
        self.name = variant + ("_real" if real else "_offline")
        self.db = "agent_commerce_eval_" + run_id.replace("-", "_") + "_" + self.name
        self.root = RUNTIME / run_id / self.name
        self.root.mkdir(parents=True, exist_ok=False)
        self.port, self.real, self.variant = port, real, variant
        self.base = f"http://127.0.0.1:{port}"
        self.env = os.environ | dict(
            POSTGRES_DB=self.db,
            EVAL_RUNTIME=str(self.root),
            EVAL_VARIANT=variant,
            PORT=str(port),
            HOST="127.0.0.1",
            COMMERCE_ENABLED="true",
            DATABASE_TYPE="postgres",
            USE_FAKE_MODEL="false" if real else "true",
            DEFAULT_MODEL="qwen3.8-max" if real else "fake",
            ASYNC_TRANSPORT="kafka",
            LOG_LEVEL="WARNING",
            KAFKA_TOPIC="commerce.eval." + run_id + "." + self.name,
            KAFKA_GROUP="commerce-eval-" + run_id + "-" + self.name,
            REDIS_URL="redis://127.0.0.1:56379/1",
        )
        config = CommerceSettings()
        with Connection.connect(
            **(config.connection_kwargs() | {"dbname": "postgres"}), autocommit=True
        ) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.db)))
        os.environ["POSTGRES_DB"] = self.db
        migrate()
        seed()
        seed_preview_policies()
        seed_fixtures(cases)
        self.cases = cases
        self.set_fault(
            dict(
                tool_failure_orders=[
                    c["order_id"] for c in cases if c["category"] == "tool_failure"
                ]
            )
        )
        if variant == "baseline":
            source = subprocess.check_output(
                [*GIT, "show", f"{BASELINE_REV}:{BASELINE_PATH}"], cwd=PROJECT
            )
            (self.root / "baseline.py").write_bytes(source)
            self.baseline_sha = hashlib.sha256(source).hexdigest()
        else:
            self.baseline_sha = None
        self.process = None
        self.outputs = []

    def set_fault(self, settings):
        target = self.root / "fault.json"
        temporary = self.root / "fault.tmp"
        write(temporary, settings)
        temporary.replace(target)

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Host already running")
        number = len(self.outputs)
        output = (self.root / f"server-{number}.log").open("w", encoding="utf-8")
        self.outputs.append(output)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            [sys.executable, str(PROJECT / "scripts/evaluation/server.py")],
            cwd=PROJECT,
            env=self.env,
            stdout=output,
            stderr=output,
            creationflags=flags,
        )
        with httpx.Client(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Evaluation host exited; inspect {self.root}")
                try:
                    if client.get(self.base + "/api/v1/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        raise TimeoutError("Evaluation host readiness")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                self.process.kill()
            self.process.wait(timeout=15)
        for output in self.outputs:
            output.flush()

    def metadata(self):
        return dict(
            name=self.name,
            database=self.db,
            model="qwen3.8-max" if self.real else "fake",
            kafka_topic=self.env["KAFKA_TOPIC"],
            kafka_group=self.env["KAFKA_GROUP"],
            port=self.port,
            baseline_revision=BASELINE_REV if self.variant == "baseline" else None,
            baseline_source_sha256=self.baseline_sha,
        )


async def login(client, username):
    response = await client.post(
        "/api/v1/auth/login", json=dict(username=username, password="Demo-1101!")
    )
    response.raise_for_status()


def ms(start):
    return round((time.monotonic() - start) * 1000, 3)


async def chat_turn(client, thread, text, request_id=None):
    started = time.monotonic()
    request_id = request_id or str(uuid4())
    response = await client.post(
        f"/api/v1/threads/{thread}/messages", json=dict(message=text, request_id=request_id)
    )
    accepted = ms(started)
    record = dict(
        message=text, request_id=request_id, http_status=response.status_code, admission_ms=accepted
    )
    if response.status_code != 202:
        record["response"] = response.json()
        return record
    run_id = response.json()["id"]
    events, after, first = [], 0, None
    deadline = time.monotonic() + 220
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/runs/{run_id}", params={"after": after})
        response.raise_for_status()
        data = response.json()
        for event in data["events"]:
            events.append(event)
            after = max(after, event["seq"])
            if first is None and event["kind"] in ("token", "preview", "handoff", "error"):
                first = ms(started)
        if data["run"]["status"] in ("completed", "failed"):
            record.update(
                run=data["run"], events=events, full_task_ms=ms(started), first_response_ms=first
            )
            return record
        await asyncio.sleep(0.25)
    record.update(run_id=run_id, events=events, timeout=True, full_task_ms=ms(started))
    return record


def judge(turn, expected, case):
    failures = []

    def check(condition, name):
        if not condition:
            failures.append(name)

    if "http_status" in expected:
        check(turn["http_status"] == expected["http_status"], "ownership_http_status")
        return failures
    check(turn["http_status"] == 202 and not turn.get("timeout"), "task_received_and_finished")
    events = turn.get("events", [])
    previews = [e["payload"] for e in events if e["kind"] == "preview"]
    halted = any(e["kind"] in ("handoff", "error") for e in events)
    if expected.get("halt"):
        check(halted and not previews, "stopped_without_preview")
    elif expected.get("no_preview"):
        check(not previews, "cancel_does_not_preview")
    elif expected.get("safe_conflict"):
        check(
            halted or bool(previews and previews[-1]["status"] in ("needs_info", "ineligible")),
            "conflicting_receipt_not_eligible",
        )
    else:
        check(bool(previews), "preview_exists")
        if previews:
            p = previews[-1]
            check(p["status"] == expected["status"], "expected_preview_status")
            if "missing" in expected:
                check(set(p["missing_fields"]) == set(expected["missing"]), "exact_missing_fields")
            if "draft" in expected:
                check(p["request"] == expected["draft"], "explicit_facts_match")
                amount = (
                    case["fixture"]["price_cents"] * expected["draft"]["quantity"]
                    if expected["status"] == "eligible" and case["kind"] == "refund"
                    else None
                )
                check(p["refund_cents"] == amount, "refund_from_integer_price")
            if "failed_check" in expected:
                check(
                    any(
                        c["code"] == expected["failed_check"] and not c["passed"]
                        for c in p["checks"]
                    ),
                    "policy_rejection_evidence",
                )
            check(p["execution_state"] == "not_executed", "preview_not_business_execution")
    return failures


async def case_run(host, case):
    async with httpx.AsyncClient(base_url=host.base, trust_env=False, timeout=35) as client:
        await login(client, case["username"])
        response = await client.post("/api/v1/threads", json={"order_id": case["order_id"]})
        if response.status_code != 201:
            turns = [
                dict(http_status=response.status_code, response=response.json(), attempt=i + 1)
                for i in range(2)
            ]
            # Make a second actual ownership attempt, not an invented second turn.
            second = await client.post("/api/v1/threads", json={"order_id": case["order_id"]})
            turns[1].update(http_status=second.status_code, response=second.json())
            thread = None
        else:
            thread = response.json()["id"]
            turns = []
            for text in case["turns"]:
                turn = await chat_turn(client, thread, text)
                if case["category"] == "duplicate_request" and turn["http_status"] == 202:
                    duplicate = await client.post(
                        f"/api/v1/threads/{thread}/messages",
                        json=dict(message=text, request_id=turn["request_id"]),
                    )
                    turn["duplicate"] = dict(
                        http_status=duplicate.status_code,
                        same_run=duplicate.json().get("id") == turn["run"]["id"],
                    )
                turns.append(turn)
        failures = [
            dict(turn=i + 1, assertions=judge(turn, expected, case))
            for i, (turn, expected) in enumerate(zip(turns, case["expected"], strict=True))
        ]
        if case["category"] == "duplicate_request":
            for item, turn in zip(failures, turns, strict=True):
                if not turn.get("duplicate", {}).get("same_run"):
                    item["assertions"].append("duplicate_request_same_run")
        return dict(
            case_id=case["id"],
            split=case["split"],
            category=case["category"],
            host=host.name,
            thread_id=thread,
            passed=not any(f["assertions"] for f in failures),
            failures=failures,
            turns=turns,
        )


async def evaluate(host, cases, output, concurrency=4):
    before = business_snapshot()
    semaphore = asyncio.Semaphore(concurrency)
    records = []

    async def execute(case):
        async with semaphore:
            try:
                record = await case_run(host, case)
            except Exception as exc:
                record = dict(
                    case_id=case["id"],
                    split=case["split"],
                    category=case["category"],
                    host=host.name,
                    passed=False,
                    harness_error=type(exc).__name__,
                )
            append(output, record)
            records.append(record)
            if len(records) % 10 == 0:
                print(
                    f"{host.name}: {len(records)}/{len(cases)} cases; passed {sum(r['passed'] for r in records)}",
                    flush=True,
                )

    await asyncio.gather(*(execute(case) for case in cases))
    after = business_snapshot()
    summary = dict(
        count=len(records),
        passed=sum(r["passed"] for r in records),
        splits={
            s: dict(
                count=sum(r["split"] == s for r in records),
                passed=sum(r["split"] == s and r["passed"] for r in records),
            )
            for s in ("dev", "frozen")
        },
        categories={
            c: dict(
                count=sum(r["category"] == c for r in records),
                passed=sum(r["category"] == c and r["passed"] for r in records),
            )
            for c in sorted({r["category"] for r in records})
        },
        business_unchanged=before == after,
        before=before,
        after=after,
        extra_handoffs=sum(
            any(
                e["kind"] in ("handoff", "error")
                for t in r.get("turns", [])
                for e in t.get("events", [])
            )
            for r in records
        ),
    )
    assert before == after, "Chat evaluation unexpectedly changed business tables"
    return summary


def select_real(cases):
    # Two frozen cases per category, fixed in advance. Includes refusals and injected failures.
    return [
        sorted(
            (c for c in cases if c["category"] == category and c["split"] == "frozen"),
            key=lambda c: c["id"],
        )[i]
        for category in sorted({c["category"] for c in cases})
        for i in (0, 1)
    ]


async def main(args):
    cases = freeze(DELIVERY)
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = DELIVERY / "runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    manifest = dict(
        run_id=run_id,
        started_at=datetime.now(UTC).isoformat(),
        seed=3201,
        platform=platform.platform(),
        python=sys.version,
        cpu=os.cpu_count(),
        baseline_revision=git("rev-parse", BASELINE_REV),
        source_revision=git("rev-parse", "HEAD"),
        corpus_sha256=json.loads((DELIVERY / "corpus-lock.json").read_text())["sha256"],
        polling_seconds=0.25,
        workers=4,
        kafka_partitions=4,
        real_case_ids=[c["id"] for c in select_real(cases)],
        hosts=[],
    )
    source_paths = [
        p
        for parent in (
            PROJECT / "src/commerce",
            PROJECT / "src/agents/after_sales",
            PROJECT / "scripts/evaluation",
        )
        for p in parent.rglob("*.py")
    ]
    source_paths += [
        PROJECT / "src/service/service.py",
        PROJECT / "pyproject.toml",
        PROJECT / "uv.lock",
    ]
    manifest["source_files_sha256"] = {
        p.relative_to(PROJECT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source_paths)
    }
    manifest["fixture_business_date"] = BUSINESS_DATE.isoformat()
    manifest["connection_pool"] = dict(async_max=24, sync_max=4, session_reset="DISCARD ALL")
    import importlib.metadata

    manifest["packages"] = {
        name: importlib.metadata.version(name)
        for name in (
            "fastapi",
            "langgraph",
            "psycopg",
            "psycopg-pool",
            "aiokafka",
            "redis",
            "langchain-openai",
            "opentelemetry-sdk",
        )
    }
    write(output / "manifest.json", manifest)
    write(DELIVERY / "latest-run.json", dict(run_id=run_id))
    summaries = {}
    for real in [False, True] if args.real else [False]:
        selected = select_real(cases) if real else cases
        if args.limit:
            selected = selected[: args.limit]
        for variant in ("baseline", "final"):
            host = Host(run_id, variant, real, cases)
            manifest["hosts"].append(host.metadata())
            write(output / "manifest.json", manifest)
            try:
                host.start()
                summaries[host.name] = await evaluate(
                    host, selected, output / (host.name + ".jsonl")
                )
                write(output / "case-summary.json", summaries)
                if variant == "final" and not real and not args.limit:
                    from evaluation.system import system_checks

                    await system_checks(host, output)
            finally:
                host.stop()
                fault_log = host.root / "fault-events.jsonl"
                if fault_log.exists():
                    (output / (host.name + "-faults.jsonl")).write_bytes(fault_log.read_bytes())
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    write(output / "manifest.json", manifest)
    print(f"Evaluation complete: {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--real",
        action="store_true",
        help="Also run the preselected 30 real-model cases per variant",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Pilot only; does not satisfy 300-case gate"
    )
    args = parser.parse_args()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(main(args))
