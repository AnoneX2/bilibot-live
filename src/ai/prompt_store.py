# -*- coding: utf-8 -*-
"""提示词加载：人设发现、从文件读取、热更新与占位符校验。

支持三种人设：
1. 默认：`prompts/system.md` + `prompts/user.md`
2. 单文件：`prompts/persona.md`（作为 system，user 模板共用 `prompts/user.md`）
3. 目录：`prompts/persona/system.md` + `prompts/persona/user.md`

占位符语法故意用 `{{name}}`，避免和 JSON 示例里的单层花括号冲突，
用户可以直接用记事本编辑 Prompt，不需要了解 Python 的 format 语法。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config import PROJECT_ROOT, Config
from ..util.log import get_logger

logger = get_logger("ai.prompt_store")

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# 时间线里每条记录的标签
TIMELINE_TAGS = {"speech": "语音", "danmaku": "弹幕", "self": "我说"}

SYSTEM_VARS: Set[str] = {"max_chars", "bot_name"}
USER_VARS: Set[str] = {
    "title",
    "alias",
    "alias_part",
    # 机器人自己登录的 B 站账号名（让它认得自己）
    "bot_name",
    "clock",
    "frame_desc",
    # 语音 + 弹幕合成的一条时间线（默认模板用它）
    "timeline",
    # 单独取语音 / 弹幕：只为兼容自己改过 user.md 的人，新模板不必用
    "transcript",
    "danmaku",
    "trigger",
    "max_chars",
    "room_id",
}

RESERVED_FILES = {"system.md", "user.md", "readme.md"}


class PromptError(ValueError):
    """Prompt 文件缺失/占位符非法/人设不存在。"""


@dataclass(frozen=True)
class ProfileInfo:
    id: str
    label: str
    kind: str  # default | file | dir
    system_path: str = ""
    user_path: str = ""


@dataclass
class _CachedFile:
    mtime: float
    text: str


# ---------------------------------------------------------------- 人设发现


def discover_profiles(base_dir: Path) -> List[ProfileInfo]:
    """列出 base_dir 下可用的人设（单文件 *.md、子目录 system.md）。"""
    profiles: List[ProfileInfo] = [
        ProfileInfo(
            id="",
            label="默认人设（system.md + user.md）",
            kind="default",
            system_path=str(base_dir / "system.md"),
            user_path=str(base_dir / "user.md"),
        )
    ]
    if not base_dir.exists():
        return profiles

    for path in sorted(base_dir.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith("."):
            continue
        if path.is_file() and path.suffix.lower() == ".md":
            lowered = path.name.lower()
            if lowered in RESERVED_FILES or lowered.startswith("system"):
                # system.md 及其副本不算独立人设
                continue
            profiles.append(
                ProfileInfo(
                    id=path.name,
                    label=f"{path.stem}（单文件人设）",
                    kind="file",
                    system_path=str(path),
                    user_path=str(base_dir / "user.md"),
                )
            )
        elif path.is_dir() and (path / "system.md").exists():
            user = path / "user.md"
            profiles.append(
                ProfileInfo(
                    id=path.name,
                    label=f"{path.name}（目录人设）",
                    kind="dir",
                    system_path=str(path / "system.md"),
                    user_path=str(user if user.exists() else base_dir / "user.md"),
                )
            )
    return profiles


def _candidate_paths(base_dir: Path, spec: str) -> List[Path]:
    path = Path(spec)
    if path.is_absolute():
        return [path]
    return [base_dir / spec, PROJECT_ROOT / spec]


def resolve_profile(base_dir: Path, spec: str) -> Tuple[Path, Path, str]:
    """把配置里的 prompt_dir（可能是 ""、"persona"、"persona.md"、"prompts/x"）解析成文件路径。"""
    base_dir = base_dir.resolve()
    spec = (spec or "").strip().replace("\\", "/")
    if not spec or spec in (".", "./"):
        return base_dir / "system.md", base_dir / "user.md", "default"

    for candidate in _candidate_paths(base_dir, spec):
        resolved = candidate.resolve()
        if resolved == base_dir:
            return base_dir / "system.md", base_dir / "user.md", "default"
        if resolved.is_file() and resolved.suffix.lower() == ".md":
            shared_user = base_dir / "user.md"
            if not shared_user.exists():
                raise PromptError(
                    f"单文件人设 {resolved.name} 需要共用 {shared_user}，但该文件不存在"
                )
            return resolved, shared_user, "file"
        if resolved.is_dir():
            system = resolved / "system.md"
            if system.exists():
                user = resolved / "user.md"
                return system, (user if user.exists() else base_dir / "user.md"), "dir"

    # 允许只写名字（persona -> persona.md）
    for candidate in _candidate_paths(base_dir, spec):
        with_suffix = candidate.with_suffix(".md")
        if with_suffix.is_file():
            shared_user = base_dir / "user.md"
            if not shared_user.exists():
                raise PromptError(f"单文件人设 {with_suffix.name} 缺少共用的 {shared_user}")
            return with_suffix, shared_user, "file"

    available = ", ".join(p.id or "(默认)" for p in discover_profiles(base_dir))
    raise PromptError(f"找不到人设「{spec}」，可用：{available}")


# ---------------------------------------------------------------- PromptStore


class PromptStore:
    """一个房间一套 Prompt（默认共用 prompts/，可按房间指定人设）。"""

    def __init__(self, cfg: Config, prompt_dir: Optional[str] = None, room_id: int = 0):
        self._cfg = cfg
        self._room_id = room_id
        self.base_dir = cfg.path(cfg.llm.prompt_dir)
        self.profile = (prompt_dir or "").strip()
        self.system_path, self.user_path, self.kind = resolve_profile(
            self.base_dir, self.profile
        )
        self.dir = self.system_path.parent
        self._cache: Dict[Path, _CachedFile] = {}
        self._last_check = 0.0

    # ------------------------------------------------------------ 读取

    def _read(self, path: Path) -> str:
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise PromptError(f"找不到提示词文件 {path}") from exc
        cached = self._cache.get(path)
        if cached and cached.mtime >= mtime:
            return cached.text
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptError(f"读取提示词失败 {path}: {exc}") from exc
        self._cache[path] = _CachedFile(mtime=mtime, text=text)
        logger.info("已加载提示词 %s（%d 字）", path.name, len(text))
        return text

    def _placeholders(self, text: str) -> Set[str]:
        return {m.group(1) for m in PLACEHOLDER_RE.finditer(text)}

    # ------------------------------------------------------------ 校验

    def validate(self) -> List[str]:
        errors: List[str] = []
        try:
            system = self._read(self.system_path)
            user = self._read(self.user_path)
        except PromptError as exc:
            return [str(exc)]

        if not system.strip():
            errors.append(f"{self.system_path.name} 为空")
        if not user.strip():
            errors.append(f"{self.user_path.name} 为空")

        unknown_system = self._placeholders(system) - SYSTEM_VARS
        if unknown_system:
            errors.append(
                f"{self.system_path.name} 含未知占位符 {sorted(unknown_system)}，"
                f"system 可用：{sorted(SYSTEM_VARS)}"
            )
        unknown_user = self._placeholders(user) - USER_VARS
        if unknown_user:
            errors.append(
                f"{self.user_path.name} 含未知占位符 {sorted(unknown_user)}，"
                f"user 可用：{sorted(USER_VARS)}"
            )
        if self.kind == "file":
            # 单文件人设只当 system 用，提示用户 user 模板是共用的
            user_vars_in_system = (self._placeholders(system) & USER_VARS) - SYSTEM_VARS
            if user_vars_in_system:
                errors.append(
                    f"单文件人设 {self.system_path.name} 里出现了 user 变量 "
                    f"{sorted(user_vars_in_system)}；单文件人设只作为 system，"
                    "动态内容请写在 prompts/user.md"
                )
        return errors

    # ------------------------------------------------------------ 渲染

    def raw(self, kind: str = "user") -> str:
        path = self.system_path if kind == "system" else self.user_path
        return self._read(path)

    def system(self, **kwargs) -> str:
        values: Dict[str, object] = {"max_chars": self._cfg.reply.max_chars}
        values.update(kwargs)
        text = self._read(self.system_path)
        return self._render(text, values, "system")

    def user(self, **kwargs) -> str:
        values = dict(kwargs)
        values.setdefault("max_chars", self._cfg.reply.max_chars)
        values.setdefault("room_id", self._room_id)
        text = self._read(self.user_path)
        return self._render(text, values, "user")

    @staticmethod
    def _render(text: str, values: Dict[str, object], name: str) -> str:
        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in values:
                logger.warning("%s 的占位符 {{%s}} 没有对应值", name, key)
                return match.group(0)
            return str(values[key])

        return PLACEHOLDER_RE.sub(replace, text)

    # ------------------------------------------------------------ 热更新

    def maybe_reload(self) -> None:
        now = time.time()
        if now - self._last_check < 2.0:
            return
        self._last_check = now
        for path in (self.system_path, self.user_path):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached is None or cached.mtime < mtime:
                try:
                    self._read(path)
                except PromptError as exc:
                    logger.warning("重新加载提示词失败: %s", exc)

    # ------------------------------------------------------------ 展示

    @property
    def profile_label(self) -> str:
        resolved = self.system_path.resolve()
        for info in discover_profiles(self.base_dir):
            try:
                if Path(info.system_path).resolve() == resolved:
                    return info.label
            except OSError:
                continue
        return self.profile or "默认人设"


def format_timeline(items: Iterable[tuple], max_chars: int, omitted: int = 0) -> str:
    """把语音和弹幕合成一条按时间排好的记录。

    每条渲染成 `- [语音] …` / `- [弹幕] 观众: …`（不带时间：顺序本身就是时间）。
    行成本 = len(text) + 8，必须和 core.context.PromptWindow.LINE_COST 一致。
    """
    rows = [(ts, kind, text) for ts, kind, text in items if text]
    if not rows:
        if omitted:
            return f"（较早的 {omitted} 条记录因篇幅已省略）"
        return "（暂无，主播可能还没说话）"
    rendered: List[str] = []
    budget = max_chars
    for ts, kind, text in reversed(rows):
        item = f"- [{TIMELINE_TAGS.get(kind, kind)}] {text}"
        if budget - len(item) < 0 and rendered:
            break
        rendered.append(item)
        budget -= len(item)
    rendered.reverse()
    omitted += len(rows) - len(rendered)
    head = f"（较早的 {omitted} 条记录因篇幅已省略）\n" if omitted > 0 else ""
    return head + "\n".join(rendered)


def format_transcript(lines: Iterable[tuple], now: float, max_chars: int,
                      omitted: int = 0) -> str:
    """把语音渲染成列表。`omitted` 是调用方在更早处已经裁掉的句数（迟滞窗口）。"""
    items = [(ts, text) for ts, text in lines if text]
    if not items:
        if omitted:
            return f"（本场较早的 {omitted} 句因篇幅已省略）"
        return "（暂无，主播可能还没说话）"
    rendered: List[str] = []
    budget = max_chars
    for ts, text in reversed(items):
        item = f"- {text}"
        if budget - len(item) < 0 and rendered:
            break
        rendered.append(item)
        budget -= len(item)
    rendered.reverse()
    omitted += len(items) - len(rendered)
    head = f"（本场较早的 {omitted} 句因篇幅已省略）\n" if omitted > 0 else ""
    return head + "\n".join(rendered)


def format_danmaku(lines: Iterable[str], max_chars: int, omitted: int = 0) -> str:
    items = list(lines)
    if not items:
        if omitted:
            return f"（较早的 {omitted} 条弹幕因篇幅已省略）"
        return "（暂无弹幕）"
    out: List[str] = []
    budget = max_chars
    for line in reversed(items):
        item = f"- {line}"
        if budget - len(item) < 0 and out:
            break
        out.append(item)
        budget -= len(item)
    out.reverse()
    omitted += len(items) - len(out)
    head = f"（较早的 {omitted} 条弹幕因篇幅已省略）\n" if omitted > 0 else ""
    return head + "\n".join(out)
