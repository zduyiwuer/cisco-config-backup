# -*- coding: utf-8 -*-
"""后台服务管理：启动 / 停止 / 查看状态。

被「后台启动服务.bat」与「关闭服务.bat」调用，也可单独使用：

    python scripts/daemonctl.py start
    python scripts/daemonctl.py stop
    python scripts/daemonctl.py status

设计要点
- PID 写在 data/service.pid，关停时精确 kill，不靠端口猜
- 用 pythonw 启动，无控制台窗口
- 先确认就绪再返回，不让 bat 干等
- 停不掉时降级为按端口查进程，避免 PID 文件过期导致关不掉
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("CBS_ROOT") or Path(__file__).resolve().parent.parent)
DATA_DIR = Path(os.environ.get("CBS_DATA_DIR") or ROOT / "data")
PID_FILE = DATA_DIR / "service.pid"
LOG_FILE = DATA_DIR / "service.log"
PORT = int(os.environ.get("CBS_PORT", "8077"))
# 解释器路径可覆盖，便于测试时指向真实 venv
PYTHON = os.environ.get("CBS_PYTHON") or str(
    ROOT / ".venv" / "Scripts" / ("pythonw.exe" if os.name == "nt" else "python")
)
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"


def is_running(pid: int) -> bool:
    """判断进程是否存活（Windows 与 POSIX 都可用）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            # tasklist 输出是系统代码页（GBK），不能用 text=True 走默认 UTF-8
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=10,
            )
            return str(pid) in out.stdout.decode("gbk", errors="ignore")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> int:
    if not PID_FILE.exists():
        return 0
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (ValueError, OSError):
        return 0


def wait_healthy(timeout: float = 40.0) -> bool:
    """轮询健康检查接口，确认服务真正可用。

    必须显式绕过系统代理：若配置了 HTTP_PROXY，本地端口的请求
    也会走代理，导致 502 误判为服务未就绪。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(HEALTH_URL, timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def port_owner() -> int:
    """查出监听指定端口的进程 PID，PID 文件不可靠时的兜底。"""
    try:
        if os.name == "nt":
            # netstat 输出同样按 GBK 解码
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, timeout=15,
            )
            for line in out.stdout.decode("gbk", errors="ignore").splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        try:
                            return int(parts[-1])
                        except ValueError:
                            continue
    except Exception:
        pass
    return 0


def cmd_start() -> int:
    # 1. PID 文件里的进程还活着就直接告知
    old = read_pid()
    if old and is_running(old):
        print(f"服务已在运行，进程号 {old}")
        print(f"如需重启，请先运行 关闭服务.bat")
        return 0
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)

    # 2. 端口被别人占了就不能起
    owner = port_owner()
    if owner:
        print(f"[错误] 端口 {PORT} 已被进程 {owner} 占用")
        print()
        print(f"先访问 http://127.0.0.1:{PORT} 确认是否本服务")
        print(f"若确认不是，可执行: taskkill /F /PID {owner}")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 3. 用 pythonw 启动（无控制台窗口），stdout/stderr 收进日志
    py = Path(PYTHON)
    if not py.exists():
        print(f"[错误] 运行环境不存在：{py}")
        print("        请先运行 启动服务.bat 初始化")
        return 1

    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    creation = 0
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
        # 让服务彻底脱离当前命令行进程组，父进程退出后继续存活；
        # CREATE_NO_WINDOW 保证不残留控制台窗口。
        creation = 0x00000200 | 0x00000008 | 0x08000000
        # stdio 全部交给日志文件，避免 DETACHED 下的句柄继承问题
        devnull = open(os.devnull, "rb")
        proc = subprocess.Popen(
            [str(py), str(ROOT / "run.py"), "--port", str(PORT)],
            cwd=str(ROOT),
            stdin=devnull,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            [str(py), str(ROOT / "run.py"), "--port", str(PORT)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"进程已启动，PID {proc.pid}")

    if not wait_healthy(40):
        print()
        print(f"[警告] 40 秒内未确认服务就绪")
        print(f"        进程可能仍在启动，或已启动失败")
        print(f"        请查看日志：{LOG_FILE}")
        return 2

    # Windows 上 python.exe 跑脚本时会再 fork 一个子进程真正监听端口，
    # Popen 返回的是父进程。PID 文件改记实际监听者，便于外部工具
    # （如端口反查、任务管理器）核对；停止时用 /T 仍能覆盖整棵树。
    owner = port_owner()
    if owner and owner != proc.pid:
        PID_FILE.write_text(str(owner), encoding="utf-8")
        print(f"服务已就绪（实际监听进程 PID {owner}）")
    else:
        print("服务已就绪")
    print(f"  地址  http://127.0.0.1:{PORT}")
    print(f"  日志  {LOG_FILE}")
    print("  停止  运行 关闭服务.bat")
    return 0


def cmd_stop() -> int:
    pid = read_pid()
    # PID 文件失效时，退回到按端口找进程
    if not pid or not is_running(pid):
        pid = port_owner()
        if not pid:
            print("服务未在运行")
            return 0
        print(f"PID 文件失效，已按端口定位到进程 {pid}")

    print(f"正在停止服务（PID {pid}）...")
    try:
        if os.name == "nt":
            # /T 连同子进程一起结束。PID 文件里记的是实际监听端口的那个
            # 进程（可能是 python.exe fork 出来的子进程），/T 会覆盖其父进程。
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=20,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        print(f"[错误] 停止失败：{exc}")
        return 1

    # 等待端口释放；未释放则按端口重新定位并重试一次，
    # 兜住「PID 文件记的进程已退出、但端口被另一个进程占着」的情况
    for _ in range(10):
        if not port_owner():
            break
        time.sleep(1)

    if port_owner():
        still = port_owner()
        print(f"[重试] 端口 {PORT} 仍被 {still} 占用，再次结束")
        try:
            subprocess.run(
                ["taskkill", "/PID", str(still), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
        for _ in range(8):
            if not port_owner():
                break
            time.sleep(1)

    PID_FILE.unlink(missing_ok=True)

    if port_owner():
        print(f"[警告] 端口 {PORT} 仍被占用，可能有其他程序")
        return 2
    print("服务已停止")
    return 0


def cmd_status() -> int:
    pid = read_pid()
    alive = is_running(pid) if pid else False
    healthy = wait_healthy(2)
    if alive and healthy:
        print(f"运行中  PID {pid}  http://127.0.0.1:{PORT}")
        return 0
    if alive:
        print(f"进程存在但健康检查未通过  PID {pid}")
        print(f"请查看日志：{LOG_FILE}")
        return 1
    print("未运行")
    return 1


def main() -> int:
    # 被 bat 调用时控制台是 GBK(936)，此时必须用 GBK 输出，
    # 否则中文会显示成乱码。直接用 python 跑时沿用终端默认编码。
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="gbk", errors="replace")
            sys.stderr.reconfigure(encoding="gbk", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="备份服务后台管理")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    args = ap.parse_args()
    return {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "restart": lambda: cmd_stop() or cmd_start(),
    }[args.action]()


if __name__ == "__main__":
    sys.exit(main())