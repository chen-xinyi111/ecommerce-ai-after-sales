from types import SimpleNamespace

from commerce.chat_repository import error_message
from commerce.presentation import preview_summary, product_name
from commerce.preview_service import preview_text


def test_reply_copy_preserves_original_snapshot():
    result = SimpleNamespace(
        summary="符合模拟政策，可以生成待确认方案。",
        questions=[],
        refund_cents=10001,
    )
    before = vars(result).copy()
    text = preview_text(result)
    assert "模拟" not in text
    assert "100.01 元" in text
    assert "尚未执行" in text
    assert vars(result) == before
    assert preview_summary("当前申请不符合模拟政策：超过期限") == "当前申请不符合售后条件：超过期限"


def test_unknown_product_names_are_preserved():
    assert product_name("3.3 模拟验收商品", "SKU-001") == "商品 SKU-001"
    assert product_name("模拟商品032", "SKU-032") == "商品032"
    assert product_name("模拟飞行器套装", "SKU-010") == "模拟飞行器套装"
    assert product_name("模拟商品999", "SKU-999") == "模拟商品999"
    assert product_name("模拟商品001", "SUPPLIER-SKU-001") == "模拟商品001"


def test_customer_errors_have_business_next_steps():
    for code in ["model_quota", "model_unavailable", "step_limit", "model_changed"]:
        value = error_message(code)
        assert "百炼" not in value and "模型" not in value
        assert "重新发送" in value or "转人工" in value
