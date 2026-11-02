# -*- coding: utf-8 -*-
"""本地设置面板服务。

只监听 127.0.0.1，提供：
  GET  /                     设置页面
  GET  /api/schema           字段元数据（中文标签/说明/范围）
  GET  /api/config           当前配置 + 校验结果
  POST /api/config           保存配置（写回 config.yaml，自动备份）
  POST /api/rooms/resolve    根据房间号查询主播/标题/开播状态
  GET  /api/prompts          读取 system.md / user.md
  POST /api/prompts          保存提示词（写前校验占位符）
  POST /api/prompts/create   新建人设（单文件或目录）
  GET  /api/prompt/preview   用示例数据渲染实际 Prompt
  POST /api/check            登录/接口/提示词体检
  GET  /api/status           运行状态与日志尾部
  GET  /api/decisions        决策历史（按房间 + 场次）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import yaml
from aiohttp import web

from ..ai.prompt_store import (
    PLACEHOLDER_RE,
    SYSTEM_VARS,
    USER_VARS,
    PromptError,
    PromptStore,
    discover_profiles,
    format_danmaku,
    format_timeline,
    format_transcript,
)
from ..bili.api import BiliAPI, BiliAPIError
from ..bili.auth import check_login, load_cookies
from ..bili.session import create_session
from ..config import PROJECT_ROOT, Config, build_config, load_config, mask_secret, sanitize_raw
from ..config_writer import save_config
from ..reload import read_state as read_reload_state
from ..retention import session_file_start
from ..runtime import pid_alive, read_runtime
from ..settings_schema import schema_dict
from .bot_manager import BotProcess
from .login_manager import LoginManager
from ..util.log import get_logger

logger = get_logger("panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH_KEY = web.AppKey("config_path", object)
BOT_KEY = web.AppKey("bot", object)
LOGIN_KEY = web.AppKey("login", object)


# ---------------------------------------------------------------- 工具


def _config_path(request: web.Request) -> Path:
    raw = request.app[CONFIG_PATH_KEY]
    return Path(raw) if raw else PROJECT_ROOT / "config.yaml"


def _read_raw(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_file_config(path: Path) -> Config:
    clean, _dropped = sanitize_raw(_read_raw(path))
    return build_config(clean)


# 面板永远不接收/返回明文 Key：留空或回传掩码都表示「保持原值」
CLEAR_SENTINEL = "__CLEAR__"


def _stored_api_key(path: Path) -> str:
    """读出配置文件里已保存的 API Key（读不到就当作空）。"""
    try:
        return (_load_file_config(path).llm.api_key or "").strip()
    except (FileNotFoundError, ValueError, OSError):
        return ""


def _resolve_api_key(path: Path, submitted: Any) -> str:
    """把面板提交的 api_key 解析成要落盘的值。

    * 空 / 掩码（sk-abc…wxyz）/ 未提交  -> 沿用配置文件里的旧值
    * __CLEAR__                        -> 清空
    * 其他                             -> 当作新 Key
    """
    existing = _stored_api_key(path)
    text = "" if submitted is None else str(submitted).strip()
    if not text or text == mask_secret(existing):
        return existing
    if text == CLEAR_SENTINEL:
        return ""
    return text


def _tail_lines(path: Path, limit: int, max_bytes: int = 256 * 1024) -> List[str]:
    """只读文件尾部再切行：bot.log 可以有 8MB，整份读会把事件循环拖住。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 第一行可能是被截断的半行
    return lines[-limit:]


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _ok(**kwargs) -> web.Response:
    return _json({"ok": True, **kwargs})


def _fail(message: str, status: int = 400, **kwargs) -> web.Response:
    return _json({"ok": False, "message": message, **kwargs}, status=status)


def _prompt_store(cfg: Config, spec: str = "", room_id: int = 0) -> PromptStore:
    store = PromptStore(cfg, prompt_dir=spec, room_id=room_id)
    resolved = store.system_path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise PromptError("人设文件必须在项目目录内")
    return store


async def _read_json(request: web.Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text=f"请求体不是 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="请求体必须是对象")
    return data


# ---------------------------------------------------------------- 面板状态


def _ui_state_path(cfg: Config) -> Path:
    return cfg.data_path / "panel_state.json"


def _read_ui_state(cfg: Config) -> Dict[str, Any]:
    try:
        data = json.loads(_ui_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ui_state(cfg: Config, **updates: Any) -> None:
    """面板自己的小状态（例如上次选中的人设），不影响 config.yaml。"""
    path = _ui_state_path(cfg)
    data = _read_ui_state(cfg)
    data.update(updates)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("写入面板状态失败: %s", path)


# ---------------------------------------------------------------- 页面


async def index(request: web.Request) -> web.StreamResponse:
    # 给静态资源加版本号：面板升级后浏览器不会继续用缓存的旧 app.js
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        try:
            version = int((STATIC_DIR / name).stat().st_mtime)
        except OSError:
            version = 0
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- 配置


async def get_schema(request: web.Request) -> web.Response:
    path = _config_path(request)
    return _ok(
        schema=schema_dict(),
        config_path=str(path),
        prompts_dir=str(PROJECT_ROOT / "prompts"),
        project_root=str(PROJECT_ROOT),
    )


async def get_config(request: web.Request) -> web.Response:
    path = _config_path(request)
    try:
        clean, dropped = sanitize_raw(_read_raw(path))
        cfg = build_config(clean)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    errors = cfg.validate()
    warnings = [f"已忽略未知/废弃配置项：{', '.join(dropped)}"] if dropped else []
    return _ok(
        config=cfg.to_dict(mask_secrets=True),
        errors=errors,
        warnings=warnings,
        config_path=str(path),
        api_key_set=bool(cfg.api_key()),
        api_key_masked=mask_secret(cfg.api_key()),
        runtime=_runtime_snapshot(cfg, path),
        reload=read_reload_state(cfg),
        env_overrides=[
            name
            for name in ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                         "BOT_ROOMS", "BOT_DRY_RUN", "BOT_LOG_LEVEL")
            if os.environ.get(name)
        ],
    )


def _runtime_snapshot(cfg: Config, path: Path) -> Dict[str, Any] | None:
    runtime = read_runtime(cfg)
    if not runtime:
        return None
    runtime = dict(runtime)
    runtime["alive"] = pid_alive(int(runtime.get("pid") or 0))
    runtime["age_sec"] = max(0.0, time.time() - float(runtime.get("started_at") or 0))
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    started_mtime = float(runtime.get("config_mtime") or 0)
    # 只有 BOT 还活着，「配置已改、待重载」才有意义；进程已经停了就是「下次启动生效」
    runtime["config_changed"] = bool(
        runtime.get("alive") and started_mtime and current_mtime > started_mtime + 0.001
    )
    runtime["mode_changed"] = bool(runtime.get("dry_run")) != bool(cfg.app.dry_run)
    return runtime


async def save_config_handler(request: web.Request) -> web.Response:
    payload = await _read_json(request)
    data = payload.get("config")
    if not isinstance(data, dict):
        return _fail("缺少 config 对象")

    path = _config_path(request)
    # API Key：面板不持有明文，空值/掩码表示沿用旧值，避免「保存一次就把 Key 抹掉」
    if isinstance(data.get("llm"), dict):
        submitted = dict(data["llm"])
        submitted["api_key"] = _resolve_api_key(path, submitted.get("api_key"))
        data = {**data, "llm": submitted}
    try:
        cfg = build_config(data)
    except (ValueError, TypeError) as exc:
        return _fail(f"配置格式错误: {exc}")

    errors = cfg.validate()
    if errors:
        return _fail("配置校验未通过", errors=errors)

    # 人设必须可用，否则 BOT 重启会直接失败
    prompt_errors: List[str] = []
    for room in cfg.app.rooms:
        try:
            store = PromptStore(cfg, prompt_dir=room.prompt_dir, room_id=room.room_id)
        except PromptError as exc:
            prompt_errors.append(f"{room.display()}: {exc}")
            continue
        prompt_errors.extend(f"{room.display()}: {err}" for err in store.validate())
    if prompt_errors:
        return _fail("人设/提示词不可用，已取消保存", errors=prompt_errors)

    backup = save_config(cfg, path)
    logger.info("设置已保存到 %s（备份=%s）", path, backup)
    runtime = _runtime_snapshot(cfg, path)
    if runtime and runtime.get("alive") and runtime.get("auto_reload"):
        message = "已保存，BOT 会在 1–2 秒内自动重启并加载新配置"
    elif runtime and runtime.get("alive"):
        message = "已保存，重启 BOT 后生效"
    else:
        message = "已保存，下次启动 BOT 时生效"
    return _ok(
        message=message,
        backup=str(backup) if backup else None,
        warnings=[],
        config=cfg.to_dict(mask_secrets=True),
        api_key_set=bool(cfg.api_key()),
        api_key_masked=mask_secret(cfg.api_key()),
        auto_reload=bool(runtime and runtime.get("auto_reload")),
    )


async def resolve_room(request: web.Request) -> web.Response:
    payload = await _read_json(request)
    raw = str(payload.get("room_id", "")).strip()
    if not raw.isdigit():
        return _fail("房间号必须是数字")
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    session = await create_session(cfg, load_cookies(cfg.cookie_path))
    try:
        api = BiliAPI(session, cfg)
        init = await api.room_init(int(raw))
        info = await api.room_info(init.room_id)
        return _ok(
            room={
                "room_id": init.room_id,
                "uid": init.uid,
                "live_status": init.live_status,
                "title": info.title,
                "uname": info.uname,
                "online": info.online,
                "area": info.area_name,
            }
        )
    except BiliAPIError as exc:
        return _fail(f"查询失败: code={exc.code} {exc.message}", status=400)
    finally:
        await session.close()


# ---------------------------------------------------------------- 提示词


async def list_profiles(request: web.Request) -> web.Response:
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    base = cfg.path(cfg.llm.prompt_dir)
    profiles = [
        {
            "id": p.id,
            "label": p.label,
            "kind": p.kind,
            "system": p.system_path,
            "user": p.user_path,
        }
        for p in discover_profiles(base)
    ]
    return _ok(profiles=profiles, base=str(base))


async def get_prompts(request: web.Request) -> web.Response:
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    explicit = "dir" in request.query
    directory = request.query.get("dir") or ""
    if not explicit:
        # 网页刷新/重开时沿用上次选中的人设
        directory = str(_read_ui_state(cfg).get("prompt_dir") or "")
    try:
        store = _prompt_store(cfg, directory)
    except PromptError as exc:
        if explicit:
            return _fail(str(exc))
        # 上次的人设被删了：回退默认，不让面板打不开
        logger.info("上次选中的人设已不可用（%s），回退默认人设", directory)
        _write_ui_state(cfg, prompt_dir="")
        try:
            store = _prompt_store(cfg, "")
        except PromptError as exc2:
            return _fail(str(exc2))
    errors = store.validate()
    if errors:
        return _fail("提示词文件有问题", errors=errors, dir=store.profile)
    return _ok(
        dir=store.profile,
        kind=store.kind,
        base=str(store.base_dir),
        system=store.raw("system"),
        user=store.raw("user"),
        user_shared=(store.kind == "file"),
        errors=[],
    )


def _validate_placeholders(text: str, known: set[str]) -> List[str]:
    unknown = {m.group(1) for m in PLACEHOLDER_RE.finditer(text)} - known
    errors = []
    if unknown:
        errors.append(f"未知占位符: {sorted(unknown)}")
    if "{{" in PLACEHOLDER_RE.sub("", text):
        errors.append("存在没有闭合的 {{")
    return errors


async def save_ui_state(request: web.Request) -> web.Response:
    payload = await _read_json(request)
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    prompt_dir = payload.get("prompt_dir")
    if isinstance(prompt_dir, str):
        try:
            store = _prompt_store(cfg, prompt_dir)
            _write_ui_state(cfg, prompt_dir=store.profile)
        except PromptError:
            _write_ui_state(cfg, prompt_dir="")
    return _ok(state=_read_ui_state(cfg))


async def save_prompts(request: web.Request) -> web.Response:
    payload = await _read_json(request)
    system = payload.get("system")
    user = payload.get("user")
    if not isinstance(system, str) or not isinstance(user, str):
        return _fail("system/user 必须是字符串")
    if not system.strip():
        return _fail("system.md 不能为空")

    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    directory = payload.get("dir") or ""
    try:
        store = _prompt_store(cfg, directory)
    except PromptError as exc:
        return _fail(str(exc))

    errors = _validate_placeholders(system, SYSTEM_VARS)
    if store.kind == "file":
        user_vars = (
            {m.group(1) for m in PLACEHOLDER_RE.finditer(system)} & USER_VARS
        ) - SYSTEM_VARS
        if user_vars:
            errors.append(
                f"单文件人设只能包含 system 变量，动态内容请写 prompts/user.md（发现 {sorted(user_vars)}）"
            )
    else:
        if not user.strip():
            return _fail("user.md 不能为空")
        errors += _validate_placeholders(user, USER_VARS)
    if errors:
        return _fail("提示词校验未通过", errors=errors)

    system_path = store.system_path
    if store.kind == "dir":
        user_path = system_path.parent / "user.md"
    else:
        user_path = store.user_path

    backup_dir = store.base_dir / ".backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets = [(system_path, system)]
    if store.kind != "file":
        targets.append((user_path, user))
    for target, text in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            (backup_dir / f"{stamp}-{target.name}").write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
        target.write_text(text, encoding="utf-8")
    _write_ui_state(cfg, prompt_dir=store.profile)
    logger.info("提示词已保存: %s", ", ".join(str(t) for t, _ in targets))
    message = (
        f"已保存单文件人设 {system_path.name}（user 模板共用 prompts/user.md）"
        if store.kind == "file"
        else "提示词已保存，运行中的 BOT 会自动重新加载"
    )
    return _ok(message=message, dir=store.profile, kind=store.kind)


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,32}$")
PROFILE_RESERVED = {"system", "user", "readme", "system.md", "user.md", "readme.md", ".backup"}

NEW_PROFILE_SYSTEM = """# 人设：{name}

你是中文直播间里的一名观众式助手，只在合适的时候接一句自然、口语化的话。

【风格】
- 短句，不超过 {{max_chars}} 字
- 不解释、不总结、不客套
- 不复述弹幕，不回复观众

【可以补充】
- 这个角色的性格 / 口癖：
- 擅长或感兴趣的话题：
- 不想聊的话题：
"""


async def create_prompt_profile(request: web.Request) -> web.Response:
    payload = await _read_json(request)
    name = str(payload.get("name") or "").strip()
    kind = str(payload.get("kind") or "file").lower()
    copy_existing = bool(payload.get("copy", True))
    source_spec = str(payload.get("copy_from") or "")

    if kind not in ("file", "dir"):
        return _fail("类型只能是 file（单文件）或 dir（目录）")
    if not PROFILE_NAME_RE.fullmatch(name):
        return _fail("人设名只能包含中文、字母、数字、下划线、短横线（1–32 个字符）")
    lowered = name.lower()
    if (lowered in PROFILE_RESERVED or lowered.startswith("system")
            or name.startswith(".")):
        return _fail(f"「{name}」是保留名称，换一个吧")

    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    base = cfg.path(cfg.llm.prompt_dir)
    target_id = f"{name}.md" if kind == "file" else name
    target = base / target_id
    if target.exists():
        return _fail(f"「{target_id}」已存在，请换个名字")

    source_system = ""
    source_user = ""
    if copy_existing:
        try:
            source = _prompt_store(cfg, source_spec)
            source_system = source.raw("system")
            source_user = source.raw("user")
        except PromptError as exc:
            return _fail(f"复制当前人设失败：{exc}")
    if not source_system:
        source_system = NEW_PROFILE_SYSTEM.replace("{name}", name)
    if not source_user:
        try:
            source_user = (base / "user.md").read_text(encoding="utf-8")
        except OSError:
            return _fail("缺少 prompts/user.md，无法创建人设")

    created: List[Path] = []
    try:
        if kind == "file":
            target.write_text(source_system, encoding="utf-8")
            created.append(target)
        else:
            target.mkdir(parents=True)
            (target / "system.md").write_text(source_system, encoding="utf-8")
            (target / "user.md").write_text(source_user, encoding="utf-8")
            created.append(target)
        store = _prompt_store(cfg, target_id)
        errors = store.validate()
        if errors:
            raise PromptError("；".join(errors))
    except (PromptError, OSError) as exc:
        for item in created:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        return _fail(f"创建失败：{exc}")

    logger.info("新建人设: %s", target_id)
    return _ok(message=f"已创建人设 {target_id}", id=target_id, kind=kind)


async def preview_prompt(request: web.Request) -> web.Response:
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    room_id = request.query.get("room_id", "")
    room = next((r for r in cfg.app.rooms if str(r.room_id) == room_id), None)
    spec = request.query.get("dir") or (room.prompt_dir if room else "") or ""
    alias_param = (request.query.get("alias") or "").strip()
    try:
        store = _prompt_store(cfg, spec, room_id=room.room_id if room else 0)
    except PromptError as exc:
        return _fail(str(exc))
    errors = store.validate()
    if errors:
        return _fail("提示词文件有问题", errors=errors)

    now = time.time()
    transcript = [
        (now - 12, "这波我们先推线，等他们过来再打"),
        (now - 4, "对面这个打野一直蹲下路，得小心一点"),
    ]
    danmaku = ["观众A: 这波操作可以啊", "观众B: 感觉要翻盘了"]
    timeline = sorted(
        [(ts, "speech", text) for ts, text in transcript]
        + [(now - 30, "danmaku", line) for line in danmaku],
        key=lambda item: item[0],
    )
    # 还没保存的新房间：用前端传来的别名渲染，避免预览里没有房间信息
    alias = (room.alias if room else "") or alias_param
    return _ok(
        system=store.system(),
        user=store.user(
            title=alias or "示例直播间",
            alias=alias,
            bot_name="示例机器人",
            # 预览里 title 已经用了别名，避免显示成「别名（别名）」
            alias_part="",
            clock=time.strftime("%H:%M:%S", time.localtime(now)),
            frame_desc="已附带一帧直播截图（约 3 秒前），可参考画面内容。",
            timeline=format_timeline(timeline, 800),
            transcript=format_transcript(transcript, now, 700),
            danmaku=format_danmaku(danmaku, 300),
            trigger="主播刚说了：「对面打野一直蹲下路」，请判断要不要接一句",
            room_id=room.room_id if room else 0,
        ),
        dir=store.profile,
        kind=store.kind,
        user_shared=(store.kind == "file"),
    )


# ---------------------------------------------------------------- 体检


async def run_check(request: web.Request) -> web.Response:
    from ..diagnostics import probe_interfaces

    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    results: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    add("配置文件", True, str(path))
    add("API Key", bool(cfg.api_key()),
        "已保存到 config.yaml" if cfg.api_key() else "未设置：请在「大模型 → API Key」填写")
    add("登录 cookie", cfg.cookie_path.exists(), str(cfg.cookie_path))

    for room in cfg.app.rooms:
        try:
            store = _prompt_store(cfg, room.prompt_dir, room.room_id)
        except PromptError as exc:
            add(f"提示词 {room.display()}", False, str(exc))
            continue
        errs = store.validate()
        detail = "；".join(errs) if errs else f"{store.profile_label}（{store.system_path}）"
        add(f"提示词 {room.display()}", not errs, detail)

    active = cfg.active_rooms()
    if not active:
        add("直播间", False, "没有启用的房间")
        return _ok(results=results)

    room = active[0]
    session = await create_session(cfg, load_cookies(cfg.cookie_path))
    try:
        is_login, uname, mid = await check_login(session)
        add("登录态", is_login, f"{uname or '-'} (mid={mid})" if is_login else "未登录，无法发弹幕")
        api = BiliAPI(session, cfg)
        for probe in await probe_interfaces(cfg, api, room.room_id):
            add(probe.name, probe.ok, probe.detail)
    except Exception as exc:  # noqa: BLE001
        add("接口体检", False, f"{type(exc).__name__}: {exc}")
    finally:
        await session.close()
    return _ok(results=results)


async def get_status(request: web.Request) -> web.Response:
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    log_tail: List[str] = _tail_lines(cfg.log_path / "bot.log", 30, 64 * 1024)
    try:
        # systemctl/psutil 是阻塞调用，放线程里，别卡住面板的请求循环
        bot_state = await asyncio.to_thread(_bot_manager(request).status)
    except Exception:  # noqa: BLE001 - 配置坏了也要能打开状态页
        bot_state = {"running": False, "pid": 0, "source": "", "uptime_sec": 0.0}
    return _ok(log_tail=log_tail, runtime=_runtime_snapshot(cfg, path),
               reload=read_reload_state(cfg), bot_running=_bot_running(), bot=bot_state,
               cookie_saved=cfg.cookie_path.exists(), data_dir=str(cfg.data_path))


# ---------------------------------------------------------------- 决策历史


# 旧记录没有场次标记时，两条决策间隔超过这么久就当作换了场（只影响历史浏览）
SESSION_GAP_SEC = 1800.0
# 一场直播不会比这更长：用它判断某份存档是否可能还包含 since 之后的记录
MAX_SESSION_SPAN_SEC = 24 * 3600.0
# 决策记录里用不到的字段（每次判断都带的上下文尾巴），列表页不需要，去掉省流量
_DECISION_FIELDS = (
    "ts", "trigger", "focus", "action", "text", "confidence",
    "reason", "sent", "dry_run", "error", "elapsed",
)


def _read_decisions(path: Path) -> List[Dict[str, Any]]:
    """读一整个 jsonl（历史记录全读，坏行跳过），按时间升序。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    items: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("ts"):
            items.append(record)
    items.sort(key=lambda r: r.get("ts") or 0)
    return items


def _session_windows(cfg: Config, room_id: str, since: float = 0.0) -> List[tuple]:
    """每场直播结束时写的存档带起止时间，用它给老记录划分场次。

    `since` 是需要覆盖到的最早记录时间。存档文件名里就有场次开始时间，
    开始得比 `since` 还早一整天以上的存档不可能包含要显示的记录，直接跳过
    不读——存档攒到几万份时也只是 glob 一遍文件名。
    """
    windows: List[tuple] = []
    try:
        files = list((cfg.data_path / "sessions").glob(f"{room_id}-*.json"))
    except OSError:
        return windows
    for file in files:
        named_start = session_file_start(file.name)
        if since and named_start is not None and named_start + MAX_SESSION_SPAN_SEC < since:
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        started = float(data.get("started_at") or 0)
        ended = float(data.get("ended_at") or started)
        if started:
            windows.append((started, ended))
    windows.sort()
    return windows


def _build_sessions(
    records: List[Dict[str, Any]], windows: List[tuple]
) -> tuple[List[Dict[str, Any]], Dict[int, List[Dict[str, Any]]]]:
    """把决策记录按「场」分组。

    优先级：记录自带的 session_started_at（新数据）→ 场次存档的时间窗 →
    按时间间隔切块（老数据、以及正在进行、还没存档的这一场）。按时间顺序扫，
    遇到「正式场次」时把紧挨着的临时组并进来，避免升级那一刻把一场劈成两半。
    """
    ordered = sorted(records, key=lambda r: float(r.get("ts") or 0))
    groups: Dict[int, List[Dict[str, Any]]] = {}
    gap_keys: set = set()
    cur_key: Optional[int] = None
    prev_ts = 0.0

    for record in ordered:
        ts = float(record.get("ts") or 0)
        key: Optional[int] = None
        started = record.get("session_started_at")
        if started:
            try:
                key = int(float(started))
            except (TypeError, ValueError):
                key = None
        if key is None:
            # 容差只留 2 秒：留大了会把上/下一场的记录吸进来（相邻两场常只差几秒）
            matched = next((w for w in windows if w[0] - 2 <= ts <= w[1] + 2), None)
            if matched is not None:
                key = int(matched[0])

        if key is not None:
            # 老记录没有场次标记，先被切成了临时组：并到真正的这一场里
            if (cur_key is not None and cur_key in gap_keys and cur_key != key
                    and ts - prev_ts <= SESSION_GAP_SEC):
                groups.setdefault(key, []).extend(groups.pop(cur_key))
                gap_keys.discard(cur_key)
            groups.setdefault(key, []).append(record)
            cur_key = key
        else:
            if cur_key is None or ts - prev_ts > SESSION_GAP_SEC:
                new_key = int(ts) or 1
                while new_key in groups:  # 极端情况：和存档场次撞了键，挪一秒
                    new_key += 1
                groups.setdefault(new_key, [])
                gap_keys.add(new_key)
                cur_key = new_key
            groups[cur_key].append(record)
        prev_ts = ts

    sessions = []
    for key, items in groups.items():
        stamps = [float(r.get("ts") or 0) for r in items]
        first = min(stamps) if stamps else float(key)
        sessions.append({
            "key": str(key),
            # 有正式场次时 key 就是这场开播/开始记录的时刻，标签按它显示更准
            "start": min(first, float(key)),
            "end": max(stamps) if stamps else float(key),
            "count": len(items),
        })
    sessions.sort(key=lambda s: s["end"], reverse=True)
    return sessions, groups


async def get_decisions(request: web.Request) -> web.Response:
    """决策历史：按房间 + 场次浏览（默认最近一场）。"""
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    decision_dir = cfg.data_path / "decisions"
    files: Dict[str, Path] = {}
    if decision_dir.exists():
        for file in decision_dir.glob("*.jsonl"):
            files[file.stem] = file
    if not files:
        return _ok(rooms=[], sessions=[], items=[], room="", session="", total=0)

    aliases = {str(room.room_id): room.alias for room in cfg.app.rooms}
    rooms = [
        {"room": room_id, "alias": aliases.get(room_id, "")}
        for room_id in sorted(files)
    ]

    wanted = request.query.get("room") or ""
    if wanted not in files:
        # 默认挑最近有记录的房间
        def _mtime(room_id: str) -> float:
            try:
                return files[room_id].stat().st_mtime
            except OSError:
                return 0.0

        wanted = max(files, key=_mtime)

    # 读文件是阻塞的，放线程里：历史记录可能攒到很大
    records = await asyncio.to_thread(_read_decisions, files[wanted])
    # 最老的记录对应的场次才有意义，比它早一整天的存档不用读（见 _session_windows）
    since = float(records[0].get("ts") or 0) if records else 0.0
    windows = await asyncio.to_thread(_session_windows, cfg, wanted, since) if records else []
    sessions, groups = _build_sessions(records, windows)

    try:
        picked_key = int(request.query.get("session") or "")
    except ValueError:
        picked_key = None
    if picked_key not in groups:
        picked_key = int(sessions[0]["key"]) if sessions else None

    items = sorted(
        groups.get(picked_key, []) if picked_key is not None else [],
        key=lambda r: r.get("ts") or 0,
        reverse=True,
    )
    slim = [{key: record.get(key) for key in _DECISION_FIELDS} for record in items]
    return _ok(
        rooms=rooms,
        room=wanted,
        alias=aliases.get(wanted, ""),
        sessions=sessions,
        session=str(picked_key) if picked_key is not None else "",
        items=slim,
        total=len(slim),
    )



# ---------------------------------------------------------------- 面板控制


def _bot_manager(request: web.Request) -> BotProcess:
    return request.app[BOT_KEY]


def _login_manager(request: web.Request) -> LoginManager:
    return request.app[LOGIN_KEY]


async def bot_control(request: web.Request) -> web.Response:
    """启动 / 停止 / 重启 BOT（POST /api/bot/{start|stop|restart}）。"""
    action = request.path.rstrip("/").rsplit("/", 1)[-1]
    if action not in ("start", "stop", "restart"):
        return _fail(f"未知操作: {action}")
    bot = _bot_manager(request)
    # 启动前先校验配置：否则 systemd 会「启动→退出→5 秒后重试」循环，
    # 连续 10 次后直接进 failed 状态，比报错难恢复得多。
    if action in ("start", "restart"):
        try:
            cfg = load_config(str(_config_path(request)))
        except ValueError as exc:
            return _fail(f"配置不完整，先修好再启动：{exc}")
        if cfg.llm.provider == "deepseek" and not cfg.api_key():
            return _fail("还没填 API Key：先在「大模型 → API Key」里保存，再启动 BOT")
    try:
        fn = {"start": bot.start, "stop": bot.stop, "restart": bot.restart}[action]
        # 启停要等进程收尾（最多十几秒），放到线程里避免阻塞面板
        result = await asyncio.to_thread(fn)
    except ValueError as exc:
        return _fail(f"配置有问题，先修好再启停：{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("BOT %s 失败", action)
        return _fail(f"{action} 失败：{type(exc).__name__}: {exc}")
    status = bot.status()
    return _ok(action=action, bot=status, result=result,
               message=result.get("message", ""), log_tail=result.get("log_tail"))


async def login_start(request: web.Request) -> web.Response:
    """申请登录二维码（POST /api/login/start）。"""
    try:
        payload = await _login_manager(request).start()
    except ValueError as exc:
        return _fail(f"配置有问题：{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("申请登录二维码失败")
        return _fail(f"申请二维码失败：{type(exc).__name__}: {exc}")
    return _ok(**payload)


async def login_status(request: web.Request) -> web.Response:
    payload = await _login_manager(request).status()
    if payload.get("state") == "ok":
        _set_login_cache(True, payload.get("uname") or "", int(payload.get("mid") or 0))
    return _ok(**payload)


async def login_cancel(request: web.Request) -> web.Response:
    return _ok(**(await _login_manager(request).cancel()))


# 登录态查询有网络开销：缓存 30 秒，避免状态轮询把 nav 接口打爆
_LOGIN_CACHE: Dict[str, Any] = {"at": 0.0, "ok": False, "uname": "", "mid": 0}
LOGIN_CACHE_TTL = 30.0


def _set_login_cache(ok: bool, uname: str, mid: int) -> None:
    _LOGIN_CACHE.update({"at": time.time(), "ok": bool(ok), "uname": uname or "", "mid": int(mid or 0)})


async def login_whoami(request: web.Request) -> web.Response:
    """当前登录的是哪个账号（面板「B 站登录」那一行显示用）。"""
    now = time.time()
    if now - float(_LOGIN_CACHE.get("at") or 0) < LOGIN_CACHE_TTL:
        return _ok(logged_in=_LOGIN_CACHE["ok"], uname=_LOGIN_CACHE["uname"], mid=_LOGIN_CACHE["mid"])
    try:
        cfg = _load_file_config(_config_path(request))
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    session = await create_session(cfg, load_cookies(cfg.cookie_path))
    try:
        ok, uname, mid = await check_login(session)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"查询登录态失败：{type(exc).__name__}: {exc}")
    finally:
        await session.close()
    _set_login_cache(ok, uname, mid)
    return _ok(logged_in=ok, uname=uname, mid=mid)


async def save_llm_key(request: web.Request) -> web.Response:
    """只保存 API Key（面板里那个「保存 Key」按钮），不用连带着保存整份配置。"""
    payload = await _read_json(request)
    key = str(payload.get("api_key") or "").strip()
    if not key:
        return _fail("Key 为空：请先填入 API Key")
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    cfg.llm.api_key = key
    try:
        save_config(cfg, path)
    except OSError as exc:
        return _fail(f"写入配置失败：{exc}", status=500)
    logger.info("API Key 已更新（%s）", mask_secret(key))
    return _ok(api_key_masked=mask_secret(key), message="API Key 已保存")


async def test_llm(request: web.Request) -> web.Response:
    """用当前填的（或已保存的）Key 发一次最小请求，测「接口地址 + Key + 模型名」是否可用。

    故意不走 provider.complete：它会带上 response_format=json_object、thinking 等参数，
    连通性测试不需要这些，反而容易被网关的特殊行为干扰。
    """
    payload = await _read_json(request)
    path = _config_path(request)
    try:
        cfg = _load_file_config(path)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)
    key = str(payload.get("api_key") or "").strip() or cfg.api_key()
    if not key:
        return _fail("还没有可用的 API Key：先填入再测试")

    url = cfg.llm.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.llm.model,
        "messages": [{"role": "user", "content": "只回复 OK 两个字符。"}],
        # 推理型模型会先「思考」，预算太小会出现 finish=length、正文为空
        "max_tokens": 512,
        "temperature": 0,
    }
    started = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(
                url, json=body, headers={"Authorization": f"Bearer {key}"}
            ) as res:
                status = res.status
                text = await res.text()
    except asyncio.TimeoutError:
        return _fail("调用超时（30 秒）：检查接口地址与网络")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"请求失败：{type(exc).__name__}: {exc}")
    elapsed = time.time() - started

    if status != 200:
        return _fail(f"接口返回 HTTP {status}：{text[:200]}")
    try:
        data = json.loads(text)
    except ValueError:
        return _fail(f"返回不是 JSON：{text[:120]}")

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    reply = str(message.get("content") or "").strip()
    if not reply:
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        return _fail(
            f"接口能连通，但正文为空（finish={choice.get('finish_reason')}，"
            f"reasoning {len(str(reasoning))} 字符）：检查模型名或「思考模式」设置"
        )
    logger.info("模型连通性测试通过：%s %.2fs", cfg.llm.model, elapsed)
    return _ok(model=cfg.llm.model, elapsed=round(elapsed, 2), reply=reply[:40])


_LOG_SOURCES = {
    "bot": "bot.log",              # logging + print/stderr（面板「实时日志」）
    "panel": "panel.log",          # 面板自己的日志
    "console": "bot.console.log",  # BOT 子进程原始输出（崩溃前的兜底）
}


async def get_logs(request: web.Request) -> web.Response:
    """读取日志尾部：/api/logs?lines=200&source=bot"""
    try:
        lines = int(request.query.get("lines", "200"))
    except ValueError:
        lines = 200
    lines = max(20, min(lines, 2000))
    source = request.query.get("source", "bot")
    if source not in _LOG_SOURCES:
        return _fail(f"未知日志来源: {source}")
    try:
        cfg = _load_file_config(_config_path(request))
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc), status=500)

    path = cfg.log_path / _LOG_SOURCES[source]
    if not path.exists():
        return _ok(lines=[], path=str(path), size=0, mtime=0.0, source=source)
    try:
        stat = path.stat()
    except OSError as exc:
        return _fail(f"读取日志失败: {exc}", status=500)
    tail = _tail_lines(path, lines, max(64 * 1024, lines * 300))
    return _ok(lines=tail, path=str(path), size=stat.st_size,
               mtime=stat.st_mtime, source=source)


def _bot_running() -> bool:
    """通过进程标题粗略判断 BOT 是否在跑（Windows/Linux 都可用 psutil）。"""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "src.app" in cmdline and "run" in cmdline:
            return True
    return False


# ---------------------------------------------------------------- 应用


async def _health(request: web.Request) -> web.Response:
    return _ok(version="0.6")


def create_app(config_path: str | None = None) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app[CONFIG_PATH_KEY] = config_path

    resolved = Path(config_path) if config_path else PROJECT_ROOT / "config.yaml"

    def _cfg_provider() -> Config:
        # 配置写坏了也不能让「启动/停止」按钮和日志页直接 500
        try:
            return load_config(str(resolved))
        except Exception:  # noqa: BLE001
            return build_config({})

    app[BOT_KEY] = BotProcess(resolved, _cfg_provider)
    app[LOGIN_KEY] = LoginManager(_cfg_provider)
    app.router.add_get("/", index)
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/schema", get_schema)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/config", save_config_handler)
    app.router.add_post("/api/rooms/resolve", resolve_room)
    app.router.add_get("/api/prompts", get_prompts)
    app.router.add_get("/api/prompts/list", list_profiles)
    app.router.add_post("/api/prompts", save_prompts)
    app.router.add_post("/api/prompts/create", create_prompt_profile)
    app.router.add_post("/api/ui-state", save_ui_state)
    app.router.add_get("/api/prompt/preview", preview_prompt)
    app.router.add_post("/api/check", run_check)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/decisions", get_decisions)
    app.router.add_get("/api/logs", get_logs)
    for _action in ("start", "stop", "restart"):
        app.router.add_post(f"/api/bot/{_action}", bot_control)
    app.router.add_post("/api/login/start", login_start)
    app.router.add_get("/api/login/status", login_status)
    app.router.add_post("/api/login/cancel", login_cancel)
    app.router.add_get("/api/login/whoami", login_whoami)
    app.router.add_post("/api/llm/key", save_llm_key)
    app.router.add_post("/api/llm/test", test_llm)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR)
    return app


async def run_panel(host: str = "127.0.0.1", port: int = 8765,
                    config_path: str | None = None, open_browser: bool = True) -> None:
    app = create_app(config_path)
    runner = web.AppRunner(app)
    await runner.setup()

    candidates = [0] if port == 0 else list(range(port, port + 20))
    site = None
    actual_port = port
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            site = web.TCPSite(runner, host, candidate)
            await site.start()
            actual_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            break
        except OSError as exc:
            last_error = exc
            site = None
            continue
    if site is None:
        await runner.cleanup()
        raise OSError(f"{host}:{port} 起 20 个端口都被占用，最后一个错误：{last_error}")

    url = f"http://{host}:{actual_port}/"
    logger.info("设置面板已启动: %s", url)
    print(f"\n设置面板: {url}")
    if actual_port != port:
        print(f"（端口 {port} 被占用，已自动改用 {actual_port}）")
    print("按 Ctrl+C 停止。\n")
    if open_browser:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
