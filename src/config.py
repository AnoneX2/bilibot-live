# -*- coding: utf-8 -*-
"""配置加载：YAML -> dataclass，支持环境变量覆盖与基本校验。"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .util.log import get_logger

log = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXAMPLE_CONFIG_NAME = "config.example.yaml"
CONFIG_NAME = "config.yaml"


def default_config_path() -> Path:
    """默认配置文件：优先 config.yaml，缺失时从模板复制一份再返回。

    不能直接拿 config.example.yaml 当运行配置：那是 Git 跟踪的模板，
    面板保存时会把 API Key 明文写进去，一次 `git add -A` 就泄露了。
    """
    target = PROJECT_ROOT / CONFIG_NAME
    if target.exists():
        return target
    example = PROJECT_ROOT / EXAMPLE_CONFIG_NAME
    if example.exists():
        try:
            shutil.copyfile(example, target)
            log.info("已根据 %s 生成 %s（模板本身不会被改写）", EXAMPLE_CONFIG_NAME, target)
        except OSError as exc:
            log.warning(
                "复制配置模板失败（%s）：只能临时读模板，保存配置会失败"
                "（不会往 Git 跟踪的 %s 里写 Key）",
                exc, EXAMPLE_CONFIG_NAME,
            )
            return example
    return target


# ---------------------------------------------------------------- 子配置


@dataclass
class RoomSpec:
    room_id: int
    alias: str = ""
    enabled: bool = True
    prompt_dir: str = ""  # 可选：该房间单独使用一套提示词目录

    def display(self) -> str:
        return f"{self.alias}({self.room_id})" if self.alias else str(self.room_id)


@dataclass
class AppConfig:
    rooms: List[RoomSpec] = field(default_factory=list)
    dry_run: bool = True
    log_level: str = "INFO"
    log_dir: str = "logs"
    data_dir: str = "data"
    global_min_interval_sec: float = 20.0
    # 被 B 站 判定「已在本房间被禁言」时，自动把这个直播间改成 enabled=false
    auto_disable_muted: bool = True
    # 决策/转写/发送记录与场次存档只保留最近几天；0 = 不清理（一直留着）
    retention_days: int = 7


@dataclass
class BiliConfig:
    cookie_file: str = "data/cookies.json"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    http_timeout_sec: float = 15.0


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    # API Key：只从配置文件读（设置面板写入；config.yaml 在 .gitignore 中，权限 600）。
    api_key: str = ""
    model: str = "deepseek-flash"
    vision: bool = True
    temperature: float = 1.0
    # 推理模型会先输出「思考」，思考 token 也计入 max_tokens。
    # 太小会导致 finish_reason=length、正文为空，因此默认给足余量。
    max_tokens: int = 12288
    timeout_sec: float = 60.0
    image_max_side: int = 768
    lenient_parse: bool = True
    # 提示词目录（内含 system.md / user.md），改完自动热更新
    prompt_dir: str = "prompts"
    # 思考模式：auto=服务端默认；enabled/disabled=显式开关（thinking.type）
    thinking: str = "auto"
    # 思考等级：auto=不传；none/low/high/max=reasoning_effort（DeepSeek 官方语义）
    reasoning_effort: str = "auto"
    # 透传给服务端的额外字段（高级用法），例如自定义网关参数
    extra_body: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ASRConfig:
    # 识别方式：
    #   stream  = 真流式，边说边出字（延迟低、更准，但背景音乐/歌声也会被转写）
    #   segment = VAD 断句后整句转写（能挡掉纯 BGM，代价是多等一次静音确认）
    mode: str = "segment"
    # X-ASR 模型根目录：下面放 chunk-<N>ms-model 目录
    xasr_model_dir: str = "data/models/xasr"
    # X-ASR 流式 chunk：越大越准（160/480/960/1920）
    xasr_chunk_ms: int = 1920
    # sherpa-onnx 推理线程数
    xasr_threads: int = 4
    max_workers: int = 1
    filter_hallucination: bool = True
    max_audio_sec: float = 12.0
    # 主播停顿后等多久再送去识别（仅 segment 模式；避免把一句话切成两半）
    merge_wait_sec: float = 1.2
    # 两段语音间隔超过该值就认为不是同一句，立即落盘（仅 segment 模式）
    merge_gap_max_sec: float = 2.5


@dataclass
class StreamConfig:
    refresh_sec: int = 1800
    stall_timeout_sec: float = 15.0
    frame_interval_sec: float = 45.0
    frame_ttl_sec: float = 180.0
    qn: int = 250
    audio_only: bool = False


@dataclass
class VadConfig:
    sample_rate: int = 16000
    min_rms: int = 210
    noise_factor: float = 3.2
    start_ms: int = 60
    silence_ms: int = 1000
    min_speech_ms: int = 900
    max_speech_ms: int = 12000
    pre_roll_ms: int = 400


@dataclass
class ReplyConfig:
    # speech：只在主播说话后判断要不要接话（默认）
    # ambient：定时巡检，结合语音+弹幕决定
    # off：只观察不回复
    mode: str = "speech"
    max_chars: int = 30
    # 主播每说一句都可能触发，这里限制「多久才允许触发一次」的判断频率
    speech_trigger_interval_sec: float = 30.0
    # 太短的口头语不触发（ASR 还会再做一次语气词过滤）
    speech_min_chars: int = 6
    # ambient 模式的巡检间隔
    ambient_interval_sec: float = 120.0
    min_interval_sec: float = 65.0
    max_per_hour: int = 60
    max_per_minute: int = 3
    # 仅对 ambient 生效；speech 模式每句都判断，不掷骰子
    probability: float = 0.7
    quiet_hours: List[str] = field(default_factory=list)
    banned_words: List[str] = field(default_factory=list)
    dup_threshold: float = 0.85
    # 置信度门槛：开启后，模型返回的 confidence 低于阈值就不发
    confidence_gate: bool = False
    confidence_threshold: float = 0.6

    # ---- 被 @ 时的回应（观众在弹幕里 @ 了 BOT 账号，才允许接一句） ----
    # 默认开启，但受下面两道独立限制约束，保证「适度」
    mention_enabled: bool = True
    # 两次「@ 回应」之间的最小间隔（秒）：比普通回复冷却更保守
    mention_min_interval_sec: float = 120.0
    # 每小时最多因为被 @ 而回应几次（不含其它发送）
    mention_max_per_hour: int = 8

    # ---- 主播指令：/mute N 让 BOT 自我禁言（/unmute 提前解除，始终可用） ----
    mute_command_enabled: bool = True


@dataclass
class ContextConfig:
    # 0 = 本场直播全程（推荐）；>0 = 只看最近 N 秒
    transcript_window_sec: float = 0.0
    danmaku_window_sec: float = 0.0
    max_danmaku: int = 80
    max_chars: int = 8000
    # 首次连上直播时补几条历史弹幕作为氛围；0=不补，避免把上一场的内容带进来
    history_danmaku: int = 0


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    bili: BiliConfig = field(default_factory=BiliConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    context: ContextConfig = field(default_factory=ContextConfig)

    # 运行时补充字段（不由 yaml 提供）
    config_path: str = ""
    # 文件里的原始内容（套用环境变量/命令行覆盖之前），热加载 diff 的基准；
    # 不参与 to_dict()，所以不会出现在面板/写回的配置里
    file_snapshot: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 路径

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def data_path(self) -> Path:
        return self.path(self.app.data_dir)

    @property
    def log_path(self) -> Path:
        return self.path(self.app.log_dir)

    @property
    def cookie_path(self) -> Path:
        return self.path(self.bili.cookie_file)

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.log_path, self.path(self.asr.xasr_model_dir)):
            p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ LLM key

    def api_key(self) -> str:
        """API Key：只认配置文件（面板保存的那份）。"""
        return (self.llm.api_key or "").strip()

    # ------------------------------------------------------------ 序列化

    def to_dict(self, mask_secrets: bool = False) -> Dict[str, Any]:
        """转成可 JSON 化/写回 YAML 的普通字典。

        mask_secrets=True 时把 API Key 换成 `sk-abcd…wxyz` 形式的掩码，
        用于面板接口返回：浏览器永远拿不到明文。
        """
        llm = asdict(self.llm)
        if mask_secrets:
            llm["api_key"] = mask_secret(llm.get("api_key", ""))
        return {
            "app": asdict(self.app),
            "bili": asdict(self.bili),
            "llm": llm,
            "asr": asdict(self.asr),
            "stream": asdict(self.stream),
            "vad": asdict(self.vad),
            "reply": asdict(self.reply),
            "context": asdict(self.context),
        }

    def active_rooms(self) -> List[RoomSpec]:
        return [room for room in self.app.rooms if room.enabled]

    # ------------------------------------------------------------ 校验

    def validate(self) -> List[str]:
        """返回错误列表，空列表代表通过。"""
        errors: List[str] = []
        if not self.app.rooms:
            errors.append("app.rooms 为空：至少配置一个直播间")
        if self.reply.mode in {"reactive", "hybrid"}:
            errors.append(
                "reply.mode 的 reactive/hybrid 已废弃：弹幕不再触发回复。"
                "请改为 speech（主播说话触发，默认）/ ambient（定时）/ off"
            )
        elif self.reply.mode not in {"speech", "ambient", "off"}:
            errors.append(f"reply.mode 非法: {self.reply.mode}（可选 speech/ambient/off）")
        if self.reply.speech_min_chars < 1:
            errors.append("reply.speech_min_chars 必须 >= 1")
        if not (1 <= self.reply.max_chars <= 100):
            errors.append("reply.max_chars 必须在 1..100")
        if not (0.0 <= self.reply.probability <= 1.0):
            errors.append("reply.probability 必须在 0..1")
        if self.reply.max_per_minute <= 0 or self.reply.max_per_hour <= 0:
            errors.append("reply 频率限制必须为正数")
        if not (0.0 <= self.reply.confidence_threshold <= 1.0):
            errors.append("reply.confidence_threshold 必须在 0..1")
        if self.reply.mention_min_interval_sec < 0:
            errors.append("reply.mention_min_interval_sec 不能为负")
        if self.reply.mention_max_per_hour < 1:
            errors.append("reply.mention_max_per_hour 必须 >= 1")
        if self.vad.sample_rate != 16000:
            errors.append("vad.sample_rate 目前仅支持 16000（ASR 模型要求）")
        if self.vad.max_speech_ms <= self.vad.min_speech_ms:
            errors.append("vad.max_speech_ms 必须大于 vad.min_speech_ms")
        if self.llm.provider == "deepseek" and self.llm.vision is False:
            # 允许，只是不带图
            pass
        if not isinstance(self.llm.extra_body, dict):
            errors.append("llm.extra_body 必须是映射（dict）")
        if self.llm.thinking not in ("auto", "enabled", "disabled"):
            errors.append("llm.thinking 只能是 auto/enabled/disabled")
        if self.llm.reasoning_effort not in ("auto", "none", "low", "high", "max"):
            errors.append("llm.reasoning_effort 只能是 auto/none/low/high/max")
        if self.llm.thinking == "disabled" and self.llm.reasoning_effort in ("low", "high", "max"):
            errors.append("llm.thinking=disabled 与 reasoning_effort 冲突")
        if self.llm.max_tokens < 64:
            errors.append("llm.max_tokens 太小（<64），推理模型会被思考吃光导致空回复")
        if self.asr.mode not in {"stream", "segment"}:
            errors.append(f"asr.mode 非法: {self.asr.mode}（可选 stream / segment）")
        if int(self.asr.xasr_chunk_ms) not in (160, 480, 960, 1920):
            errors.append("asr.xasr_chunk_ms 只能是 160 / 480 / 960 / 1920")
        if not (1 <= int(self.asr.xasr_threads) <= 32):
            errors.append("asr.xasr_threads 必须在 1..32")
        if self.asr.merge_wait_sec < 0.2:
            errors.append("asr.merge_wait_sec 太小（<0.2），会频繁切句")
        if self.asr.merge_gap_max_sec <= self.asr.merge_wait_sec:
            errors.append("asr.merge_gap_max_sec 应大于 asr.merge_wait_sec")
        for spec in self.app.rooms:
            if spec.room_id <= 0:
                errors.append(f"房间号非法: {spec.room_id}")
        return errors

    def summary(self) -> str:
        rooms = ", ".join(r.display() for r in self.app.rooms) or "(空)"
        asr = (
            f"xasr/{self.asr.mode}/chunk-{self.asr.xasr_chunk_ms}ms"
            f"/{self.asr.xasr_threads}线程"
        )
        return (
            f"rooms=[{rooms}] dry_run={self.app.dry_run} "
            f"llm={self.llm.provider}/{self.llm.model} vision={self.llm.vision} "
            f"asr={asr} "
            f"reply={self.reply.mode} max_chars={self.reply.max_chars}"
        )


# ---------------------------------------------------------------- 机密


def mask_secret(value: str) -> str:
    """把机密打码成 `sk-abc…wxyz`；太短则完全遮住。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


# ---------------------------------------------------------------- 加载


def _filter_known(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__} 存在未知配置项: {sorted(unknown)}")
    return {k: v for k, v in data.items() if k in known}


def _parse_rooms(raw: Any) -> List[RoomSpec]:
    rooms: List[RoomSpec] = []
    if raw is None:
        return rooms
    if not isinstance(raw, list):
        raise ValueError("app.rooms 必须是列表")
    for item in raw:
        if isinstance(item, (int, str)):
            rooms.append(RoomSpec(room_id=int(item)))
        elif isinstance(item, dict):
            item = _filter_known(RoomSpec, item)
            rooms.append(RoomSpec(**item))
        else:
            raise ValueError(f"app.rooms 元素类型非法: {type(item)}")
    return rooms


def _apply_env(cfg: Config) -> None:
    """环境变量覆盖（便于容器/临时调试）。"""
    if v := os.environ.get("BOT_DRY_RUN"):
        cfg.app.dry_run = v.strip().lower() in {"1", "true", "yes", "on"}
    if v := os.environ.get("BOT_ROOMS"):
        ids = [x.strip() for x in v.split(",") if x.strip()]
        cfg.app.rooms = [RoomSpec(room_id=int(x)) for x in ids]
    if v := os.environ.get("BOT_LOG_LEVEL"):
        cfg.app.log_level = v.strip().upper()
    if v := os.environ.get("DEEPSEEK_BASE_URL"):
        cfg.llm.base_url = v.strip()
    if v := os.environ.get("DEEPSEEK_MODEL"):
        cfg.llm.model = v.strip()


SECTION_TYPES: Dict[str, Any] = {
    "app": AppConfig,
    "bili": BiliConfig,
    "llm": LLMConfig,
    "asr": ASRConfig,
    "stream": StreamConfig,
    "vad": VadConfig,
    "reply": ReplyConfig,
    "context": ContextConfig,
}


def _clean_fields(cls, data: Dict[str, Any], label: str) -> tuple[Dict[str, Any], List[str]]:
    known = {f.name for f in fields(cls)}
    cleaned = {k: v for k, v in data.items() if k in known}
    dropped = [f"{label}.{k}" for k in data if k not in known]
    return cleaned, dropped


def sanitize_raw(raw: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """丢弃配置里已废弃/拼错的字段，返回 (干净配置, 被丢弃的键)。

    用于设置面板：旧版配置（如 reply.style、reply.keywords）不会导致面板打不开，
    保存时会自动清理这些键。
    """
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是映射")
    dropped: List[str] = []
    clean: Dict[str, Any] = {}
    for section, value in raw.items():
        if section not in SECTION_TYPES:
            dropped.append(section)
            continue
        value = value or {}
        if not isinstance(value, dict):
            dropped.append(section)
            continue
        if section == "app":
            app_raw = dict(value)
            rooms = app_raw.pop("rooms", None)
            cleaned, drops = _clean_fields(AppConfig, app_raw, "app")
            dropped.extend(drops)
            if rooms is not None:
                if not isinstance(rooms, list):
                    dropped.append("app.rooms")
                else:
                    clean_rooms = []
                    for index, room in enumerate(rooms):
                        if isinstance(room, dict):
                            room_clean, room_drops = _clean_fields(
                                RoomSpec, room, f"app.rooms[{index}]"
                            )
                            dropped.extend(room_drops)
                            clean_rooms.append(room_clean)
                        else:
                            clean_rooms.append(room)
                    cleaned["rooms"] = clean_rooms
            clean["app"] = cleaned
        else:
            cleaned, drops = _clean_fields(SECTION_TYPES[section], value, section)
            dropped.extend(drops)
            clean[section] = cleaned
    return clean, dropped


def build_config(raw: Dict[str, Any]) -> Config:
    """从普通字典构建配置（不做文件 IO、不套用环境变量）。"""
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是映射")

    known_sections = {f.name for f in fields(Config)} - {"config_path"}
    unknown_sections = set(raw) - known_sections
    if unknown_sections:
        raise ValueError(f"存在未知配置段: {sorted(unknown_sections)}")

    app_raw = dict(raw.get("app") or {})
    rooms = _parse_rooms(app_raw.pop("rooms", None))
    return Config(
        app=AppConfig(rooms=rooms, **_filter_known(AppConfig, app_raw)),
        bili=BiliConfig(**_filter_known(BiliConfig, raw.get("bili") or {})),
        llm=LLMConfig(**_filter_known(LLMConfig, raw.get("llm") or {})),
        asr=ASRConfig(**_filter_known(ASRConfig, raw.get("asr") or {})),
        stream=StreamConfig(**_filter_known(StreamConfig, raw.get("stream") or {})),
        vad=VadConfig(**_filter_known(VadConfig, raw.get("vad") or {})),
        reply=ReplyConfig(**_filter_known(ReplyConfig, raw.get("reply") or {})),
        context=ContextConfig(**_filter_known(ContextConfig, raw.get("context") or {})),
    )


def load_config(path: Optional[str] = None, *, apply_env: bool = True) -> Config:
    """加载配置。

    path 为 None 时用 config.yaml（缺失则从模板复制一份）。
    apply_env=False 用来「只读文件内容做对比」（热加载 diff），不套环境变量覆盖。
    """
    cfg_path = Path(path) if path else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError("找不到配置文件：请复制 config.example.yaml 为 config.yaml")

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # 兼容旧配置：这些字段已废弃，直接丢掉不要炸
    if isinstance(raw, dict):
        if isinstance(raw.get("llm"), dict):
            if raw["llm"].pop("api_key_env", None) is not None:
                log.warning("配置项 llm.api_key_env 已废弃，已忽略（Key 只从 config.yaml 读取）")
        if isinstance(raw.get("reply"), dict):
            if raw["reply"].pop("mention_names", None) is not None:
                log.warning("配置项 reply.mention_names 已废弃，已忽略（@ 只按 reply_mid 判断）")
    cfg = build_config(raw)
    cfg.config_path = str(cfg_path)
    # 基准必须在覆盖之前取：否则 BOT_DRY_RUN=true 这类启动期覆盖会被当成
    # 文件内容，之后改文件里的 dry_run 反而不产生 diff、热加载不触发
    cfg.file_snapshot = cfg.to_dict()
    if apply_env:
        _apply_env(cfg)

    errors = cfg.validate()
    if errors:
        raise ValueError("配置校验失败:\n  - " + "\n  - ".join(errors))
    return cfg
