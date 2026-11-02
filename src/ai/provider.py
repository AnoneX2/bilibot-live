# -*- coding: utf-8 -*-
"""LLM Provider：DeepSeek（OpenAI 兼容）/ Mock / Ollama。"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from ..config import Config
from ..util.log import get_logger

logger = get_logger("ai.provider")


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    elapsed: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    text_source: str = "content"


def extract_message_text(message: Dict[str, Any]) -> tuple[str, str]:
    """从 message 中取出可用的文本。

    不同网关/模型把结果放在不同字段：
    * 普通模型：content
    * 推理模型：content 为空，思考在 reasoning_content（此时不能直接当答案用）
    * 部分网关：content 是 parts 数组，或字段名为 reasoning / thinking
    返回 (text, source)，source 用于判断这段文本是不是「真正的回答」。
    """
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)
    if isinstance(content, str) and content.strip():
        return content.strip(), "content"

    for key in ("reasoning_content", "reasoning", "thinking", "analysis"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
        if isinstance(value, list):
            joined = "".join(str(v) for v in value if isinstance(v, str))
            if joined.strip():
                return joined.strip(), key
    return "", ""


def parse_response_body(body_text: str) -> Dict[str, Any]:
    """解析响应体，兼容「忽略 stream=false 仍返回 SSE」的网关。

    正常返回 OpenAI 兼容 JSON；若网关返回 `data: {...}` 流，则把 delta 拼成
    一个 message，保证上层逻辑不用区分。
    """
    body = (body_text or "").strip()
    if not body:
        raise LLMError("响应体为空")
    if not body.startswith("data:"):
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是 JSON（网关可能强制流式）: {body[:200]}") from exc

    content: list[str] = []
    reasoning: list[str] = []
    finish = ""
    usage: Dict[str, Any] = {}
    model = ""
    last_chunk: Dict[str, Any] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        last_chunk = obj
        model = obj.get("model", model)
        usage = obj.get("usage") or usage
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for key in ("reasoning_content", "reasoning"):
                if isinstance(delta.get(key), str):
                    reasoning.append(delta[key])
            finish = choice.get("finish_reason") or finish
    if not last_chunk:
        raise LLMError(f"SSE 响应里没有可解析的 data 块: {body[:200]}")
    last_chunk["choices"] = [
        {
            "message": {
                "content": "".join(content),
                "reasoning_content": "".join(reasoning),
            },
            "finish_reason": finish,
        }
    ]
    last_chunk.setdefault("model", model)
    if usage:
        last_chunk["usage"] = usage
    return last_chunk


class BaseProvider:
    name = "base"

    async def complete(self, system: str, user: str, image_jpeg: Optional[bytes] = None) -> LLMResponse:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    @property
    def supports_vision(self) -> bool:
        return False


class DeepSeekProvider(BaseProvider):
    """OpenAI 兼容的 /chat/completions，多模态内容用 image_url + data URL。"""

    name = "deepseek"

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._api_key = cfg.api_key()
        self._session: Optional[aiohttp.ClientSession] = None
        self._use_json_mode = True

    @property
    def supports_vision(self) -> bool:
        return self._cfg.llm.vision

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._cfg.llm.timeout_sec)
            )
        return self._session

    def _payload(
        self, system: str, user: str, image_jpeg: Optional[bytes],
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        if image_jpeg and self._cfg.llm.vision:
            b64 = base64.b64encode(image_jpeg).decode("ascii")
            user_content: Any = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        else:
            user_content = user
        payload: Dict[str, Any] = {
            "model": self._cfg.llm.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": self._cfg.llm.temperature,
            "max_tokens": max_tokens or self._cfg.llm.max_tokens,
            "stream": False,
        }
        if self._use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 思考开关与等级：官方文档中 thinking.type 只接受 enabled/disabled，
        # 等级用 reasoning_effort（none/low/high/max）；只传 thinking.type=high 是无效的。
        effort = (self._cfg.llm.reasoning_effort or "auto").lower()
        thinking = (self._cfg.llm.thinking or "auto").lower()
        if effort != "auto":
            payload["reasoning_effort"] = effort
            if thinking == "auto":
                payload["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
        if thinking != "auto":
            payload["thinking"] = {"type": thinking}
        # 透传服务端特有参数（高级用法），但不允许覆盖消息/模型/流式等核心字段
        if self._cfg.llm.extra_body:
            reserved = {"messages", "model", "stream", "response_format"}
            payload.update(
                {k: v for k, v in self._cfg.llm.extra_body.items() if k not in reserved}
            )
        return payload

    async def complete(self, system: str, user: str, image_jpeg: Optional[bytes] = None) -> LLMResponse:
        if not self._api_key:
            raise LLMError(
                "缺少 API Key：请在设置面板「大模型 → API Key」填写并保存", status=401
            )
        url = self._cfg.llm.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        session = await self._ensure_session()

        last_error: Optional[Exception] = None
        max_tokens = self._cfg.llm.max_tokens
        attempt = 0
        while attempt < 3:
            attempt += 1
            payload = self._payload(system, user, image_jpeg, max_tokens)
            started = time.time()
            try:
                async with session.post(url, json=payload, headers=headers) as res:
                    body_text = await res.text()
                    if res.status != 200:
                        retryable = res.status in (408, 429, 500, 502, 503, 504)
                        # 某些服务不支持 response_format，降级重试一次
                        if res.status == 400 and self._use_json_mode and "response_format" in body_text:
                            logger.warning("服务端不支持 json_object，去掉该参数重试")
                            self._use_json_mode = False
                            continue
                        # 服务端上限低于我们提升后的 max_tokens：回退一档再试
                        if (
                            res.status == 400
                            and "max_tokens" in body_text.lower()
                            and attempt >= 2
                            and max_tokens > self._cfg.llm.max_tokens
                        ):
                            fallback = max(self._cfg.llm.max_tokens, max_tokens // 2)
                            if fallback < max_tokens:
                                logger.warning(
                                    "服务端拒绝 max_tokens=%d，回退到 %d 重试", max_tokens, fallback
                                )
                                max_tokens = fallback
                                continue
                        raise LLMError(
                            f"HTTP {res.status}: {body_text[:300]}",
                            status=res.status,
                            retryable=retryable,
                        )
                    try:
                        data = parse_response_body(body_text)
                    except LLMError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise LLMError(f"响应解析失败: {exc}") from exc
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning("LLM 超时（第 %d 次）", attempt)
                await asyncio.sleep(1.5 * attempt)
                continue
            except aiohttp.ClientError as exc:
                last_error = exc
                logger.warning("LLM 网络错误（第 %d 次）: %s", attempt, exc)
                await asyncio.sleep(1.5 * attempt)
                continue

            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            finish = str(choice.get("finish_reason") or "")
            text, source = extract_message_text(message)
            usage = data.get("usage") or {}
            reasoning_tokens = ((usage.get("completion_tokens_details") or {}) or {}).get(
                "reasoning_tokens"
            )

            # 推理模型把 max_tokens 全花在「思考」上时，正文会是空的，必须加预算重试
            if finish == "length" and (source != "content" or not text):
                if attempt < 3:
                    new_max = min(max_tokens * 2, 65536)
                    logger.warning(
                        "输出被截断：finish=length source=%s max_tokens=%d reasoning_tokens=%s，"
                        "提升到 %d 重试",
                        source or "-", max_tokens, reasoning_tokens, new_max,
                    )
                    max_tokens = new_max
                    continue
                raise LLMError(
                    "模型输出被 Token 上限截断（finish_reason=length，正文为空）。"
                    f"当前 max_tokens={max_tokens}，reasoning_tokens={reasoning_tokens}。"
                    "请在 config.yaml 提高 llm.max_tokens（建议 ≥2048），"
                    "或通过 llm.extra_body 关闭思考模式。"
                )

            if not text:
                raise LLMError(
                    f"返回内容为空（finish={finish or '-'}, "
                    f"message 字段={list(message.keys())}, usage={usage}）"
                )

            if finish == "length":
                logger.warning("输出被截断但已有正文，按现有内容继续（%d 字）", len(text))
            if source != "content":
                logger.warning("模型未返回 content，使用 %s 兜底（%d 字）", source, len(text))

            logger.debug(
                "LLM 响应 finish=%s source=%s chars=%d usage=%s", finish, source, len(text), usage
            )
            return LLMResponse(
                text=text,
                model=data.get("model", self._cfg.llm.model),
                elapsed=time.time() - started,
                usage=usage,
                raw=data,
                finish_reason=finish,
                text_source=source,
            )

        raise LLMError(f"重试 3 次仍失败: {last_error}", retryable=True)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class OllamaProvider(BaseProvider):
    """本地 Ollama（可选），base_url 例：http://127.0.0.1:11434"""

    name = "ollama"

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def supports_vision(self) -> bool:
        return self._cfg.llm.vision

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._cfg.llm.timeout_sec)
            )
        return self._session

    async def complete(self, system: str, user: str, image_jpeg: Optional[bytes] = None) -> LLMResponse:
        url = self._cfg.llm.base_url.rstrip("/") + "/api/chat"
        message: Dict[str, Any] = {"role": "user", "content": user}
        if image_jpeg and self._cfg.llm.vision:
            message["images"] = [base64.b64encode(image_jpeg).decode("ascii")]
        payload = {
            "model": self._cfg.llm.model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            "format": "json",
            "options": {"temperature": self._cfg.llm.temperature},
        }
        session = await self._ensure_session()
        started = time.time()
        try:
            async with session.post(url, json=payload) as res:
                data = await res.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LLMError(f"Ollama 请求失败: {exc}", retryable=True) from exc
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError(f"Ollama 返回为空: {str(data)[:200]}")
        return LLMResponse(text=text, model=data.get("model", ""), elapsed=time.time() - started)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class MockProvider(BaseProvider):
    """离线联调用的假 Provider：按输入内容生成确定性回复。"""

    name = "mock"

    _CANDS = [
        "这波操作确实有点东西",
        "刚那句没听清，再说一遍呗",
        "笑死，主播这反应也太真实了",
        "画面里这个细节有人注意到吗",
        "这局感觉要打满了",
        "好家伙，节奏突然就起来了",
        "这波要是稳住了就赢一半了",
        "主播刚才那句话说得好有道理",
        "这阵容看着就很有想法",
        "别急，慢慢来还有机会",
        "这一幕我能笑一天",
        "解说这波分析挺到位的",
        "感觉对面已经在找机会了",
        "这手速真的练过吧",
        "怎么突然就打成这样了",
        "稳住，别浪，能赢",
    ]

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._rng = random.Random(42)

    @property
    def supports_vision(self) -> bool:
        return True

    async def complete(self, system: str, user: str, image_jpeg: Optional[bytes] = None) -> LLMResponse:
        await asyncio.sleep(0.05)
        # 一半概率 skip，保证 dry-run 里两种分支都能被覆盖到
        if self._rng.random() < 0.35:
            return LLMResponse(
                text=json.dumps(
                    {"action": "skip", "text": "", "reason": "mock: 此刻不适合插话", "confidence": 0.4},
                    ensure_ascii=False,
                ),
                model="mock",
                elapsed=0.05,
            )
        text = self._rng.choice(self._CANDS)
        return LLMResponse(
            text=json.dumps(
                {"action": "say", "text": text, "reason": "mock: 贴合当前内容", "confidence": 0.8},
                ensure_ascii=False,
            ),
            model="mock",
            elapsed=0.05,
        )


def create_provider(cfg: Config) -> BaseProvider:
    name = (cfg.llm.provider or "deepseek").lower()
    if name == "deepseek":
        return DeepSeekProvider(cfg)
    if name == "ollama":
        return OllamaProvider(cfg)
    if name == "mock":
        return MockProvider(cfg)
    raise ValueError(f"未知 LLM provider: {cfg.llm.provider}")
