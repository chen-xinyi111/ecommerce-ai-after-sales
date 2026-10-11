from unittest.mock import patch

import pytest
from pydantic import SecretStr

from core.llm import get_model
from core.settings import Settings
from schema.models import DashScopeModelName


def test_dashscope_key_and_default():
    with patch.dict("os.environ", {"DASHSCOPE_APIKEY": "unit-test-key"}, clear=True):
        settings = Settings(_env_file=None)
    assert settings.DEFAULT_MODEL == DashScopeModelName.QWEN38_MAX
    assert DashScopeModelName.QWEN38_MAX in settings.AVAILABLE_MODELS
    assert "unit-test-key" not in repr(settings.DASHSCOPE_APIKEY)


def test_fake_model_wins_with_dashscope_key():
    with patch.dict(
        "os.environ", {"DASHSCOPE_APIKEY": "test", "USE_FAKE_MODEL": "true"}, clear=True
    ):
        settings = Settings(_env_file=None)
    assert settings.DEFAULT_MODEL == "fake"


def test_dashscope_adapter_configuration():
    get_model.cache_clear()
    with patch("core.llm.settings") as settings:
        settings.DASHSCOPE_APIKEY = SecretStr("unit-test-key")
        settings.DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = get_model(DashScopeModelName.QWEN38_MAX)
    assert model.model_name == "qwen3.8-max"
    assert model.streaming is True
    assert model.extra_body["enable_thinking"] is False
    assert "dashscope.aliyuncs.com" in str(model.openai_api_base)
    get_model.cache_clear()


def test_dashscope_adapter_rejects_missing_key():
    get_model.cache_clear()
    with patch("core.llm.settings") as settings:
        settings.DASHSCOPE_APIKEY = None
        with pytest.raises(ValueError, match="DASHSCOPE_APIKEY"):
            get_model(DashScopeModelName.QWEN38_MAX)
    get_model.cache_clear()
