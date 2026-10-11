"""Persist measured worker queueing and span evidence for the load run."""

import argparse
import json
import os
from datetime import datetime

from commerce.db import connect_sync
from evaluation.report import distribution, read, records
from evaluation.run import DELIVERY, write


def analyze(output):
    manifest = read(output / "manifest.json")
    database = next(h["database"] for h in manifest["hosts"] if h["name"] == "final_offline")
    assert database.startswith("agent_commerce_eval_")
    previous = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = database
    raw = records(output / "load-chat.jsonl")
    assert len(raw) == 50
    ids = [r["run"]["id"] for r in raw]
    start = min(datetime.fromisoformat(r["run"]["created_at"]).timestamp() for r in raw) * 1e9
    end = (max(datetime.fromisoformat(r["run"]["finished_at"]).timestamp() for r in raw) + 1) * 1e9
    try:
        with connect_sync() as conn:
            workers = conn.execute(
                "SELECT attributes->>'task_id' AS id,started_ns,ended_ns FROM commerce.trace_spans WHERE name='worker.chat' AND attributes->>'task_id'=ANY(%s)",
                (ids,),
            ).fetchall()
            spans = conn.execute(
                "SELECT name,count(*) AS n,percentile_cont(.5) WITHIN GROUP (ORDER BY (ended_ns-started_ns)/1000000.0) AS p50_ms,percentile_cont(.95) WITHIN GROUP (ORDER BY (ended_ns-started_ns)/1000000.0) AS p95_ms FROM commerce.trace_spans WHERE started_ns BETWEEN %s AND %s GROUP BY name ORDER BY name",
                (int(start), int(end)),
            ).fetchall()
            polls = conn.execute(
                "SELECT count(*) AS n FROM commerce.trace_spans WHERE name='api.request' AND attributes->>'http.route'='/api/v1/runs/{run_id}' AND started_ns BETWEEN %s AND %s",
                (int(start), int(end)),
            ).fetchone()["n"]
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = previous
    assert len(workers) == len({w["id"] for w in workers}) == 50
    starts = {r["id"]: r for r in workers}
    queue = [
        (
            starts[r["run"]["id"]]["started_ns"] / 1e9
            - datetime.fromisoformat(r["run"]["created_at"]).timestamp()
        )
        * 1000
        for r in raw
    ]
    processing = [(r["ended_ns"] - r["started_ns"]) / 1e6 for r in workers]
    result = dict(
        scope="50 concurrent offline chat tasks; four Kafka consumers; 250ms polling client",
        worker_spans=50,
        queue_ms=distribution(queue),
        worker_ms=distribution(processing),
        run_poll_requests=polls,
        span_timings=spans,
        interpretation="Measured queueing and worker spans grow under load. Four consumers limit processing concurrency; repeated API polling and synchronous trace export add database and event-loop work. Causal contributions were not isolated by ablation; this is not a production capacity or SLO claim.",
    )
    write(
        output / "load-worker-spans.json",
        dict(worker_spans=workers, window_start_ns=int(start), window_end_ns=int(end)),
    )
    write(output / "bottleneck-analysis.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args = parser.parse_args()
    output = DELIVERY / "runs" / (args.run_id or read(DELIVERY / "latest-run.json")["run_id"])
    print(json.dumps(analyze(output), ensure_ascii=False))
