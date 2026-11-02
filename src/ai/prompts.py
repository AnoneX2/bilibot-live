# -*- coding: utf-8 -*-
"""模型输出解析。

Prompt 本身已外置到 `prompts/system.md`、`prompts/user.md`，
由 `prompt_store.PromptStore` 加载，方便直接改文案与风格。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..util.text import normalize, parse_json_object


@dataclass
class Decision:
    action: str = "skip"
    text: str = ""
    reason: str = ""
    confidence: float = 0.0
    raw: str = ""
    parse_error: str = ""

    @property
    def should_say(self) -> bool:
        return self.action == "say" and bool(self.text.strip())


def parse_decision(raw: str, lenient: bool = True) -> Decision:
    """把模型输出解析成 Decision，尽量容错。"""
    decision = Decision(raw=raw)
    text = normalize(raw)
    data = parse_json_object(raw)
    if isinstance(data, dict):
        action = str(data.get("action", "")).strip().lower()
        if action in {"say", "skip"}:
            decision.action = action
        elif data.get("text"):
            decision.action = "say"
        else:
            decision.action = "skip"
        decision.text = str(data.get("text") or "").strip()
        decision.reason = str(data.get("reason") or "").strip()[:120]
        try:
            decision.confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            decision.confidence = 0.0
        if not decision.text and decision.action == "say":
            decision.action = "skip"
            decision.reason = decision.reason or "模型说要说但没给文本"
        return decision

    # 不是 JSON：lenient 模式下把短文本当回复
    decision.parse_error = "非JSON输出"
    if lenient and text and len(text) <= 60 and not text.startswith("{"):
        decision.action = "say"
        decision.text = text
        decision.reason = "非JSON，按短文本兜底"
        decision.confidence = 0.3
    else:
        decision.action = "skip"
    return decision
