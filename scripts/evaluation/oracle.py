"""Audited policy correction: retain frozen inputs and all original v1 grades."""

import copy
import hashlib
import json
from pathlib import Path

from evaluation.cases import cases_from
from evaluation.run import judge, write


def corrected_cases(directory):
    cases = copy.deepcopy(cases_from(directory))
    corrections = []
    for case in cases:
        if case["category"] == "receipt_conflict" and case["kind"] in ("exchange", "repair"):
            before = copy.deepcopy(case["expected"][0])
            assert before["draft"]["received"] is False
            assert before["draft"]["reason"] == "not_received"
            case["expected"][0] = before | {
                "status": "ineligible",
                "failed_check": "received_required",
            }
            corrections.append(
                dict(
                    case_id=case["id"],
                    turn=1,
                    original=before,
                    corrected=case["expected"][0],
                    reason="Confirmed v3 policy requires receipt for exchange/repair; nonreceipt cannot be eligible",
                )
            )
    assert len(corrections) == 10
    return cases, corrections


def regrade(raw, cases):
    definitions = {case["id"]: case for case in cases}
    corrected = copy.deepcopy(raw)
    for record in corrected:
        record["original_v1_grade"] = dict(
            passed=record["passed"], failures=record.get("failures", [])
        )
        record["grading_version"] = "1.1-policy-correction"
        if "harness_error" in record:
            continue
        case = definitions[record["case_id"]]
        failures = [
            dict(turn=i + 1, assertions=judge(turn, expected, case))
            for i, (turn, expected) in enumerate(
                zip(record["turns"], case["expected"], strict=True)
            )
        ]
        if case["category"] == "duplicate_request":
            for item, turn in zip(failures, record["turns"], strict=True):
                if not turn.get("duplicate", {}).get("same_run"):
                    item["assertions"].append("duplicate_request_same_run")
        record["failures"] = failures
        record["passed"] = not any(f["assertions"] for f in failures)
    return corrected


def audit(directory, output, raw_by_variant):
    cases, corrections = corrected_cases(directory)
    write(directory / "cases-policy-corrected.json", cases)
    config = Path(__file__).resolve().parents[2] / "src/commerce/seed_1301.py"
    evidence = dict(
        version="1.1-policy-correction",
        original_corpus_sha256=json.loads(
            (directory / "corpus-lock.json").read_text(encoding="utf-8")
        )["sha256"],
        source="src/commerce/seed_1301.py POLICY_RULES v3; approved preview rules require received_required for exchange/repair",
        source_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        post_hoc=True,
        new_model_requests=0,
        changed_inputs=0,
        changed_first_turn_expected_statuses=10,
        corrections=corrections,
    )
    write(directory / "oracle-corrections.json", evidence)
    graded = {name: regrade(raw, cases) for name, raw in raw_by_variant.items()}
    for name, records in graded.items():
        path = output / ("regraded-" + name + ".jsonl")
        content = [
            json.dumps(
                dict(
                    case_id=record["case_id"],
                    original=record["original_v1_grade"],
                    corrected=dict(passed=record["passed"], failures=record.get("failures", [])),
                    original_record_sha256=hashlib.sha256(
                        json.dumps(original, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                ),
                ensure_ascii=False,
            )
            for record, original in zip(records, raw_by_variant[name], strict=True)
        ]
        path.write_text("\n".join(content) + "\n", encoding="utf-8")
    return graded, evidence
