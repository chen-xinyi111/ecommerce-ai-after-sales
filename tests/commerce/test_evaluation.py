"""Evaluator must reject bad output and preserve frozen definitions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evaluation import report
from evaluation.cases import cases_from, corpus, freeze
from evaluation.oracle import corrected_cases, regrade
from evaluation.report import distribution, summarize
from evaluation.run import judge


def test_frozen_definition_cannot_silently_change(tmp_path):
    cases = freeze(tmp_path)
    assert len(cases) == 300
    assert sum(c["split"] == "dev" for c in cases) == 150
    assert sum(c["split"] == "frozen" for c in cases) == 150
    assert all(len(c["turns"]) == len(c["expected"]) == 2 for c in cases)
    (tmp_path / "cases.json").write_text("[]", encoding="utf-8")
    with pytest.raises(AssertionError):
        cases_from(tmp_path)
    with pytest.raises(RuntimeError, match="Frozen corpus differs"):
        freeze(tmp_path)


def test_invented_eligibility_and_missing_policy_evidence_fail():
    case = next(c for c in corpus() if c["category"] == "expired")
    forged = dict(
        status="eligible",
        request=case["expected"][1]["draft"],
        refund_cents=0,
        checks=[],
        execution_state="not_executed",
    )
    result = dict(http_status=202, events=[dict(kind="preview", payload=forged)])
    failures = judge(result, case["expected"][1], case)
    assert "expected_preview_status" in failures
    assert "policy_rejection_evidence" in failures
    assert judge(dict(http_status=202, events=[]), {"halt": True}, case) == [
        "stopped_without_preview"
    ]


def test_failed_case_and_zero_response_do_not_disappear_from_summary():
    summary = summarize(
        [
            dict(
                case_id="failed",
                passed=False,
                split="frozen",
                category="normal",
                harness_error="TimeoutError",
            )
        ]
    )
    assert summary["cases"] == summary["failed"] == 1
    assert summary["passed"] == 0
    assert summary["failures"][0]["harness_error"] == "TimeoutError"
    assert distribution([None]) == dict(n=0, p50=None, p95=None, maximum=None)
    assert distribution(range(1, 101)) == dict(n=100, p50=50, p95=95, maximum=100)
    errors = summarize(
        [
            dict(
                case_id="error",
                passed=False,
                split="dev",
                category="tool_failure",
                turns=[dict(events=[dict(kind="error", payload={})])],
            )
        ]
    )
    assert errors["extra_handoff_cases"] == 0
    assert errors["unclassified_failure_cases"] == 1


def test_policy_audit_preserves_frozen_inputs_and_does_not_award_wrong_facts(tmp_path):
    original = freeze(tmp_path)
    frozen_bytes = (tmp_path / "cases.json").read_bytes()
    cases, corrections = corrected_cases(tmp_path)
    assert len(corrections) == 10
    assert (tmp_path / "cases.json").read_bytes() == frozen_bytes
    assert [{k: v for k, v in c.items() if k != "expected"} for c in cases] == [
        {k: v for k, v in c.items() if k != "expected"} for c in original
    ]
    case = next(c for c in cases if c["category"] == "receipt_conflict" and c["kind"] == "repair")
    preview = dict(
        status="ineligible",
        request=case["expected"][0]["draft"],
        refund_cents=None,
        execution_state="not_executed",
        checks=[dict(code="received_required", passed=False)],
    )
    turn = dict(http_status=202, events=[dict(kind="preview", payload=preview)])
    raw = dict(
        case_id=case["id"],
        passed=False,
        failures=[dict(turn=1, assertions=["expected_preview_status"])],
        turns=[turn, turn],
    )
    assert regrade([raw], cases)[0]["passed"]
    assert raw["passed"] is False
    assert "grading_version" not in raw
    preview["request"] = preview["request"] | {"received": True}
    assert not regrade([raw], cases)[0]["passed"]


def test_report_regeneration_keeps_evidence_hashes_valid(tmp_path, monkeypatch):
    import hashlib
    import json

    freeze(tmp_path)
    monkeypatch.setattr(report, "DELIVERY", tmp_path)
    output = tmp_path / "runs/20261010-000000"
    output.mkdir(parents=True)
    (output / "manifest.json").write_text('{"hosts":[]}', encoding="utf-8")
    report.generate("20261010-000000")
    result = report.generate("20261010-000000")
    assert "summary.json" not in result["raw_files_sha256"]
    assert all(
        hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
        for name, digest in result["raw_files_sha256"].items()
    )
    assert (
        json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["run_id"]
        == "20261010-000000"
    )
