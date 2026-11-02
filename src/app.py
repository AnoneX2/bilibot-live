# -*- coding: utf-8 -*-
"""命令行入口。

  python -m src.app run     --config config.yaml [--dry-run|--live] [--room 123] [--login]
  python -m src.app check   --config config.yaml [--room 123]
  python -m src.app login   --config config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import __version__
from .ai.prompt_store import (
    PromptError,
    PromptStore,
    format_danmaku,
    format_timeline,
    format_transcript,
)
from .ai.provider import create_provider
from .asr import XASREngine
from .bili.api import BiliAPI
from .bili.auth import ensure_login, load_cookies
from .bili.session import create_session
from .config import (
    PROJECT_ROOT,
    Config,
    RoomSpec,
    default_config_path,
    load_config,
    mask_secret,
)
from .core.guard import GlobalRateLimiter
from .core.room_worker import RoomWorker
from .diagnostics import (
    check_asr_assets,
    check_cookies,
    check_llm_config,
    check_prompts,
    probe_interfaces,
)
from .reload import RELOAD_EXIT_CODE, config_diff, write_state as write_reload_state
from .retention import PRUNE_INTERVAL_SEC, prune as prune_records
from .runtime import write_runtime
from .util.log import get_logger, setup_logging

logger = get_logger("app")


def _prepare(args) -> Config:
    cfg = load_config(args.config)
    # 热加载 diff 的基准（file_snapshot）由 load_config 在套用命令行/环境变量
    # 覆盖「之前」就记好了：否则 --mock/--dry-run/BOT_DRY_RUN 覆盖过的字段
    # 会被误报成「配置变了」，或者反过来改了文件却 diff 为空、不触发重载。
    if getattr(args, "room", None):
        cfg.app.rooms = [RoomSpec(room_id=int(args.room))]
    cfg.ensure_dirs()
    setup_logging(cfg.app.log_level, cfg.log_path)

    # 命令行开关覆盖配置时明确告知，避免「面板开了演练模式却仍真实发送」
    if getattr(args, "dry_run", False) and not cfg.app.dry_run:
        logger.warning("命令行 --dry-run 覆盖了配置中的 dry_run=false")
        cfg.app.dry_run = True
    if getattr(args, "live", False) and cfg.app.dry_run:
        logger.warning("命令行 --live 覆盖了配置中的 dry_run=true，将真实发送弹幕")
        cfg.app.dry_run = False
    if getattr(args, "mock", False):
        logger.warning("命令行 --mock：使用本地假回复，不调用真实模型")
        cfg.llm.provider = "mock"
    return cfg


def _print_probes(probes) -> None:
    width = max(len(p.name) for p in probes) + 2
    for probe in probes:
        if probe.ok:
            mark = "[ OK ]"
        else:
            mark = "[FAIL]" if probe.critical else "[WARN]"
        print(f"  {mark} {probe.name:<{width}} {probe.detail}  ({probe.elapsed_ms:.0f}ms)")


# ---------------------------------------------------------------- api key


# ---------------------------------------------------------------- 配置热加载

CONFIG_POLL_SEC = 1.0
CONFIG_SETTLE_SEC = 0.5


def _config_signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


async def _watch_config(cfg: Config, stop_event: asyncio.Event) -> bool:
    """轮询配置文件；变更且校验通过后写状态、请求重启。

    返回值 True 表示「配置已变更，需要重新加载」。
    """
    path = Path(cfg.config_path)
    last = _config_signature(path)
    while not stop_event.is_set():
        await asyncio.sleep(CONFIG_POLL_SEC)
        signature = _config_signature(path)
        if signature is None or signature == last:
            continue
        # 等文件写稳定，避免读到面板写到一半的内容
        await asyncio.sleep(CONFIG_SETTLE_SEC)
        if _config_signature(path) != signature:
            last = signature
            continue
        last = signature
        try:
            # 只比「文件内容」：解析时不再套用环境变量覆盖，否则
            # BOT_DRY_RUN=true 这类启动期覆盖会污染 diff（改了文件却不触发重载）
            new_cfg = load_config(str(path), apply_env=False)
        except ValueError:
            # 极端情况：配置靠环境变量补全（如 BOT_ROOMS），文件本身校验不过
            try:
                new_cfg = load_config(str(path))
            except Exception as exc:  # noqa: BLE001
                write_reload_state(cfg, changed=[], error=str(exc)[:500])
                logger.error("配置文件有误，继续使用旧配置：%s", exc)
                continue
        except Exception as exc:  # noqa: BLE001
            write_reload_state(cfg, changed=[], error=str(exc)[:500])
            logger.error("配置文件有误，继续使用旧配置：%s", exc)
            continue
        baseline = getattr(cfg, "file_snapshot", None) or cfg.to_dict()
        changed = config_diff(baseline, new_cfg.to_dict())
        if not changed:
            logger.info("配置文件已保存，但内容没有变化，忽略")
            continue
        write_reload_state(cfg, changed=changed)
        logger.warning(
            "检测到配置变更（%d 项）：%s；准备自动重启加载新配置",
            len(changed),
            "、".join(changed[:12]) + ("…" if len(changed) > 12 else ""),
        )
        stop_event.set()
        return True
    return False


async def _retention_loop(cfg: Config, stop_event: asyncio.Event) -> None:
    """每 6 小时清一次过期记录（启动时已经清过一遍，见 _run_async）。"""
    interval = max(600.0, float(PRUNE_INTERVAL_SEC))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            # 放线程里：文件可能不小，别卡住事件循环
            await asyncio.to_thread(prune_records, cfg)
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响直播
            logger.warning("清理旧记录失败（不影响运行）: %s", exc)


# ---------------------------------------------------------------- run


def _hard_exit_after(seconds: float = 6.0) -> None:
    """优雅收尾已经做完，但解释器可能被模型/推理线程拖住，兜底强制退出。"""

    def _exit() -> None:
        try:
            logging.shutdown()  # 把日志 handler 的缓冲刷掉
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    timer = threading.Timer(seconds, _exit)
    timer.daemon = True
    timer.start()


def _stop_signals() -> Tuple[int, ...]:
    """要接管成「正常收尾」的信号。

    SIGTERM：POSIX 上 kill / systemd stop / 面板 stop 的默认信号；
    SIGBREAK：Windows 上没有 SIGTERM，面板用 CTRL_BREAK_EVENT 通知收尾。
    """
    signals = [signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    return tuple(signals)


def _install_stop_signal(stop_event: asyncio.Event) -> None:
    """把停止信号转成正常收尾（关 ffmpeg、写场次存档）。

    不是所有环境都能注册 asyncio 的 signal handler（Windows、非主线程），
    退化成 signal.signal；再不行就只能靠默认行为。
    """

    def _request_stop(*_args: object) -> None:
        stop_event.set()
        _hard_exit_after()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    for sig in _stop_signals():
        if loop is not None:
            try:
                loop.add_signal_handler(sig, _request_stop)
                continue
            except (NotImplementedError, RuntimeError, AttributeError, ValueError):
                pass
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError, RuntimeError):
            pass


async def _run_async(cfg: Config, login: bool) -> int:
    logger.info("bilibot %s 启动：%s", __version__, cfg.summary())
    if cfg.app.dry_run:
        logger.warning("演练模式：只记录「将要发送」的内容，不会真的发弹幕")
    else:
        logger.warning("真实发送模式：会向直播间发送弹幕！如需演练请把 app.dry_run 改为 true")
    # API Key 只从 config.yaml 读（面板里填），不再走终端输入 / 环境变量。
    if cfg.llm.provider == "deepseek" and not cfg.api_key():
        logger.error(
            "缺少 API Key：请运行 `python -m src.app panel`，在「大模型 → API Key」填写并保存。"
            "联调可用 --mock。"
        )
        return 2
    if cfg.llm.provider == "deepseek":
        logger.info("API Key：%s（来自 config.yaml）", mask_secret(cfg.api_key()))

    # 启动前校验提示词，避免跑到一半才发现文件缺失/占位符写错
    for spec in cfg.active_rooms():
        try:
            store = PromptStore(cfg, prompt_dir=spec.prompt_dir, room_id=spec.room_id)
        except PromptError as exc:
            logger.error("人设不可用（房间 %s）：%s", spec.display(), exc)
            return 2
        errors = store.validate()
        if errors:
            logger.error("提示词校验失败（%s）：%s", store.dir, "；".join(errors))
            logger.error("请检查 prompts/system.md 与 prompts/user.md，或用 `python -m src.app check` 查看")
            return 2
    logger.info("提示词目录：%s", cfg.path(cfg.llm.prompt_dir))

    asr = XASREngine(cfg)
    problems = asr.check_assets()
    if problems:
        for item in problems:
            logger.error("ASR 不可用：%s", item)
        return 2
    logger.info("ASR 引擎：%s", asr.describe())

    active = cfg.active_rooms()
    if not active:
        logger.error("没有启用的直播间。请在设置面板里勾选启用，或修改 config.yaml 的 app.rooms[].enabled")
        return 2
    if len(active) != len(cfg.app.rooms):
        logger.info("已跳过 %d 个未启用的直播间", len(cfg.app.rooms) - len(active))

    write_runtime(cfg)
    logger.info("运行标记已写入，设置面板可据此提示「配置是否已重启生效」")

    if os.name == "nt":
        # Windows 上 chmod 只改只读位，不会设置 ACL，等同没有保护
        logger.warning(
            "Windows 无法用 chmod 收紧权限：data/cookies.json 与 config.yaml 等同于"
            "账号与 API Key，请勿同步到网盘或打包分享"
        )

    # 启动时先清一次旧记录：此刻还没有任何并发写入，可以放开手清
    try:
        await asyncio.to_thread(prune_records, cfg, None, True)
    except Exception as exc:  # noqa: BLE001 - 清理失败不能拦住启动
        logger.warning("清理旧记录失败（不影响运行）: %s", exc)

    cookies = load_cookies(cfg.cookie_path)
    session = await create_session(cfg, cookies)
    provider = None
    stop_event = asyncio.Event()
    _install_stop_signal(stop_event)
    tasks: List[asyncio.Task] = []
    warmup: Optional[asyncio.Task] = None
    watcher: Optional[asyncio.Task] = None
    retention: Optional[asyncio.Task] = None
    workers_done = None
    reload_requested = False
    try:
        is_login, uname, mid = await ensure_login(session, cfg, interactive=login)
        if not is_login:
            logger.warning("未登录：能听能看，但无法发送弹幕（发送会被拒绝）")

        api = BiliAPI(session, cfg)
        api.uid, api.uname = mid, uname
        provider = create_provider(cfg)

        # 后台预热 ASR，避免第一句话等十几秒
        warmup = asyncio.create_task(asr.warmup())

        limiter = GlobalRateLimiter(cfg)
        workers = [
            RoomWorker(
                cfg,
                spec,
                api=api,
                session=session,
                asr=asr,
                provider=provider,
                global_limiter=limiter,
                stop_event=stop_event,
            )
            for spec in active
        ]
        tasks = [asyncio.create_task(w.run(), name=f"room-{w.label}") for w in workers]
        logger.info("已启动 %d 个直播间 worker", len(workers))
        watcher = asyncio.create_task(_watch_config(cfg, stop_event), name="config-watch")
        retention = asyncio.create_task(_retention_loop(cfg, stop_event), name="retention")
        workers_done = asyncio.gather(*tasks)
        done, _pending = await asyncio.wait(
            {workers_done, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        if watcher in done and not watcher.cancelled():
            reload_requested = bool(watcher.result())
        elif workers_done in done:
            # worker 异常退出时保持原来的行为：把异常抛出来
            workers_done.result()
    except asyncio.CancelledError:
        return 0
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # gather 的那个 future 自己不取消的话，退出时会打印
        # "_GatheringFuture exception was never retrieved"，看着像崩溃
        if workers_done is not None:
            workers_done.cancel()
            try:
                await workers_done
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if watcher is not None and not watcher.done():
            watcher.cancel()
        if retention is not None and not retention.done():
            retention.cancel()
        if warmup is not None and not warmup.done():
            warmup.cancel()
        if provider is not None:
            await provider.close()
        asr.close()
        await session.close()
        logger.info("已退出")
    if reload_requested:
        return RELOAD_EXIT_CODE
    return 0


def _reload_process() -> None:
    """配置热加载：Linux 上换掉进程映像（PID 不变），而不是在同一进程里再跑一轮。

    注意：Windows 的 os.execv 是「spawn 新进程 + 退出当前进程」，PID 会变，
    那里靠 runtime.json/pid 文件短期失真、panel 用进程表兜底来自愈。

    进程内重启时，旧的 ASR recognizer 不会立刻归还给系统（onnxruntime 的
    arena 分配 + 残留引用），每重载一次就多占约 600MB，两次就把内存/swap 打满，
    面板和隧道跟着卡死。execv 之后内存彻底回收，代价是重新 import + 加载模型，
    与「手动停止再启动」等价。
    """
    argv = list(getattr(sys, "orig_argv", []) or [])
    if not argv:
        argv = [sys.executable, "-m", "src.app", *sys.argv[1:]]
    argv[0] = sys.executable
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
        os.execv(sys.executable, argv)
    except OSError as exc:  # noqa: BLE001 - 换映像失败就退回进程内重启
        logger.warning("重新拉起进程失败（%s），改为进程内重启", exc)


def cmd_run(args) -> int:
    while True:
        cfg = _prepare(args)
        try:
            rc = asyncio.run(_run_async(cfg, login=args.login))
        except KeyboardInterrupt:
            print("\n收到中断，正在退出……")
            return 0
        if rc != RELOAD_EXIT_CODE:
            return rc
        print("\n配置已变更，正在重新加载 BOT（几秒）……")
        time.sleep(0.3)
        _reload_process()


# ---------------------------------------------------------------- check


async def _check_async(cfg: Config, room: int) -> int:
    print(f"\nbilibot {__version__}  配置: {cfg.config_path}")
    print(f"  {cfg.summary()}\n")

    probes = [check_cookies(cfg), check_asr_assets(cfg), check_llm_config(cfg)]
    probes.append(
        check_prompts(
            cfg,
            prompt_dir=cfg.app.rooms[0].prompt_dir if cfg.app.rooms else "",
        )
    )

    cookies = load_cookies(cfg.cookie_path)
    session = await create_session(cfg, cookies)
    try:
        api = BiliAPI(session, cfg)
        probes.extend(await probe_interfaces(cfg, api, room))
    finally:
        await session.close()

    print("体检结果：")
    _print_probes(probes)

    failed = [p for p in probes if not p.ok and p.critical]
    warns = [p for p in probes if not p.ok and not p.critical]
    print()
    if warns:
        print(f"{len(warns)} 项警告（不阻塞启动，但影响体验）：")
        for probe in warns:
            print(f"  - {probe.name}: {probe.detail}")
    if failed:
        print(f"{len(failed)} 项核心检查失败：")
        for probe in failed:
            print(f"  - {probe.name}: {probe.detail}")
        return 1
    print("核心检查通过 ✅" if not warns else "核心检查通过（有警告）")
    return 0


def cmd_check(args) -> int:
    cfg = _prepare(args)
    room = int(args.room) if args.room else (cfg.app.rooms[0].room_id if cfg.app.rooms else 0)
    if not room:
        print("请在配置里填写 app.rooms 或使用 --room 指定直播间")
        return 2
    return asyncio.run(_check_async(cfg, room))


# ---------------------------------------------------------------- login


async def _login_async(cfg: Config) -> int:
    cookies = load_cookies(cfg.cookie_path)
    session = await create_session(cfg, cookies)
    try:
        ok, uname, mid = await ensure_login(session, cfg, interactive=True)
        if ok:
            print(f"登录成功：{uname} (mid={mid})，cookie 已保存到 {cfg.cookie_path}")
            return 0
        print("登录失败")
        return 1
    finally:
        await session.close()


def cmd_login(args) -> int:
    cfg = _prepare(args)
    return asyncio.run(_login_async(cfg))


# ---------------------------------------------------------------- prompt


def cmd_prompt(args) -> int:
    """展示当前实际发送的 Prompt（含人设/风格），方便直接改文件。"""
    cfg = _prepare(args)
    room = cfg.app.rooms[0] if cfg.app.rooms else RoomSpec(room_id=0)
    prompt_dir = args.prompt_dir or room.prompt_dir
    try:
        store = PromptStore(cfg, prompt_dir=prompt_dir, room_id=room.room_id)
    except PromptError as exc:
        print(f"人设不可用：{exc}")
        return 1

    errors = store.validate()
    print(f"人设：{store.profile_label}")
    print(f"system：{store.system_path}")
    print(f"user  ：{store.user_path}")
    if errors:
        print("校验失败：")
        for err in errors:
            print(f"  - {err}")
        return 1

    if args.raw:
        print("\n" + "=" * 24 + f" {store.system_path.name} " + "=" * 24)
        print(store.raw("system"))
        print("\n" + "=" * 24 + f" {store.user_path.name} " + "=" * 24)
        print(store.raw("user"))
        return 0

    now = time.time()
    sample_transcript = [
        (now - 12, "这波我们先推线，等他们过来再打"),
        (now - 4, "对面这个打野一直蹲下路，得小心一点"),
    ]
    sample_danmaku = ["观众A: 这波操作可以啊", "观众B: 感觉要翻盘了"]
    # 语音 + 弹幕合成示例时间线（按时间排序，和线上一致）
    sample_timeline = sorted(
        [(ts, "speech", text) for ts, text in sample_transcript]
        + [(now - 30, "danmaku", line) for line in sample_danmaku],
        key=lambda item: item[0],
    )
    print("\n" + "=" * 24 + " 渲染后的 system prompt " + "=" * 24)
    print(store.system())
    print("\n" + "=" * 24 + " 渲染后的 user prompt（示例） " + "=" * 24)
    print(
        store.user(
            title="示例直播间",
            alias=room.alias or "",
            bot_name="示例机器人",
            alias_part=f"（{room.alias}）" if room.alias else "",
            clock=time.strftime("%H:%M:%S", time.localtime(now)),
            frame_desc="已附带一帧直播截图（约 3 秒前），可参考画面内容。",
            timeline=format_timeline(sample_timeline, 800),
            transcript=format_transcript(sample_transcript, now, 600),
            danmaku=format_danmaku(sample_danmaku, 300),
            trigger="主播刚说了：「对面打野一直蹲下路」，请判断要不要接一句",
            room_id=room.room_id,
        )
    )
    print("\n提示：直接编辑上面两个 .md 文件即可，保存后运行中的 BOT 会自动重新加载。")
    return 0


# ---------------------------------------------------------------- panel


def cmd_panel(args) -> int:
    """启动本地设置面板（只监听 127.0.0.1）。"""
    from .panel.server import run_panel

    # 没有 config.yaml 时由 default_config_path() 从模板复制一份，
    # 不会拿 Git 跟踪的 config.example.yaml 当配置写（Key 会被提交上去）
    config_path = args.config or str(default_config_path())
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    # 面板与 BOT 分开写日志：两个进程同时轮转同一个文件会互相踩
    setup_logging("INFO", PROJECT_ROOT / "logs", filename="panel.log")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"警告：绑定到 {args.host} 会允许局域网访问，请确认网络环境安全")
    try:
        asyncio.run(
            run_panel(
                host=args.host,
                port=args.port,
                config_path=config_path,
                open_browser=not args.no_open,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("\n设置面板已停止")
        return 0


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bilibot", description="直播内容理解驱动的自动弹幕回复 BOT")
    parser.add_argument("--version", action="version", version=f"bilibot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="启动 BOT")
    run.add_argument("--config", default=None)
    run.add_argument("--room", default=None, help="只跑指定直播间，覆盖配置")
    run.add_argument("--dry-run", action="store_true", help="只记录不发送")
    run.add_argument("--live", action="store_true", help="强制真实发送（覆盖配置）")
    run.add_argument("--mock", action="store_true", help="用 Mock Provider 联调，不调用真实模型")
    run.add_argument("--login", action="store_true", help="启动前强制扫码登录")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="接口与依赖体检")
    check.add_argument("--config", default=None)
    check.add_argument("--room", default=None)
    check.set_defaults(func=cmd_check)

    login = sub.add_parser("login", help="扫码登录并保存 cookie")
    login.add_argument("--config", default=None)
    login.set_defaults(func=cmd_login)

    prompt = sub.add_parser("prompt", help="展示/检查当前发送给模型的提示词")
    prompt.add_argument("--config", default=None)
    prompt.add_argument("--raw", action="store_true", help="打印原始 md 文件（含占位符）")
    prompt.add_argument("--prompt-dir", default=None,
                        help="指定人设：目录名、单文件（persona.md）或完整路径")
    prompt.set_defaults(func=cmd_prompt)

    panel = sub.add_parser("panel", help="启动本地设置面板（浏览器里改配置）")
    panel.add_argument("--config", default=None)
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8765)
    panel.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    panel.set_defaults(func=cmd_panel)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台可能是 GBK，避免 emoji/生僻字导致进程崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件缺失: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
