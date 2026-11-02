# -*- coding: utf-8 -*-
"""文本工具：清洗、截断、相似度、JSON 提取。"""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

_URL_RE = re.compile(r"(https?://|www\.|b23\.tv|\.com/|\.cn/)", re.IGNORECASE)
_AT_RE = re.compile(r"@[\w\u4e00-\u9fff\-]{1,20}")
# 联系方式：关键词、手机号，以及「关键词后面紧跟的一串号码」。
# 注意不要裸匹配 6~11 位数字——“123456 伤害”这种正常回复会被误杀。
_CONTACT_RE = re.compile(
    r"(加\s*群|私\s*聊|vx|wx|weixin|微信|qq\s*群?|企鹅群|加我|联系我|扫码|二维码|"
    r"\b1[3-9]\d{9}\b)",
    re.IGNORECASE,
)
# 关键词附近出现的号码（QQ 号等）：只用「明确在给联系方式」的词，
# 不用单独的「加/群/找」——「加到 123456 血」这种正常回复不该被拦。
_CONTACT_NUMBER_RE = re.compile(
    r"(加群|进群|拉群|私聊|私我|联系我|qq|q\s*群|vx|wx|weixin|微信|扣扣|企鹅群|扫码)"
    r"[^\n]{0,8}?(\d{5,11})",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """全角转半角、去多余空白。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", text).strip()


def clean_for_send(text: str) -> str:
    """发送前的轻量清洗：去换行、去首尾空白、压缩空格。"""
    text = (text or "").replace("\r", "").replace("\n", " ")
    text = re.sub(r"\[[^\]]{1,12}\]", lambda m: m.group(0), text)  # 保留 B 站表情
    text = _WS_RE.sub(" ", text).strip()
    return text


def char_len(text: str) -> int:
    return len(text or "")


def truncate(text: str, max_chars: int, ellipsis: str = "") -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + ellipsis


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def has_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def has_at(text: str) -> bool:
    return bool(_AT_RE.search(text or ""))


def has_contact(text: str) -> bool:
    text = text or ""
    return bool(_CONTACT_RE.search(text) or _CONTACT_NUMBER_RE.search(text))


def _extract_json_candidates(raw: str) -> list[str]:
    """从模型输出中挑出可能的 JSON 片段（支持 ```json 包裹）。"""
    raw = (raw or "").strip()
    out: list[str] = []
    if "```" in raw:
        for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE):
            out.append(block.strip())
    out.append(raw)
    # 最外层花括号
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        out.append(raw[start : end + 1])
    return out


def parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    for candidate in _extract_json_candidates(raw):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def is_meaningful_text(text: str, min_len: int = 2) -> bool:
    """判断 ASR 文本是否值得进入上下文。"""
    text = normalize(text)
    if len(text) < min_len:
        return False
    # 纯标点/符号
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    # 单字符复读，如 "啊啊啊啊"、"。。。"
    core = text.strip("。，、！？!?.,~～ ")
    if core and len(set(core)) <= 1:
        return False
    return True
