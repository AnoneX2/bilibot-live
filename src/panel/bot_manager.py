# -*- coding: utf-8 -*-
"""在设置面板里启动 / 停止 / 重启 BOT 进程。

面板只是一个普通进程，BOT 是它的子进程（独立 session，面板退出后 BOT 继续跑）。
pid 记在 `data/panel_bot.pid`；如果 BOT 是别人（终端、systemd、botctl）启动的，
则通过 `data/runtime.json` 里的 pid 识别，同样能停掉。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..config import PROJECT_ROOT, Config
from ..runtime import pid_alive, read_runtime
from ..util.log import get_logger

logger = get_logger("panel.bot")

PID_FILENAME = "panel_bot.pid"
SYSTEMD_UNIT = "bilibot.service"
SYSTEMD_TIMEOUT_SEC = 30.0
START_GRACE_SEC = 2.5
STOP_TIMEOUT_SEC = 8.0
STOP_TIMEOUT_EXTRA_SEC = 12.0


def _psutil():
    try:
        import psutil  # noqa: F401

        return psutil
    except ImportError:  # pragma: no cover - 依赖已声明，缺失时降级
        return None


def _looks_like_bot(cmdline: List[str]) -> bool:
    text = " ".join(cmdline)
    return "src.app" in text and " run" in f" {text}"


def _run_cmd(args: List[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"命令不存在: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"超时: {' '.join(args)}"


def systemd_unit_present() -> bool:
    """装了 bilibot.service 就优先用 systemd 管理 BOT。"""
    rc, out = _run_cmd(["systemctl", "show", "-p", "LoadState", "--value", SYSTEMD_UNIT], timeout=5)
    return rc == 0 and out.strip() == "loaded"


class BotProcess:
    def __init__(self, cfg_path: Path, cfg_provider: Callable[[], Config]):
        self._cfg_path = Path(cfg_path)
        self._cfg_provider = cfg_provider
        # systemctl 是阻塞调用（负载高时能慢到几秒），面板每 3 秒轮询一次状态，
        # 每次都 fork 两三个 systemctl 会拖住事件循环 —— 缓存几秒。
        self._present_at = 0.0
        self._present_value = False
        self._state_at = 0.0
        self._state_value: Dict = {}

    # ---------------------------------------------------------------- 路径

    def _cfg(self) -> Config:
        return self._cfg_provider()

    @property
    def pid_file(self) -> Path:
        return self._cfg().data_path / PID_FILENAME

    @property
    def stdout_log(self) -> Path:
        return self._cfg().log_path / "bot.console.log"

    # ---------------------------------------------------------------- 状态

    def _pid_file_pid(self) -> int:
        try:
            data = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        try:
            return int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return 0

    def _live_procs(self) -> List:
        """当前真实的 BOT 进程列表（第一个是主进程）。"""
        psutil = _psutil()
        if psutil is None:
            return []
        wanted = [self._pid_file_pid()]
        try:
            runtime = read_runtime(self._cfg()) or {}
            wanted.append(int(runtime.get("pid") or 0))
        except Exception:  # noqa: BLE001 - 配置坏了也要能看状态
            pass

        procs = []
        seen = set()
        for pid in wanted:
            if not pid or pid in seen:
                continue
            try:
                proc = psutil.Process(pid)
                if _looks_like_bot(proc.cmdline()):
                    seen.add(pid)
                    procs.append(proc)
            except Exception:  # noqa: BLE001
                continue

        if not procs:
            # pid 文件没命中，再扫一遍进程表兜底
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if pid_alive(proc.pid) and _looks_like_bot(proc.info.get("cmdline") or []):
                        procs.append(proc)
                        break
                except Exception:  # noqa: BLE001
                    continue
        return procs

    # ---------------------------------------------------------------- systemd

    @property
    def managed_by_systemd(self) -> bool:
        now = time.time()
        if now - self._present_at > 5.0:
            self._present_value = systemd_unit_present()
            self._present_at = now
        return self._present_value

    def _unit_state(self, force: bool = False) -> Dict:
        if not force and time.time() - self._state_at < 2.0 and self._state_value:
            return dict(self._state_value)
        rc, out = _run_cmd(
            ["systemctl", "show", "-p", "ActiveState,SubState,MainPID,ExecMainStartTimestamp",
             SYSTEMD_UNIT], timeout=5,
        )
        data = {}
        if rc == 0:
            for line in out.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    data[key.strip()] = value.strip()
        pid = data.get("MainPID", "0")
        state = {
            "active": data.get("ActiveState", "unknown"),
            "sub": data.get("SubState", ""),
            "pid": int(pid) if str(pid).isdigit() else 0,
        }
        self._state_at = time.time()
        self._state_value = dict(state)
        return state

    def _sudo_systemctl(self, verb: str) -> tuple[int, str]:
        return _run_cmd(["sudo", "-n", "systemctl", verb, SYSTEMD_UNIT], timeout=SYSTEMD_TIMEOUT_SEC)

    def _wait_unit(self, want: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._unit_state(force=True)["active"] == want:
                return True
            time.sleep(0.3)
        return self._unit_state()["active"] == want

    def status(self) -> Dict:
        if self.managed_by_systemd:
            return self._status_systemd()
        return self._status_process()

    def _status_systemd(self) -> Dict:
        state = self._unit_state()
        running = state["active"] in ("active", "activating", "reloading")
        pid = state["pid"]
        uptime = 0.0
        if running and pid:
            psutil = _psutil()
            try:
                uptime = time.time() - psutil.Process(pid).create_time()
            except Exception:  # noqa: BLE001
                uptime = 0.0
        return {
            "running": running,
            "pid": pid,
            "started_at": (time.time() - uptime) if uptime else 0.0,
            "uptime_sec": uptime,
            "source": "systemd",
            "managed": "systemd",
            "active_state": state["active"],
            "pid_file": str(self.pid_file),
            "console_log": str(self.stdout_log),
        }

    def _status_process(self) -> Dict:
        procs = self._live_procs()
        if not procs:
            return {"running": False, "pid": 0, "uptime_sec": 0.0, "source": "",
                    "managed": "process",
                    "pid_file": str(self.pid_file), "console_log": str(self.stdout_log)}
        main = procs[0]
        try:
            started = main.create_time()
        except Exception:  # noqa: BLE001
            started = 0.0
        pid_from_file = self._pid_file_pid()
        return {
            "running": True,
            "pid": main.pid,
            "started_at": started,
            "uptime_sec": max(0.0, time.time() - started) if started else 0.0,
            "source": "panel" if pid_from_file == main.pid else "external",
            "managed": "process",
            "pid_file": str(self.pid_file),
            "console_log": str(self.stdout_log),
        }

    # ---------------------------------------------------------------- 启停

    def start(self) -> Dict:
        if self.managed_by_systemd:
            return self._start_systemd()
        current = self.status()
        if current["running"]:
            return {**current, "ok": True, "message": f"BOT 已在运行（pid={current['pid']}）"}

        cfg = self._cfg()
        cfg.ensure_dirs()
        self.stdout_log.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        # 静态 ffmpeg 装在 ~/.local/bin，面板从 GUI/launchd 启动时 PATH 可能没有
        extra = [str(Path.home() / ".local" / "bin"), str(Path(sys.executable).parent)]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [sys.executable, "-u", "-m", "src.app", "run", "--config", str(self._cfg_path)]
        logger.info("启动 BOT: %s", " ".join(cmd))
        # POSIX 用新会话，方便按进程组收尾；Windows 没有 SIGTERM，只有独立的
        # 进程组才能收到 CTRL_BREAK_EVENT（见 _request_stop），否则停止只能硬杀
        spawn: Dict[str, object] = {}
        if os.name == "nt":
            spawn["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn["start_new_session"] = True
        fh = open(self.stdout_log, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
                **spawn,
            )
        finally:
            fh.close()  # 子进程已继承句柄，父进程这份可以关掉

        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(
                json.dumps({"pid": proc.pid, "started_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写 pid 文件失败: %s", exc)

        # 给它一点时间加载模型/配置，退出就说明启动失败
        deadline = time.time() + START_GRACE_SEC
        while time.time() < deadline:
            rc = proc.poll()
            if rc is not None:
                self._remove_pid_file()
                tail = self.tail(self.stdout_log, 40)
                logger.error("BOT 启动失败（exit=%s）", rc)
                return {"ok": False, "running": False,
                        "message": f"启动失败（退出码 {rc}），请查看日志", "log_tail": tail}
            time.sleep(0.2)

        logger.info("BOT 已启动 pid=%s", proc.pid)
        return {"ok": True, "running": True, "pid": proc.pid, "message": "已启动"}

    @staticmethod
    def _request_stop(proc) -> None:
        """请求 BOT 正常收尾：POSIX=SIGTERM，Windows=CTRL_BREAK。

        Windows 上没有 SIGTERM，Popen.terminate() 走 TerminateProcess——
        直接杀进程，不执行任何 Python 收尾（关 ffmpeg、写场次存档）。所以那边
        改用 CTRL_BREAK_EVENT（start() 用 CREATE_NEW_PROCESS_GROUP 起了独立
        进程组），让 BOT 的 SIGBREAK handler 走正常退出；发不出去才退回硬杀。
        """
        if os.name != "nt":
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ValueError, OSError, AttributeError) as exc:
            logger.warning(
                "无法给 pid=%s 发 CTRL_BREAK（%s），只能强制结束：本场存档会丢失",
                getattr(proc, "pid", "?"), exc,
            )
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> Dict:
        if self.managed_by_systemd:
            return self._stop_systemd()
        psutil = _psutil()   # 下面几处 wait_procs 要用；缺失时降级为只发信号
        procs = self._live_procs()
        if not procs:
            self._remove_pid_file()
            return {"ok": True, "running": False, "message": "BOT 未在运行"}

        targets = list(procs)
        for proc in procs:
            try:
                targets.extend(proc.children(recursive=True))
            except Exception:  # noqa: BLE001
                pass

        # 先发「请收尾」信号：BOT 注册了 handler，会关 ffmpeg、写场次存档
        for proc in procs:
            self._request_stop(proc)

        alive = targets
        if psutil is not None:
            _gone, alive = psutil.wait_procs(targets, timeout=STOP_TIMEOUT_SEC)

        if alive and psutil is not None:
            # 正在加载 ASR 模型时进程可能一时收不到信号，多给一点时间
            logger.info("BOT 还在收尾，再等 %.0fs", STOP_TIMEOUT_EXTRA_SEC)
            _gone, alive = psutil.wait_procs(alive, timeout=STOP_TIMEOUT_EXTRA_SEC)

        if alive:
            # 再补一次收尾请求（第一次可能正好赶上模型加载/重连，被吞掉了）
            for proc in alive:
                self._request_stop(proc)
            if psutil is not None:
                _gone, alive = psutil.wait_procs(alive, timeout=2)

        if alive:
            logger.warning("BOT 未在 %.0fs 内退出，强制结束 %d 个进程（本场存档可能不完整）",
                           STOP_TIMEOUT_SEC + STOP_TIMEOUT_EXTRA_SEC, len(alive))
            for proc in alive:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            if psutil is not None:
                psutil.wait_procs(alive, timeout=3)

        self._remove_pid_file()
        logger.info("BOT 已停止（pid=%s）", procs[0].pid)
        return {"ok": True, "running": False, "message": "已停止",
                "killed": len(alive) if alive else 0}

    def restart(self) -> Dict:
        if self.managed_by_systemd:
            rc, out = self._sudo_systemctl("restart")
            if rc != 0:
                return {"ok": False, "running": self.status()["running"],
                        "message": f"重启失败（{rc}）：{out.strip()[:200]}"}
            self._wait_unit("active", timeout=20)
            state = self.status()
            return {"ok": True, "running": state["running"], "pid": state["pid"], "message": "已重启"}
        self.stop()
        time.sleep(0.6)
        return self.start()

    # ---------------------------------------------------------------- systemd 启停

    def _systemd_hint(self, rc: int, out: str) -> str:
        if rc in (1, 126, 127) and ("sudo" in out or "terminal" in out or "password" in out):
            return ("没有免密 sudo 权限：请按 README 安装 /etc/sudoers.d/bilibot "
                    "（或直接在服务器上跑 systemctl）")
        return f"{out.strip()[:200]}"

    def _start_systemd(self) -> Dict:
        state = self._unit_state()
        if state["active"] == "active":
            return {"ok": True, "running": True, "pid": state["pid"], "message": "BOT 已在运行"}
        rc, out = self._sudo_systemctl("start")
        if rc != 0:
            logger.error("systemctl start 失败: %s", out)
            return {"ok": False, "running": False, "message": f"启动失败：{self._systemd_hint(rc, out)}"}
        ok = self._wait_unit("active", timeout=20)
        if not ok:
            tail = self.tail(self.stdout_log, 40)
            return {"ok": False, "running": False,
                    "message": "启动失败：服务没有进入 active，请看日志 / journalctl -u bilibot",
                    "log_tail": self.tail(self._cfg().log_path / "bot.log", 40) or tail}
        state = self._unit_state()
        return {"ok": True, "running": True, "pid": state["pid"], "message": "已启动（systemd）"}

    def _stop_systemd(self) -> Dict:
        state = self._unit_state()
        if state["active"] == "inactive":
            return {"ok": True, "running": False, "message": "BOT 未在运行"}
        rc, out = self._sudo_systemctl("stop")
        if rc != 0:
            logger.error("systemctl stop 失败: %s", out)
            return {"ok": False, "running": state["active"] == "active",
                    "message": f"停止失败：{self._systemd_hint(rc, out)}"}
        self._wait_unit("inactive", timeout=30)
        return {"ok": True, "running": False, "message": "已停止（systemd）"}

    # ---------------------------------------------------------------- 工具

    def _remove_pid_file(self) -> None:
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    @staticmethod
    def tail(path: Path, lines: int = 200) -> List[str]:
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return text.splitlines()[-lines:]
