"""Staff-only access to saved evaluation evidence; never launches an evaluation."""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from commerce import auth
from commerce.approval_repository import require_staff
from commerce.models import Principal

router = APIRouter(prefix="/api/v1/evaluation", tags=["评测证据"])
Actor = Annotated[Principal, Depends(auth.current_actor)]
REPORT_ROOT = Path(__file__).resolve().parents[2] / ".runtime/evaluation"


def summary():
    path = REPORT_ROOT / "summary.json"
    if not path.is_file():
        raise HTTPException(503, "评测报告尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/summary", response_model=dict[str, Any])
async def evaluation_summary(actor: Actor):
    require_staff(actor)
    return summary()


@router.get("/report", response_class=FileResponse)
async def evaluation_report(actor: Actor):
    require_staff(actor)
    path = REPORT_ROOT / "评测报告.html"
    if not path.is_file():
        raise HTTPException(503, "评测报告尚未生成")
    return FileResponse(
        path,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        },
    )


@router.get("/runs/{run_id}/{filename}", response_class=FileResponse)
async def evaluation_evidence(run_id: str, filename: str, actor: Actor):
    require_staff(actor)
    data = summary()
    if run_id != data["run_id"] or not re.fullmatch(r"\d{8}-\d{6}", run_id):
        raise HTTPException(404, "评测记录不存在")
    expected = data["raw_files_sha256"].get(filename)
    if expected is None or not re.fullmatch(r"[a-z][a-z0-9_-]*\.jsonl?", filename):
        raise HTTPException(404, "评测记录不存在")
    path = (REPORT_ROOT / "runs" / run_id / filename).resolve()
    if not path.is_relative_to(REPORT_ROOT.resolve()) or not path.is_file():
        raise HTTPException(404, "评测记录不存在")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise HTTPException(409, "评测记录摘要已变化，请维护人员重新核对")
    return FileResponse(
        path,
        media_type="application/json" if filename.endswith(".json") else "application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
