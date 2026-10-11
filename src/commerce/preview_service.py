"""Read-only eligibility rules. Model text never determines money or permissions."""

import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import SerializationFailure, UniqueViolation
from psycopg.types.json import Jsonb

from commerce.db import connect
from commerce.preview_models import (
    PolicyReference,
    PreviewDraft,
    PreviewFacts,
    PreviewResult,
    RuleCheck,
    StoredPreview,
)
from commerce.repository import visible_order
from commerce.seed import BUSINESS_DATE

QUESTIONS = {
    "kind": "您希望退款、换货、补发还是维修？",
    "order_item_id": "请选择要办理售后的商品，或提供订单明细编号。",
    "quantity": "要办理几件？请提供整数数量。",
    "reason": "原因是质量问题、错发商品、未收到货，还是不想要了？",
    "received": "您是否已经收到商品？",
}
KIND_LABEL = {"refund": "退款", "exchange": "换货", "reship": "补发", "repair": "维修"}


def policy_reference(row):
    return PolicyReference(
        **{
            key: row[key]
            for key in ("id", "version", "kind", "valid_from", "valid_until", "explanation")
        },
        window_days=row["conditions"]["window_days"],
    )


def evaluate(
    order, items, histories, shipment, stock, policy, draft: PreviewDraft
) -> PreviewResult:
    """Pure evaluator; inputs are trusted database snapshots, draft is untrusted user facts."""
    item = next((item for item in items if item["id"] == draft.order_item_id), None)
    if draft.order_item_id and item is None:
        raise HTTPException(404, "订单明细不存在")
    historical_refund = sum(row["refund_cents"] for row in histories)
    historical_qty = sum(
        row["quantity"] for row in histories if item and row["order_item_id"] == item["id"]
    )
    facts = PreviewFacts(
        purchased_on=order["purchased_on"],
        age_days=(BUSINESS_DATE - order["purchased_on"]).days,
        order_status=order["status"],
        shipment_status=shipment["status"] if shipment else None,
        paid_cents=order["paid_cents"],
        historical_refund_cents=historical_refund,
        remaining_refund_cents=order["paid_cents"] - historical_refund,
        purchased_quantity=item["quantity"] if item else None,
        historical_after_sales_quantity=historical_qty if item else None,
        remaining_quantity=item["quantity"] - historical_qty if item else None,
        available_stock=stock["on_hand"] - stock["reserved"] if stock else None,
        unit_price_cents=item["unit_price_cents"] if item else None,
    )
    missing = [name for name in QUESTIONS if getattr(draft, name) is None]
    checks = []

    def check(code, passed, explanation):
        checks.append(RuleCheck(code=code, passed=bool(passed), explanation=explanation))

    if draft.kind:
        check("policy_available", policy is not None, "使用业务日期内有效的最新模拟政策版本。")
    if policy:
        rules = policy["conditions"]
        check(
            "within_window",
            0 <= facts.age_days <= rules["window_days"],
            f"购买后 {facts.age_days} 天；政策允许 0–{rules['window_days']} 天（含边界）。",
        )
        check(
            "order_status",
            order["paid_cents"] > 0 and order["status"] in rules["statuses"],
            "订单须已支付且处于政策允许的状态。",
        )
        if draft.reason:
            check(
                "reason_allowed",
                draft.reason in rules["reasons"],
                "申请原因须在所选售后政策范围内。",
            )
        if draft.received is not None and draft.reason:
            check(
                "receipt_consistent",
                (draft.received and draft.reason != "not_received")
                or (not draft.received and draft.reason == "not_received"),
                "收货状态与未收到货的原因须一致。",
            )
        if draft.kind in ("exchange", "repair") and draft.received is not None:
            check("received_required", draft.received, "换货或维修需要已经收到商品。")
        if draft.kind == "reship":
            check(
                "shipment_lost", facts.shipment_status == "lost", "补发需要数据库中的物流丢失记录。"
            )
            if draft.received is not None:
                check("not_received_required", not draft.received, "补发需要尚未收到商品。")
    if item:
        check(
            "history_consistent",
            historical_qty == item["after_sales_quantity"]
            and historical_refund == order["refunded_cents"],
            "历史售后数量与累计退款必须和订单一致。",
        )
        if draft.quantity is not None:
            assert facts.remaining_quantity is not None
            check(
                "quantity_available",
                draft.quantity <= facts.remaining_quantity,
                f"购买 {item['quantity']} 件，历史已办理 {historical_qty} 件，剩余 {facts.remaining_quantity} 件。",
            )
            if draft.kind in ("exchange", "reship"):
                check(
                    "stock_available",
                    facts.available_stock is not None and facts.available_stock >= draft.quantity,
                    f"库存快照可用 {facts.available_stock} 件，需要 {draft.quantity} 件；本次不预留库存。",
                )
            if draft.kind == "refund":
                check(
                    "refund_limit",
                    draft.quantity * item["unit_price_cents"] <= facts.remaining_refund_cents,
                    "按商品单价乘申请数量计算，不能超过订单剩余可退实付。",
                )
    failed = [row for row in checks if not row.passed]
    status = "needs_info" if missing else "ineligible" if failed else "eligible"
    summary = (
        "请补充信息后重新预览。"
        if missing
        else "符合模拟政策，可以生成待确认方案。"
        if not failed
        else "当前申请不符合模拟政策：" + "；".join(row.explanation for row in failed)
    )
    cents = None
    if status == "eligible" and draft.kind == "refund":
        assert item is not None and draft.quantity is not None
        cents = draft.quantity * item["unit_price_cents"]
    return PreviewResult(
        order_id=order["id"],
        status=status,
        business_date=BUSINESS_DATE,
        request=draft,
        policy=policy_reference(policy) if policy else None,
        facts=facts,
        checks=checks,
        missing_fields=missing,
        questions=[QUESTIONS[name] for name in missing],
        refund_cents=cents,
        summary=summary,
        warnings=[
            "所有订单与政策均为模拟用途；业务日期固定为 2026-10-09。",
            "售后期限以购买日期计算；收货状态为用户自报信息。",
            "这是条件快照，未创建工单、退款、发货或扣减库存；后续执行必须重新核验。",
        ]
        + (["维修预览仅判断基础资格，不代表已完成故障诊断。"] if draft.kind == "repair" else []),
    )


async def compute(conn, order_id, actor, draft, *, reserved_credit=0):
    order = await visible_order(conn, order_id, actor)

    async def rows(table):
        # Names are fixed by this module, never request data.
        cursor = await conn.execute(
            f"SELECT * FROM commerce.{table} WHERE order_id=%s ORDER BY id", (order_id,)
        )
        return await cursor.fetchall()

    items = await rows("order_items")
    histories = await rows("after_sales_history")
    shipments = await rows("shipments")
    item = next((row for row in items if row["id"] == draft.order_item_id), None)
    stock = None
    if item:
        cursor = await conn.execute(
            "SELECT * FROM commerce.inventory WHERE sku_id=%s", (item["sku_id"],)
        )
        stock = await cursor.fetchone()
        if stock and reserved_credit:
            stock = stock | {"reserved": stock["reserved"] - reserved_credit}
    policy = None
    if draft.kind:
        cursor = await conn.execute(
            "SELECT * FROM commerce.policies WHERE kind=%s AND valid_from<=%s AND valid_until>=%s AND version>=3 ORDER BY version DESC LIMIT 1",
            (draft.kind, BUSINESS_DATE, BUSINESS_DATE),
        )
        policy = await cursor.fetchone()
    return evaluate(
        order, items, histories, shipments[0] if shipments else None, stock, policy, draft
    )


def stored(row):
    return StoredPreview(
        **row["result"],
        id=row["id"],
        version=row["version"],
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
    )


async def save_preview(
    order_id, actor, draft, request_id, *, thread_id=None, run_id=None, expected_result=None
):
    for attempt in range(3):
        try:
            return await _save_preview(
                order_id,
                actor,
                draft,
                request_id,
                thread_id=thread_id,
                run_id=run_id,
                expected_result=expected_result,
            )
        except (SerializationFailure, UniqueViolation):
            if attempt == 2:
                raise HTTPException(409, "预览并发冲突，请使用相同请求编号重试") from None
    raise RuntimeError("Preview retries exhausted")


async def _save_preview(
    order_id, actor, draft, request_id, *, thread_id=None, run_id=None, expected_result=None
):
    async with connect() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        await visible_order(conn, order_id, actor)
        from commerce.approval_repository import lock_scope, supersede

        await lock_scope(conn, actor.id, order_id)
        # Serializes duplicate request IDs without mutating financial tables.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"preview:{actor.id}:{request_id}",),
        )
        cursor = await conn.execute(
            "SELECT * FROM commerce.previews WHERE user_id=%s AND request_id=%s",
            (actor.id, request_id),
        )
        previous = await cursor.fetchone()
        data = draft.model_dump(mode="json")
        if previous:
            if (
                previous["order_id"] != order_id
                or previous["input"] != data
                or previous["run_id"] != run_id
            ):
                raise HTTPException(409, "该请求编号已用于另一份预览")
            return stored(previous)
        if thread_id:
            from commerce.chat_repository import visible_thread

            thread = await visible_thread(conn, thread_id, actor)
            if thread["order_id"] != order_id:
                raise HTTPException(404, "会话不存在")
        result = (await compute(conn, order_id, actor, draft)).model_dump(mode="json")
        if expected_result is not None and result != expected_result:
            raise HTTPException(409, "方案条件发生变化，请重新生成预览")
        digest = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cursor = await conn.execute(
            "SELECT coalesce(max(version),0)+1 AS version FROM commerce.previews WHERE user_id=%s AND order_id=%s",
            (actor.id, order_id),
        )
        version_row = await cursor.fetchone()
        assert version_row is not None
        version = version_row["version"]
        cursor = await conn.execute(
            "INSERT INTO commerce.previews(id,user_id,order_id,thread_id,run_id,request_id,version,input,result,content_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                actor.id,
                order_id,
                thread_id,
                run_id,
                request_id,
                version,
                Jsonb(data),
                Jsonb(result),
                digest,
            ),
        )
        row = await cursor.fetchone()
        assert row is not None
        await supersede(conn, actor.id, order_id, row["id"])
        return stored(row)


def preview_text(result):
    from commerce.presentation import preview_summary

    return (
        preview_summary(result.summary)
        + ("\n" + "\n".join(result.questions) if result.questions else "")
        + (
            f"\n预览退款金额：{result.refund_cents // 100}.{result.refund_cents % 100:02d} 元。"
            if result.refund_cents is not None
            else ""
        )
        + (
            "\n方案尚未执行，请核对后提交申请；提交后等待客服审批。"
            if getattr(result, "status", "eligible") == "eligible"
            else "\n请补充必要信息后重新查看方案。"
            if result.status == "needs_info"
            else "\n暂不能提交申请，请查看不符合条件的原因或联系人工客服。"
        )
    )
