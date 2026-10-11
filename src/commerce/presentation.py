"""Business reply copy, independent of persisted policy and preview snapshots."""

import re


def product_name(value: str, sku: str) -> str:
    if value == "3.3 模拟验收商品":
        return f"商品 {sku}"
    match = re.fullmatch(r"模拟商品(\d{3})", value)
    if match and 1 <= int(match[1]) <= 60 and (not sku or sku == f"SKU-{match[1]}"):
        return f"商品{match[1]}"
    return value


def preview_summary(value: str) -> str:
    if value == "符合模拟政策，可以生成待确认方案。":
        return "符合售后条件，请核对方案后提交申请。"
    if value.startswith("当前申请不符合模拟政策："):
        value = value.replace("当前申请不符合模拟政策：", "当前申请不符合售后条件：", 1)
        value = value.replace("补发需要数据库中的物流丢失记录。", "补发需要物流记录确认包裹丢失。")
        return re.sub(
            r"库存快照可用 (\d+|None) 件，需要 (\d+) 件；本次不预留库存。",
            r"可用库存 \1 件，本次申请 \2 件。",
            value,
        )
    return value
