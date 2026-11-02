# -*- coding: utf-8 -*-
"""把 Config 写回 config.yaml（带中文注释）。

面板保存时用这个模块生成 YAML，注释由 `settings_schema` 提供，
因此 YAML 永远和面板里的说明一致，不会出现「文档与配置脱节」。
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

from .config import Config
from .settings_schema import GROUPS, Field

SECTION_ORDER = ["app", "reply", "asr", "vad", "stream", "llm", "bili", "context"]


def _dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _get(data: Dict[str, Any], key: str) -> Any:
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _comment(field: Field) -> str:
    text = f"{field.label}：{field.help}"
    if field.unit:
        text += f"（单位：{field.unit}）"
    return text


def dump_config(cfg: Config) -> str:
    data = cfg.to_dict()
    fields_by_section: "OrderedDict[str, List[Field]]" = OrderedDict()
    for section in SECTION_ORDER:
        fields_by_section[section] = []
    for group in GROUPS:
        for field in group.fields:
            if field.key == "app.rooms":
                continue
            section = field.key.split(".")[0]
            fields_by_section.setdefault(section, []).append(field)

    lines: List[str] = [
        "# ============================================================",
        "# bilibot 配置",
        f"# 由设置面板生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# 建议用 `python -m src.app panel` 修改，字段说明与面板一致；",
        "# 也可以手动编辑，改完重启 BOT 生效。",
        "# ============================================================",
    ]

    for section in fields_by_section:
        fields = fields_by_section[section]
        lines.append("")
        lines.append(f"{section}:")
        if section == "app":
            lines.extend(_dump_rooms(data["app"]["rooms"]))
        for field in fields:
            key = field.key.split(".", 1)[1] if "." in field.key else field.key
            value = _get(data, field.key)
            lines.append(f"  # {_comment(field)}")
            lines.append(f"  {key}: {_dump_value(value)}")

    lines.append("")
    return "\n".join(lines)


def _dump_rooms(rooms: List[Dict[str, Any]]) -> List[str]:
    lines = [
        "  # 直播间列表；enabled=false 的房间会被跳过。",
        "  # 每个房间可单独指定 prompt_dir（独立人设），留空表示用全局提示词。",
        "  rooms:",
    ]
    if not rooms:
        lines[-1] = "  rooms: []"
        return lines
    for room in rooms:
        lines.append(f"    - room_id: {int(room.get('room_id', 0))}")
        lines.append(f"      alias: {_dump_value(room.get('alias', ''))}")
        lines.append(f"      enabled: {_dump_value(bool(room.get('enabled', True)))}")
        prompt_dir = room.get("prompt_dir", "") or ""
        lines.append(f"      prompt_dir: {_dump_value(prompt_dir)}")
    return lines


def _restrict(path: Path) -> None:
    """配置文件里可能有 API Key，尽量收紧到 600（非 POSIX 平台失败就算了）。"""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_config(cfg: Config, path: Path, backup: bool = True) -> Path | None:
    """原子写入，返回备份文件路径（没有旧文件则为 None）。"""
    if ".example." in path.name:
        # config.example.yaml 是 Git 跟踪的模板，里面一旦写进 API Key 就会被提交
        raise ValueError(f"拒绝写入配置模板 {path.name}：请保存到 config.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _restrict(backup_path)

    text = dump_config(cfg)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict(tmp)
    tmp.replace(path)
    _restrict(path)
    return backup_path
