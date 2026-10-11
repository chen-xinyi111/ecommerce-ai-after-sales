from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from commerce import auth
from commerce.db import connect
from commerce.models import Principal
from commerce.preview_models import (
    DemoCasePage,
    PolicyPage,
    PreviewDraft,
    PreviewInput,
    PreviewPage,
    StoredPreview,
)
from commerce.preview_service import policy_reference, save_preview, stored
from commerce.repository import visible_order
from commerce.seed import BUSINESS_DATE

router = APIRouter(prefix="/api/v1", tags=["模拟售后预览"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


@router.post(
    "/orders/{order_id}/previews",
    response_model=StoredPreview,
    dependencies=[Depends(auth.same_origin)],
)
async def preview(order_id: str, data: PreviewInput, actor: Actor):
    return await save_preview(
        order_id, actor, PreviewDraft(**data.model_dump(exclude={"request_id"})), data.request_id
    )


@router.get("/orders/{order_id}/previews", response_model=PreviewPage)
async def previews(order_id: str, actor: Actor):
    async with connect() as conn:
        await visible_order(conn, order_id, actor)
        cursor = await conn.execute(
            "SELECT * FROM commerce.previews WHERE user_id=%s AND order_id=%s ORDER BY created_at DESC,id LIMIT 30",
            (actor.id, order_id),
        )
        return {"items": [stored(row) for row in await cursor.fetchall()]}


@router.get("/previews/{preview_id}", response_model=StoredPreview)
async def one_preview(preview_id: UUID, actor: Actor):
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.previews WHERE id=%s AND user_id=%s", (preview_id, actor.id)
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(404, "预览不存在")
        await visible_order(conn, row["order_id"], actor)
        return stored(row)


@router.get("/policies", response_model=PolicyPage)
async def policies(actor: Actor):
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT ON (kind) * FROM commerce.policies WHERE version>=3 AND valid_from<=%s AND valid_until>=%s ORDER BY kind,version DESC",
            (BUSINESS_DATE, BUSINESS_DATE),
        )
        return {
            "items": [policy_reference(row) for row in await cursor.fetchall()],
            "business_date": BUSINESS_DATE,
        }


@router.get("/demo-cases", response_model=DemoCasePage)
async def demo_cases(actor: Actor):
    import json
    from pathlib import Path

    cases = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("preview_cases_v1.json")
        .read_text(encoding="utf-8")
    )
    acceptance = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("acceptance_cases_33.json")
        .read_text(encoding="utf-8")
    )
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT id FROM commerce.orders WHERE scenario='acceptance-3.3'"
        )
        prepared = {row["id"] for row in await cursor.fetchall()}
    cases = [case for case in acceptance if case["order_id"] in prepared] + cases
    if actor.role != "staff":
        async with connect() as conn:
            cursor = await conn.execute(
                "SELECT id FROM commerce.orders WHERE user_id=%s", (actor.id,)
            )
            allowed = {row["id"] for row in await cursor.fetchall()}
        cases = [
            case
            for case in cases
            if case["order_id"] in allowed and case["category"] != "identity_mismatch"
        ]
    return {"items": cases, "total": len(cases)}
