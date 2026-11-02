# -*- coding: utf-8 -*-
"""设置面板的字段元数据。

这里集中定义「每个参数叫什么、怎么解释、可填什么、默认值」，
前端据此渲染表单，`tests/test_settings_schema.py` 会校验它覆盖了所有 dataclass 字段，
以后加参数忘了写说明会直接测试失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Field:
    key: str                      # 完整路径，如 reply.max_chars
    label: str                    # 中文名
    help: str                     # 一句话说明（面板直接展示）
    type: str                     # bool/int/float/str/enum/list_str/dict/rooms
    options: Sequence[Tuple[Any, str]] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    default: Any = None
    advanced: bool = False        # 折叠到「高级」里
    # 条件显示：只有当 depends_on[0] 这一项等于 depends_on[1] 时才显示本字段
    depends_on: Optional[Tuple[str, Any]] = None


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    desc: str
    fields: Sequence[Field] = field(default_factory=tuple)


_LEVELS = (("DEBUG", "DEBUG（调试）"), ("INFO", "INFO（推荐）"),
           ("WARNING", "WARNING（只看警告）"), ("ERROR", "ERROR（只看错误）"))

GROUPS: List[Group] = [
    Group(
        id="rooms",
        title="直播间",
        desc="BOT 会同时监听这里启用的房间；未勾选「启用」的房间会被跳过。"
             "填写真实房间号或短号都可以（保存时用 B 站接口归一化）。"
             "开关「启用」、添加/删除房间会立即自动保存，BOT 随即重新加载。",
        fields=[
            Field("app.rooms", "直播间列表", "添加/删除房间，设置别名、是否启用、是否使用独立提示词。", "rooms"),
        ],
    ),
    Group(
        id="reply",
        title="回复策略",
        desc="决定「什么时候说话、说多少、多久说一次」。BOT 主要还是和主播互动；"
             "只有在弹幕里被 @ 时才会适度接一句（可单独关闭或限制频率）。",
        fields=[
            Field("reply.mode", "触发模式",
                  "speech=只在主播说完一段后判断要不要接话（推荐）；ambient=按固定间隔巡检；off=只看不说。",
                  "enum", options=(("speech", "speech：主播说话触发（推荐）"),
                                   ("ambient", "ambient：定时巡检"),
                                   ("off", "off：只观察不回复")), default="speech"),
            Field("reply.max_chars", "回复字数上限",
                  "单条弹幕最大字数。B 站实际限制 20 字，超过会被平台拒绝，建议 20～30。",
                  "int", minimum=1, maximum=100, default=30, unit="字"),
            Field("reply.speech_min_chars", "最短触发字数",
                  "主播这句话转写后短于该长度就不判断，避免「嗯」「就这」这类碎片触发。",
                  "int", minimum=1, maximum=50, default=6, unit="字"),
            Field("reply.speech_trigger_interval_sec", "两次判断的最小间隔",
                  "主播连续说话时，最多多久让模型判断一次，控制成本与频率。",
                  "float", minimum=1, maximum=600, step=1, default=30, unit="秒"),
            Field("reply.min_interval_sec", "同一房间发送冷却",
                  "两次真正发弹幕之间的最小间隔，防止刷屏。",
                  "float", minimum=0, maximum=3600, step=1, default=65, unit="秒"),
            Field("reply.max_per_hour", "每小时发送上限",
                  "单房间每小时最多发几条，建议不超过 20，降低账号风控概率。",
                  "int", minimum=1, maximum=120, default=60, unit="条/小时"),
            Field("reply.max_per_minute", "全局每分钟上限",
                  "所有房间合计每分钟最多发几条，账号级保护。",
                  "int", minimum=1, maximum=30, default=3, unit="条/分钟"),
            Field("reply.probability", "定时巡检触发概率",
                  "仅 ambient 模式生效：每次巡检有多大概率真的问模型。",
                  "float", minimum=0, maximum=1, step=0.05, default=0.7),
            Field("reply.ambient_interval_sec", "巡检间隔",
                  "ambient 模式下多久巡检一次当前内容。",
                  "float", minimum=10, maximum=3600, step=1, default=120, unit="秒"),
            Field("reply.confidence_gate", "置信度门槛",
                  "开启后，模型返回的 confidence 低于阈值就不发这条。"
                  "模型没给 confidence 时按 0 处理（会被拦掉）。"
                  "被弹幕 @ 时不受此限制：那是有人点名要 BOT 接话，"
                  "是否回应已由下面的间隔/配额决定。",
                  "bool", default=False),
            Field("reply.confidence_threshold", "置信度阈值",
                  "0～1。建议 0.5～0.7：太低等于没开，太高会经常不出声。",
                  "float", minimum=0.0, maximum=1.0, step=0.05, default=0.6,
                  depends_on=("reply.confidence_gate", True)),
            Field("reply.mute_command_enabled", "允许主播用 /mute 让 BOT 安静",
                  "开启后，主播在弹幕里发 `/mute 100` 可以让 BOT 自我禁言 100 分钟"
                  "（只认主播本人，N 是分钟，1~1440）；期间照常记录弹幕/语音，但不再请求大模型、"
                  "也不再发弹幕。主播发 `/unmute` 可以提前解除。"
                  "关掉后 /mute 与 /unmute 都不再响应，已经在禁言中的会立即解除，"
                  "且不会往直播间发任何提示。",
                  "bool", default=True),
            Field("reply.mention_enabled", "被 @ 时回应",
                  "有人「回复」了 BOT 发的弹幕时，允许接一句（B 站把 @ 记在弹幕的 reply_mid 里，"
                  "不是正文）。需要先扫码登录，否则没有 UID 可比对。仍受下面的间隔/配额以及冷却、"
                  "黑名单等闸门约束，但不受置信度门槛影响。",
                  "bool", default=True),
            Field("reply.mention_min_interval_sec", "两次 @ 回应的最小间隔",
                  "防止有人刷 @ 就把 BOT 变成自动回复机；比普通回复冷却更保守。",
                  "float", minimum=0, maximum=3600, step=10, default=120, unit="秒"),
            Field("reply.mention_max_per_hour", "@ 回应每小时上限",
                  "每小时最多因为被 @ 而回应几次（与「每小时发送上限」叠加，取更严的那个）。",
                  "int", minimum=1, maximum=120, default=8, unit="次/小时"),
            Field("reply.quiet_hours", "静默时段",
                  '该时段内完全不发弹幕，格式如 ["01:00-08:00"]，支持跨午夜。留空表示全天可发。',
                  "list_str", default=[]),
            Field("reply.banned_words", "回复黑名单词",
                  "回复中出现任意一个词就丢弃，一行一个。",
                  "list_str", default=[]),
            Field("reply.dup_threshold", "重复判定阈值",
                  "与最近发过的内容相似度超过该值就不发（0.85 表示 85% 相似）。",
                  "float", minimum=0.5, maximum=1.0, step=0.01, default=0.85, advanced=True),
        ],
    ),
    Group(
        id="asr",
        title="语音识别（ASR）",
        desc="X-ASR 中英双语流式模型（sherpa-onnx，纯 CPU 约 30 倍实时）。"
             "可选「真流式」或「VAD 断句后整句」两种识别方式；改完需重启 BOT 生效。",
        fields=[
            Field("asr.mode", "识别方式",
                  "stream=真流式，边说边出字（延迟低、上下文完整，但背景音乐/歌声也会被转写）；"
                  "segment=VAD 断句后整句转写（能挡掉纯 BGM，代价是多等一次静音确认）。",
                  "enum", options=(("stream", "stream：流式（边说边出字）"),
                                   ("segment", "segment：逐句（VAD 断句，推荐有 BGM 的直播）")),
                  default="segment"),
            Field("asr.xasr_model_dir", "X-ASR 模型目录",
                  "存放 chunk-<N>ms-model 的目录，默认 data/models/xasr；"
                  "找不到时会自动回退到与 BOT 同级的 X-ASR 安装目录。",
                  "str", default="data/models/xasr"),
            Field("asr.xasr_chunk_ms", "X-ASR 流式分块",
                  "越大越准、越慢：1920ms 最准（实测 35 倍实时），160ms 延迟最低。默认 1920。"
                  "至少放 asr.xasr_chunk_ms 对应的一档；四档都放可随时切换。改完 BOT 会自动重启加载。",
                  "enum", options=((160, "160ms（最低延迟）"), (480, "480ms"),
                                   (960, "960ms"), (1920, "1920ms（最准，推荐）")),
                  default=1920, unit="毫秒"),
            Field("asr.xasr_threads", "X-ASR 线程数",
                  "推理线程数，16 核建议 4；开太大反而拖慢抓流与模型请求。",
                  "int", minimum=1, maximum=32, default=4, unit="线程"),
            Field("asr.merge_wait_sec", "停顿等待（仅 segment）",
                  "主播停顿时先等这么多秒，看有没有下半句，避免把一句话切成两半。"
                  "调大可减少截断，但字幕会晚一点。",
                  "float", minimum=0.3, maximum=4.0, step=0.1, default=1.2, unit="秒"),
            Field("asr.merge_gap_max_sec", "换句判定（仅 segment）",
                  "两段语音间隔超过该值就认为换了一句，立即送去识别。",
                  "float", minimum=0.5, maximum=8.0, step=0.1, default=2.5, unit="秒", advanced=True),
            Field("asr.max_audio_sec", "单句最长音频",
                  "一句话最多取多少秒：流式模式下达到该长度会强制断句，逐句模式下会截断。",
                  "float", minimum=3, maximum=30, step=1, default=12, unit="秒"),
            Field("asr.filter_hallucination", "过滤幻觉文本",
                  "丢弃模型常见的复读、字幕组套话、纯语气词。",
                  "bool", default=True),
            Field("asr.max_workers", "转写线程数",
                  "多房间共用一个模型时串行转写的并发数，建议保持 1，避免抢 CPU。",
                  "int", minimum=1, maximum=4, default=1, advanced=True),
        ],
    ),
    Group(
        id="vad",
        title="断句（VAD）",
        desc="判断主播什么时候「说完了」。阈值太小会把句子切碎，太大则延迟变高。默认值适合大多数直播间。",
        fields=[
            Field("vad.silence_ms", "静音多久算一句结束",
                  "直播解说停顿短，但太短会把一句话切碎。默认 1000ms；"
                  "经常被截断就调到 1200～1500，嫌字幕慢可降到 800。",
                  "int", minimum=200, maximum=2500, step=50, default=1000, unit="毫秒"),
            Field("vad.min_speech_ms", "最短有效语音",
                  "短于该长度的声音直接丢弃，过滤咳嗽、键盘声。",
                  "int", minimum=200, maximum=3000, step=50, default=900, unit="毫秒"),
            Field("vad.max_speech_ms", "单句最长",
                  "超过该长度强制切分，避免一句话等太久。",
                  "int", minimum=2000, maximum=30000, step=500, default=12000, unit="毫秒"),
            Field("vad.min_rms", "音量阈值",
                  "环境嘈杂/主播声音小可调低（如 150）；背景音乐大导致误触发可调高（如 300）。",
                  "int", minimum=30, maximum=2000, step=10, default=210, advanced=True),
            Field("vad.noise_factor", "噪声底倍数",
                  "自适应阈值 = 噪声底 × 该倍数，一般不用改。",
                  "float", minimum=1.0, maximum=10.0, step=0.1, default=3.2, advanced=True),
            Field("vad.start_ms", "确认说话的时长",
                  "连续多少毫秒有声才认为开始说话，抗突发噪声。",
                  "int", minimum=20, maximum=500, step=10, default=60, unit="毫秒", advanced=True),
            Field("vad.pre_roll_ms", "句首保留",
                  "切句时往前多保留多少毫秒，避免吃掉第一个字。",
                  "int", minimum=0, maximum=1000, step=50, default=400, unit="毫秒", advanced=True),
            Field("vad.sample_rate", "采样率",
                  "X-ASR 要求 16000，一般不要改。",
                  "int", minimum=8000, maximum=48000, default=16000, advanced=True),
        ],
    ),
    Group(
        id="stream",
        title="直播流与画面",
        desc="拉流、抓帧与地址刷新的参数。",
        fields=[
            Field("stream.frame_interval_sec", "抓帧间隔",
                  "多久抓一张画面给模型看。越频繁越贵，45～60 秒足够。",
                  "float", minimum=5, maximum=600, step=5, default=45, unit="秒"),
            Field("stream.frame_ttl_sec", "画面有效期",
                  "超过这个时间的画面不再作为视觉输入。",
                  "float", minimum=10, maximum=600, step=10, default=180, unit="秒"),
            Field("stream.qn", "拉流清晰度",
                  "250=原画、150=高清、80=流畅。越高越吃带宽，仅看画面不需要太高。",
                  "enum", options=((250, "250 原画"), (150, "150 高清"), (80, "80 流畅")), default=250),
            Field("stream.stall_timeout_sec", "断流判定",
                  "多久收不到音频就重建拉流管道。",
                  "float", minimum=5, maximum=120, step=1, default=15, unit="秒"),
            Field("stream.refresh_sec", "播放地址刷新",
                  "B 站播放地址会过期，定期重新获取；实际还会参考 URL 里的过期时间。",
                  "int", minimum=60, maximum=7200, step=60, default=1800, unit="秒", advanced=True),
            Field("stream.audio_only", "只拉音频",
                  "开启省带宽，但抓帧也会失效（模型看不到画面）。",
                  "bool", default=False, advanced=True),
        ],
    ),
    Group(
        id="llm",
        title="大模型",
        desc="负责「看画面 + 听语音 + 读弹幕」后决定说什么。"
             "API Key 可在下面直接填写并保存到 config.yaml（启动 BOT 不再询问）；"
             "也支持用环境变量兜底。",
        fields=[
            Field("llm.provider", "服务商",
                  "deepseek=OpenAI 兼容接口；mock=本地假回复（联调用）；ollama=本地模型。",
                  "enum", options=(("deepseek", "deepseek / OpenAI 兼容"), ("ollama", "ollama 本地"),
                                   ("mock", "mock 假回复（联调）")), default="deepseek"),
            Field("llm.base_url", "接口地址",
                  "官方为 https://api.deepseek.com；第三方网关一般以 /v1 结尾。",
                  "str", default="https://api.deepseek.com"),
            Field("llm.model", "模型名",
                  "如模型不支持图像输入，请关闭下方的「发送画面」设置。网关的模型名可能和官方不同。",
                  "str", default="deepseek-flash"),
            Field("llm.api_key", "API Key",
                  "保存在 config.yaml（该文件已在 .gitignore，保存时权限 600）。"
                  "填写并保存后直接生效；留空表示保持原值不变。"
                  "输入 __CLEAR__ 可清除已保存的 Key。",
                  "secret", default=""),
            Field("llm.vision", "发送画面",
                  "开启后每次请求附带一张直播截图；模型不支持图像时请关闭。",
                  "bool", default=True),
            Field("llm.max_tokens", "输出预算",
                  "含推理模型的「思考」token。思考等级越高越费预算；上限 65536，太小会正文为空。",
                  "int", minimum=64, maximum=65536, step=64, default=12288),
            Field("llm.thinking", "思考模式",
                  "auto=跟随服务端；enabled/disabled 显式开关。注意 thinking.type 只接受这两个值，"
                  "「思考等级」请用下面的 reasoning_effort。",
                  "enum", options=(("auto", "跟随服务端"), ("enabled", "开启思考"),
                                   ("disabled", "关闭思考")), default="auto"),
            Field("llm.reasoning_effort", "思考等级",
                  "DeepSeek 官方参数 reasoning_effort：none=关闭，low/high/max=开启并指定强度。"
                  "官方默认 high；网关若支持也会按这个字段识别。",
                  "enum", options=(("auto", "不设置（服务端默认）"), ("none", "none（关闭思考）"),
                                   ("low", "low（低）"), ("high", "high（高，官方默认）"),
                                   ("max", "max（极高）")), default="auto"),
            Field("llm.temperature", "随机性",
                  "越高越活泼发散，越低越稳。唠嗑建议 0.8～1.2。",
                  "float", minimum=0, maximum=2, step=0.1, default=1.0),
            Field("llm.timeout_sec", "请求超时",
                  "单次调用最长等待时间，推理模型建议 ≥60 秒。",
                  "float", minimum=5, maximum=300, step=5, default=60, unit="秒"),
            Field("llm.image_max_side", "图片最长边",
                  "截图会缩放到该尺寸再发送，越小越省 token。",
                  "int", minimum=256, maximum=1920, step=32, default=768, unit="像素", advanced=True),
            Field("llm.prompt_dir", "提示词根目录",
                  "存放 system.md / user.md 以及各套人设的目录，默认 prompts。",
                  "str", default="prompts"),
            Field("llm.lenient_parse", "宽松解析",
                  "模型返回非 JSON 时，把短文本直接当回复。",
                  "bool", default=True, advanced=True),
            Field("llm.extra_body", "额外请求参数",
                  '高级透传 JSON。一般参数请用上面的「思考模式 / 思考等级」；'
                  '注意 thinking.type 只接受 enabled/disabled，不要写 high。',
                  "dict", default={}, advanced=True),
        ],
    ),
    Group(
        id="app",
        title="运行与安全",
        desc="运行行为、日志与账号级保护。",
        fields=[
            Field("app.dry_run", "演练模式",
                  "开启后只把「本来要发的弹幕」写进日志，不真正发送。面板「运行控制」里的开关"
                  "默认在左边（即本项为 true），拨到右边才真实发送；切换后自动保存并重新加载 BOT。",
                  "bool", default=True),
            Field("app.global_min_interval_sec", "账号级最小间隔",
                  "任意两个房间之间两次发送的最小间隔，防止账号被判定异常。",
                  "float", minimum=0, maximum=600, step=1, default=20, unit="秒"),
            Field("app.log_level", "日志级别", "排查问题时用 DEBUG，平时 INFO。",
                  "enum", options=_LEVELS, default="INFO"),
            Field("app.auto_disable_muted", "被禁言时自动停用直播间",
                  "B 站 提示「你已经在本房间被禁言」时，自动把该直播间改成不启用并退出，"
                  "避免继续对着一个发不出话的房间跑。停用后想恢复：在面板「直播间」里重新勾选启用。"
                  "关掉这项则不停用：BOT 会照常按节奏继续尝试发送（每次失败记一条告警，"
                  "约每分钟一次），发送成功就说明禁言已解除，无需人工重启。",
                  "bool", default=True),
            Field("app.retention_days", "记录保留天数",
                  "决策、转写、发送记录和场次存档只留最近这么多天，更早的自动清理"
                  "（BOT 启动时清一次，之后每 6 小时一次）。填 0 = 不清理，一直保留。"
                  "日志不在此列，它按大小轮转。",
                  "int", minimum=0, maximum=3650, step=1, default=7, unit="天"),
            Field("app.log_dir", "日志目录", "运行日志保存位置。", "str", default="logs", advanced=True),
            Field("app.data_dir", "数据目录",
                  "转写、决策、发送记录与模型缓存都放在这里。",
                  "str", default="data", advanced=True),
        ],
    ),
    Group(
        id="bili",
        title="B 站账号",
        desc="登录态与请求参数。登录请在命令行执行 `python -m src.app login` 扫码。",
        fields=[
            Field("bili.cookie_file", "登录态文件", "扫码登录后 cookie 的保存位置。",
                  "str", default="data/cookies.json", advanced=True),
            Field("bili.user_agent", "User-Agent",
                  "请求头，一般不用改；风控严重时可换成自己浏览器的 UA。",
                  "str", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36", advanced=True),
            Field("bili.http_timeout_sec", "接口超时", "B 站接口请求的超时时间。",
                  "float", minimum=3, maximum=120, step=1, default=15, unit="秒", advanced=True),
        ],
    ),
    Group(
        id="context",
        title="上下文",
        desc="送给模型的语音与弹幕各取多久、多少条。窗口越大越懂上下文，也越贵。",
        fields=[
            Field("context.transcript_window_sec", "语音回看窗口",
                  "0 = 本场直播全程（推荐）；填秒数则只看最近 N 秒。",
                  "float", minimum=0, maximum=3600, step=10, default=0, unit="秒"),
            Field("context.danmaku_window_sec", "弹幕回看窗口",
                  "0 = 本场直播全程（推荐）；填秒数则只看最近 N 秒。",
                  "float", minimum=0, maximum=3600, step=10, default=0, unit="秒"),
            Field("context.history_danmaku", "补历史弹幕",
                  "首次连上直播时补几条历史弹幕（0=不补）。补的话可能把上一场最后几条带进来；"
                  "要求严格按场隔离就保持 0。",
                  "int", minimum=0, maximum=50, default=0, unit="条", advanced=True),
            Field("context.max_danmaku", "最多弹幕条数",
                  "整场最多带多少条弹幕进 Prompt；直播弹幕多，建议 60–100。",
                  "int", minimum=0, maximum=200, default=80, unit="条"),
            Field("context.max_chars", "上下文总预算",
                  "语音+弹幕的总字符上限；超出时保留最近内容并标注省略条数。"
                  "8000 约等于 6k token，按需增减。",
                  "int", minimum=500, maximum=20000, step=500, default=8000, unit="字符", advanced=True),
        ],
    ),
]

ROOM_FIELDS: Sequence[Field] = (
            Field("room_id", "房间号", "真实房间号或短号，保存时会用 B 站接口校验并归一化。", "int"),
    Field("alias", "别名", "显示用，例如「示例直播间」。", "str"),
    Field("enabled", "启用", "取消勾选后 BOT 不会监听/回复该房间。", "bool", default=True),
    Field("prompt_dir", "人设（提示词）",
          "默认=用 prompts/system.md；也可以选 prompts/persona.md 这类单文件人设，"
          "或 prompts/<名字>/ 目录人设。",
          "str"),
)

FIELD_INDEX: Dict[str, Field] = {f.key: f for group in GROUPS for f in group.fields}


def schema_dict() -> Dict[str, Any]:
    """给前端用的 JSON 结构。"""

    def field_dict(f: Field) -> Dict[str, Any]:
        return {
            "key": f.key,
            "label": f.label,
            "help": f.help,
            "type": f.type,
            "options": [{"value": v, "label": label} for v, label in f.options],
            "min": f.minimum,
            "max": f.maximum,
            "step": f.step,
            "unit": f.unit,
            "default": f.default,
            "advanced": f.advanced,
            "depends_on": (
                {"key": f.depends_on[0], "value": f.depends_on[1]}
                if f.depends_on else None
            ),
        }

    return {
        "groups": [
            {
                "id": g.id,
                "title": g.title,
                "desc": g.desc,
                "fields": [field_dict(f) for f in g.fields],
            }
            for g in GROUPS
        ],
        "room_fields": [field_dict(f) for f in ROOM_FIELDS],
    }
