import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from commerce.mcp_models import READ_TOOLS
from commerce.preview_models import AfterSalesKind, PreviewDraft

ROOT = Path(__file__).parent / "skills"
VERSION = "1.0.0"
FIELDS = ["kind", "order_item_id", "quantity", "reason", "received"]


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: PreviewDraft
    note: str


class SkillConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: AfterSalesKind
    version: str
    required_fields: list[str]
    allowed_tools: list[str]
    policy_id: str
    window_days: int = Field(gt=0)
    next_step: str
    examples: list[Example]


def load_skill(kind):
    kind = AfterSalesKind(kind)
    directory = ROOT / kind.value / VERSION
    instructions = (directory / "SKILL.md").read_text(encoding="utf-8")
    config_bytes = (directory / "config.json").read_bytes()
    config = SkillConfig.model_validate_json(config_bytes)
    if (
        config.kind != kind
        or config.version != VERSION
        or config.required_fields != FIELDS
        or set(config.allowed_tools) != READ_TOOLS
        or len(config.allowed_tools) != len(READ_TOOLS)
        or config.policy_id != "POL-" + kind.value.upper()
    ):
        raise ValueError("Skill 配置与受控业务契约不一致")
    digest = hashlib.sha256(instructions.encode() + b"\0" + config_bytes).hexdigest()
    return config, instructions, digest


def skill_metadata(kind):
    config, _, digest = load_skill(kind)
    return json.loads(config.model_dump_json()) | {"content_sha256": digest}
