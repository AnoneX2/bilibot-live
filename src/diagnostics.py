# -*- coding: utf-8 -*-
"""接口体检：把 TASK_PLAN 第 2 节的实测结论固化成可重复执行的检查。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

from .bili.api import BiliAPI, BiliAPIError
from .config import Config, mask_secret
from .util.log import get_logger

logger = get_logger("diagnostics")


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    elapsed_ms: float
    critical: bool = True
    """critical=False 表示这是「需要人处理但不阻塞核心链路」的项（如未登录）。"""


async def _timed(name: str, coro, critical: bool = True) -> Probe:
    started = time.time()
    try:
        detail = await coro
        return Probe(name, True, str(detail), (time.time() - started) * 1000, critical)
    except BiliAPIError as exc:
        return Probe(name, False, f"code={exc.code} {exc.message}",
                     (time.time() - started) * 1000, critical)
    except Exception as exc:  # noqa: BLE001
        return Probe(name, False, f"{type(exc).__name__}: {exc}",
                     (time.time() - started) * 1000, critical)


async def probe_interfaces(cfg: Config, api: BiliAPI, room: int) -> List[Probe]:
    """检查所有关键接口，返回逐项结果。"""
    return [
        await _timed("room_init 房间号解析", _probe_room_init(api, room)),
        await _timed("room_info 房间详情", _probe_room_info(api, room)),
        await _timed("playurl 播放地址", _probe_playurl(api, room)),
        await _timed("gethistory 弹幕历史", _probe_history(api, room), critical=False),
        await _timed("getDanmuInfo 弹幕服务器(WBI)", _probe_danmu_server(api, room)),
        await _timed("nav 登录态", _probe_nav(api), critical=False),
    ]


async def _probe_room_init(api: BiliAPI, room: int):
    info = await api.room_init(room)
    return f"room_id={info.room_id} live_status={info.live_status} uid={info.uid}"


async def _probe_room_info(api: BiliAPI, room: int):
    info = await api.room_info(room)
    if not info.title and not info.live_status and not info.uid:
        raise RuntimeError("返回空数据")
    return f"title={info.title[:24]} live={info.live_status} online={info.online}"


async def _probe_playurl(api: BiliAPI, room: int):
    candidates = await api.playurl(room)
    if not candidates:
        raise RuntimeError("无候选地址")
    best = candidates[0]
    kinds = sorted({f"{c.protocol}/{c.format}/{c.codec}" for c in candidates})
    return f"{len(candidates)} 个地址，例如 {best.protocol}/{best.format}/{best.codec}；类型={kinds}"


async def _probe_history(api: BiliAPI, room: int):
    items = await api.danmaku_history(room)
    sample = items[-1].text[:20] if items else "(空)"
    return f"{len(items)} 条，最后一条={sample}"


async def _probe_danmu_server(api: BiliAPI, room: int):
    data = await api.danmaku_server(room)
    hosts = data.get("host_list") or []
    if not hosts:
        raise RuntimeError("host_list 为空")
    return f"{len(hosts)} 个节点，token={'有' if data.get('token') else '无'}"


async def _probe_nav(api: BiliAPI):
    from .bili.auth import check_login

    is_login, uname, mid = await check_login(api.session)
    return f"isLogin={is_login} uname={uname or '-'} mid={mid}"


def check_asr_assets(cfg: Config) -> Probe:
    started = time.time()
    from .asr.xasr_engine import resolve_model_dir

    try:
        model_dir = resolve_model_dir(cfg)
    except Exception as exc:  # noqa: BLE001
        return Probe("ASR 模型文件(X-ASR)", False, str(exc), 0, critical=False)
    total = sum(p.stat().st_size for p in model_dir.rglob("*") if p.is_file())
    return Probe(
        "ASR 模型文件(X-ASR)",
        True,
        f"{model_dir.name} 共 {total / 1024 / 1024:.0f} MB · 模式={cfg.asr.mode} · {model_dir}",
        (time.time() - started) * 1000,
        critical=False,
    )


def check_llm_config(cfg: Config) -> Probe:
    if cfg.llm.provider == "mock":
        return Probe("LLM 配置", True, "provider=mock（离线联调）", 0, critical=False)
    key = cfg.api_key()
    if not key:
        return Probe(
            "LLM 配置",
            False,
            f"缺少 API Key：请在设置面板「大模型 → API Key」填写并保存"
            f"（模型={cfg.llm.model} base={cfg.llm.base_url}）",
            0,
            critical=False,
        )
    return Probe(
        "LLM 配置",
        True,
        f"provider={cfg.llm.provider} model={cfg.llm.model} "
        f"key={mask_secret(key)}（config.yaml）vision={cfg.llm.vision}",
        0,
        critical=False,
    )


def check_prompts(cfg: Config, prompt_dir: str = "") -> Probe:
    from .ai.prompt_store import PromptError, PromptStore

    try:
        store = PromptStore(cfg, prompt_dir=prompt_dir)
    except PromptError as exc:
        return Probe("提示词/人设", False, str(exc), 0, critical=False)
    errors = store.validate()
    if errors:
        return Probe("提示词/人设", False, "；".join(errors), 0, critical=False)
    return Probe(
        "提示词/人设",
        True,
        f"{store.profile_label} · {store.system_path} / {store.user_path}",
        0,
        critical=False,
    )


def check_cookies(cfg: Config) -> Probe:
    path = cfg.cookie_path
    if not path.exists():
        return Probe("登录 cookie", False, f"未找到 {path}，请先执行 login", 0, critical=False)
    return Probe("登录 cookie", True, f"{path}（{path.stat().st_size} bytes）", 0, critical=False)
