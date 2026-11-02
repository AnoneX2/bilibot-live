# Bili-live-ai

 B 站直播 AI 弹幕机器人

> 请只在**自己有权互动的直播间**使用，并遵守 B 站社区规则与当地法律法规。
> 默认 `dry_run: true`，先观察、再考虑真实发送。

## 特性

- **对主播说话进行回应**：语音触发判断。
- **看画面 + 听声音**：X-ASR 流式/逐句语音识别 + 定时截图。
- **两档识别模式**：`stream` 边说边出字、延迟低；`segment` 用 VAD 断句后整句识别，能挡掉纯 BGM。
- **整场上下文**：默认把本场直播的语音转写和最多 80 条弹幕放进 Prompt（超预算时保留最近内容并标注省略），
  直播结束后上下文清空，按场隔离。
- **多种安全闸门**：冷却、每小时/每分钟配额、静默时段、黑名单、去重、置信度门槛。
- **设置面板**：本地网页配置参数。
- **配置热加载**：保存 `config.yaml` 后自动重启加载（约几秒），不必手动重启 BOT。
- **演练模式**：`dry_run: true` 时只记录「将要发送」的内容，不发弹幕。

## 工作原理

```
直播流 ──ffmpeg──► PCM 16kHz ──VAD──► 句子 ──X-ASR──► 转写文本 ─┐
直播流 ──ffmpeg──► JPEG 截图（可配置间隔）──────────────────────┤
弹幕 WebSocket ─────────────────────────► 弹幕（氛围；被 @ 时额外触发）─┤
                                                                ▼
                                              组装 Prompt（本场语音+弹幕+画面）
                                                                │
                                     主播说话触发 ──────────────┤
                                                                ▼
                                             LLM（OpenAI 兼容接口）→ JSON 决策
                                                                │
                                  ReplyGuard（长度/黑名单/去重/配额/静默）
                                                                │
                                                    发送弹幕 / 记录 skip
```

## 环境要求

- Python 3.10+
- ffmpeg
- Windows 、Linux
- 一个 OpenAI 兼容的大模型接口（默认 DeepSeek）
- 默认使用X-ASR 模型（见 [MODELS.md](MODELS.md)）

## 安装

```bash
git clone https://github.com/AnoneX2/bilibot-live.git
cd bilibot-live

python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
cp .env.example .env        # 可选：看里面注释，程序不会自动读 .env
```

下载语音模型（仓库不包含模型文件）：见 [MODELS.md](MODELS.md)，把
`chunk-1920ms-model`（或全部四档）放到 `data/models/xasr/` 下。

## 快速开始

1. 打开面板

   ```bash
   Linux：
   	python -m src.app panel        # 打开设置面板 http://127.0.0.1:8765
   Windows：
   	双击 start_panel.bat
   ```

2. 登录 B 站（扫码登录，cookie 保存在 `data/cookies.json`）

3. 设置 API Key：在面板「大模型 → API Key」里粘贴 Key，点旁边的「保存 Key」。

4. 添加直播间号（短号/真实房间号均可）。

5. 调整设置（回复策略、提示词等）。

6. 启动bot(默认演练模式，不发送弹幕)，先跑一段时间看日志。

7. 观察日志，确认语气、频率、触发都合适。

8. 确认没问题后，把「运行控制」里那个开关拨到右边「真实发送模式」（默认在左边是演练模式）。

### 设置面板

```bash
python -m src.app panel
```

面板提供：

| 面板左侧选项 | 对应功能 |
|---|---|
| 运行控制 | 扫码登录 B 站（显示当前账号与 UID）、启动/停止/重启 BOT、演练模式开关、实时日志（与终端输出一致） |
| 直播间 | 添加/删除房间、启用开关、别名、独立人设，并显示主播昵称与开播状态；可预览该房间实际 Prompt |
| 回复策略 | 触发模式、回复字数上限、冷却与每小时/每分钟配额、静默时段、黑名单、去重、置信度门槛、被 @ 回应、主播 /mute 指令开关 |
| 语音识别（ASR） | 识别方式（stream / segment）、X-ASR 分块与线程数、断句等待、幻觉文本过滤 |
| 断句（VAD） | 音量阈值、噪声底倍数、静音判定时长、最短/最长语音、句首保留 |
| 直播流与画面 | 抓帧间隔与画面有效期、拉流清晰度、断流判定、播放地址刷新 |
| 大模型 | API Key（保存到配置文件、一键测连通）、接口地址、模型名、思考模式与等级、输出预算、超时 |
| 运行与安全 | 账号级最小发送间隔、被禁言时自动停用直播间、记录保留天数、日志级别、日志目录、数据目录 |
| B 站账号 | 登录态文件位置、User-Agent、接口超时 |
| 上下文 | 语音/弹幕回看窗口、弹幕条数、字符预算、是否补历史弹幕 |
| 提示词 | 编辑 / 新建人设、校验占位符、预览实际渲染结果 |
| 体检与诊断 | 登录态、接口连通性、模型文件、提示词、API Key 体检 |
| 运行状态 | 完整决策历史（按房间、按场次切换，可逐段加载更早的记录）、上次热加载时间与变更字段 |

改完点右上角「保存配置」：BOT 会在 1–2 秒内自动重启加载（进程内重启，直播流重连、ASR 重新加载，通常几秒）。

#### 面板里的完整流程

```
打开面板 → 「运行控制」里扫码登录 B 站 → 「大模型」里填 API Key 并保存
        → 回到「运行控制」点「启动 BOT」 → 下方实时日志滚动
```

## 运行命令

| 命令 | 行为 |
|---|---|
| `python -m src.app check` | 配置 + 登录 + 接口 + ASR 模型 + 提示词 + Key 体检 |
| `python -m src.app panel` | 启动本地设置面板 |
| `python -m src.app login` | 终端二维码扫码登录，保存 cookie |
| `python -m src.app prompt` | 打印实际渲染后的 Prompt（`--raw` 看原始文件） |
| `python -m src.app run --dry-run` | 全链路运行，回复只写日志 |
| `python -m src.app run --live` | 全链路运行并真实发送 |
| `python -m src.app run --mock` | 不调用真实模型，用假回复联调 |

`--room` 可临时覆盖配置里的直播间，`--config` 指定其他配置文件。

## 配置要点

完整字段与中文注释见 `config.example.yaml`，面板里的说明更全。最常改的：

| 配置 | 说明 | 默认 |
|---|---|---|
| `app.rooms` | 直播间列表（房间号、别名、是否启用、独立人设） | 空 |
| `app.dry_run` | 只记录不发送 | `true` |
| `llm.base_url` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `llm.model` | 模型名（不支持图像输入时关掉「发送画面」） | `deepseek-flash` |
| `llm.thinking` | 思考开关：`auto` / `enabled` / `disabled` | `auto` |
| `llm.reasoning_effort` | 思考等级：`auto` / `none` / `low` / `high` / `max` | `auto` |
| `llm.max_tokens` | 输出预算（含思考 token，上限 65536） | `12288` |
| `asr.mode` | `stream`（边说边出字）/ `segment`（VAD 断句） | `segment` |
| `asr.xasr_chunk_ms` | X-ASR 分块：160/480/960/1920，越大越准 | `1920` |
| `asr.xasr_threads` | 识别线程数 | `4` |
| `reply.mode` | `speech` / `ambient` / `off` | `speech` |
| `reply.max_per_hour` | 每房间每小时发送上限 | `60` |
| `reply.confidence_gate` | 置信度低于阈值就不发 | `false` |
| `reply.mention_enabled` | 弹幕里被 @ 时是否允许回一句 | `true` |
| `reply.mention_min_interval_sec` | 两次 @ 回应的最小间隔（秒） | `120` |
| `reply.mention_max_per_hour` | 每小时因被 @ 最多回应几次 | `8` |
| `context.max_danmaku` | 本场最多带多少条弹幕 | `80` |
| `context.max_chars` | 语音+弹幕总字符预算 | `8000` |

## 提示词与人设

`prompts/` 下是发给模型的完整 Prompt：

- `system.md`：默认人设（角色、语气、输出格式、硬性规则）；
- `user.md`：动态内容模板，用 `{{变量}}` 占位；
- 单文件人设：`prompts/<名字>.md`，只作为 system，共用 `user.md`；
- 目录人设：`prompts/<名字>/system.md` + `user.md`。

面板「提示词」页可以直接新建、编辑、预览；保存后运行中的 BOT 自动重新加载。
字段与占位符说明见 [prompts/README.md](prompts/README.md)。

## 语音识别

- `asr.mode: segment`（默认）：VAD 断句后整句识别，能挡掉纯 BGM/歌声，代价是多等一次静音确认。
- `asr.mode: stream`：真流式，边说边出字，延迟最低，但背景音乐/歌声也会被转写。
- `asr.xasr_chunk_ms` 决定用哪一档模型；四档都放在模型目录里时可以随时切。
- 模型下载与目录结构见 [MODELS.md](MODELS.md)。

## 目录结构

```
src/
  app.py              CLI 入口（run/check/login/panel/prompt）
  config.py           配置模型与校验
  reload.py           配置热加载（diff/状态）
  runtime.py          运行标记（给面板看）
  settings_schema.py  面板字段元数据
  ai/                 大模型调用、Prompt 加载与渲染
  asr/                X-ASR 引擎与识别后处理
  bili/               B 站接口、登录、弹幕、WBI 签名
  core/               上下文、回复引擎、频率/内容守卫、房间编排
  live/               拉流、音频、截图
  panel/              本地设置面板（aiohttp + 原生 JS）
blivedm/              第三方 B 站直播协议库（MIT，见声明）
prompts/              人设与 Prompt 模板
config.example.yaml   配置模板
MODELS.md             本地模型下载索引
```

运行产物都在 `data/` 与 `logs/`：cookie、模型、转写、决策、发送记录、
场次存档 `data/sessions/*.json`、日志。删除 `data/` 不会影响代码，但会丢失登录态和统计。

> **`data/` 与 `config.yaml` 视同密钥**：`data/cookies.json` 里的 `SESSDATA`/`bili_jct`
> 等价于 B 站登录态，`config.yaml` 里有 LLM API Key。不要打包分享、不要同步到网盘。
> Linux 上保存时会 `chmod 600`；Windows 上 chmod 不生效（不设 ACL），需自行确认目录权限。

其中转写、决策、发送记录和场次存档是只追加、不轮转的，所以默认只保留最近 7 天
（`app.retention_days`，面板「运行与安全 → 记录保留天数」可改，填 0 = 一直保留）：
BOT 启动时清一次，之后每 6 小时清一次，过期的行会从 jsonl 里删掉、过期的场次存档
直接删除。日志不在此列——它按大小轮转（8MB × 6）。

## 第三方

- [blivedm](https://github.com/xfgryujk/blivedm)（MIT）：B 站直播协议客户端，见 `blivedm/LICENSE`；
- [X-ASR](https://huggingface.co/GilgameshWind/X-ASR-zh-en)（Apache-2.0）：语音识别模型；
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（Apache-2.0）：ONNX 推理运行时。

详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可

本项目使用 MIT 许可证，Copyright (c) 2026 AnoneX2，见 [LICENSE](LICENSE)。
内嵌的 `blivedm/` 为第三方 MIT 代码（Copyright (c) 2018 xfgryujk），其许可证全文在 `blivedm/LICENSE`。

请只在有权互动的直播间使用，并自行承担账号与内容合规责任。
