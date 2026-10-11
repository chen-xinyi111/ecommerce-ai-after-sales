"""Small repeatable product evaluation. New output directory and isolated DB each run.

Answers are scored only for declared key facts, not overall language quality.
Human reference review is explicitly pending until a person signs the case file.
"""

import argparse
import asyncio
import hashlib
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from psycopg import Connection, sql

from commerce.config import CommerceSettings
from evaluation.oracle import corrected_cases
from evaluation.run import DELIVERY, Host, business_snapshot, case_run, write


def quantile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    index = int(position)
    following = min(index + 1, len(values) - 1)
    return round(values[index] + (values[following] - values[index]) * (position - index), 3)


def answer_fact_check(turn, expected, case):
    text = "".join(
        event["payload"].get("text", "")
        for event in turn.get("events", [])
        if event["kind"] == "token"
    )
    if not text or turn.get("run", {}).get("status") != "completed":
        return False
    state = expected.get("status")
    if state == "needs_info":
        return "补充" in text
    if state == "ineligible":
        return "不符合" in text
    if state == "eligible":
        if "不符合" in text or "符合" not in text or "核对" not in text or "尚未执行" not in text:
            return False
        if case["kind"] == "refund":
            amount = case["fixture"]["price_cents"] * expected["draft"]["quantity"]
            return f"{amount // 100}.{amount % 100:02d}" in text
        return True
    return False


async def evaluate(args):
    corpus, _ = corrected_cases(DELIVERY)
    selected = [
        next(
            case
            for case in corpus
            if case["kind"] == kind and case["category"] == category and case["split"] == "frozen"
        )
        for kind in ("refund", "exchange", "reship", "repair")
        for category in ("normal", "expired", "staged_fields")
    ]
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = Path(__file__).resolve().parents[2] / ".runtime/product-evaluation" / run_id
    output.mkdir(parents=True, exist_ok=False)
    write(output / "cases.json", selected)
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "scope": "12 frozen synthetic Chinese cases, concurrency 1; not production accuracy",
        "human_reference_review": "pending",
        "grader_version": "2-business-facts",
        "business_date": "2026-10-09",
        "case_sha256": hashlib.sha256((output / "cases.json").read_bytes()).hexdigest(),
        "poll_seconds": 0.25,
        "hosts": [],
        "source_sha256": {
            str(path.relative_to(output.parents[3])): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__).resolve().parents[2] / "src/commerce").glob("*.py")
        },
    }
    summaries = []
    original_db = os.environ.get("POSTGRES_DB")
    try:
        for real in [True] if args.real_only else [False, True] if args.real else [False]:
            host = Host("product_" + run_id, "final", real, selected, port=18082)
            host.env["BUSINESS_BACKEND"] = "database"
            manifest["hosts"].append(host.metadata())
            write(output / "manifest.json", manifest)
            records = []
            before = business_snapshot()
            try:
                await asyncio.to_thread(host.start)
                for case in selected:
                    record = await case_run(host, case)
                    record["answer_key_facts"] = [
                        answer_fact_check(turn, expected, case)
                        for turn, expected in zip(record["turns"], case["expected"], strict=True)
                    ]
                    records.append(record)
                    write(output / (host.name + "-raw.json"), records)
                    print(
                        f"{host.name}: {len(records)}/{len(selected)}; {record['case_id']}: {record['passed']}",
                        flush=True,
                    )
                times = [
                    turn["full_task_ms"]
                    for record in records
                    for turn in record["turns"]
                    if "full_task_ms" in turn
                ]
                first = [
                    turn["first_response_ms"]
                    for record in records
                    for turn in record["turns"]
                    if turn.get("first_response_ms") is not None
                ]
                turns = sum(len(record["turns"]) for record in records)
                summary = {
                    "model_mode": "real" if real else "offline",
                    "host": host.metadata(),
                    "cases": len(records),
                    "turns": turns,
                    "answer_key_facts_correct": sum(
                        sum(record["answer_key_facts"]) for record in records
                    ),
                    "answer_key_facts_accuracy": sum(
                        sum(record["answer_key_facts"]) for record in records
                    )
                    / turns,
                    "plan_cases_correct": sum(record["passed"] for record in records),
                    "plan_case_accuracy": sum(record["passed"] for record in records)
                    / len(records),
                    "first_reply_ms": {
                        "count": len(first),
                        "p50": quantile(first, 0.5),
                        "p95": quantile(first, 0.95),
                    },
                    "full_reply_ms": {
                        "count": len(times),
                        "p50": quantile(times, 0.5),
                        "p95": quantile(times, 0.95),
                    },
                    "business_unchanged": before == business_snapshot(),
                    "human_reference_review": "pending",
                }
                assert summary["business_unchanged"]
                summaries.append(summary)
                write(output / "summary.json", summaries)
            finally:
                await asyncio.to_thread(host.stop)
                # Exact generated name only; never touch the workstation database.
                assert host.db.startswith("agent_commerce_eval_product_")
                with Connection.connect(
                    **(CommerceSettings().connection_kwargs() | {"dbname": "postgres"}),
                    autocommit=True,
                ) as admin:
                    admin.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(host.db))
                    )
    finally:
        if original_db is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = original_db
    print(f"Output: {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--real", action="store_true", help="Also run the same cases with the configured real model"
    )
    modes.add_argument(
        "--real-only", action="store_true", help="Run only the real-model cases after authorization"
    )
    args = parser.parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(evaluate(args))
