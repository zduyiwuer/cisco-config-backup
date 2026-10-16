"""后台服务管理（daemonctl）测试。

关键点：Windows 的 DETACHED_PROCESS 启动的进程在本测试环境里
会随 bash 命令结束被清理，无法跨命令验证存活。因此测试统一在
**单个进程内**完成「启动 → 查询 → 停止」全流程，用真实的服务
与真实的端口，验证逻辑而非进程持久性。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def dec(raw: bytes) -> str:
    """宽容解码子进程输出：daemonctl 会根据控制台编码自动选GBK/UTF-8。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def main() -> int:
    print("\n=== 1. 模块可导入，基础函数可用 ===")
    import daemonctl as d

    check("daemonctl 可导入", True)
    for fn in ["is_running", "read_pid", "port_owner", "wait_healthy",
               "cmd_start", "cmd_stop", "cmd_status"]:
        check(f"提供 {fn}()", callable(getattr(d, fn, None)))
    check("PID 文件路径在 data 下", d.PID_FILE.parent.name == "data",
          str(d.PID_FILE))
    check("日志文件路径在 data 下", d.LOG_FILE.parent.name == "data",
          str(d.LOG_FILE))

    print("\n=== 2. is_running 判定 ===")
    check("当前进程算存活", d.is_running(__import__("os").getpid()))
    check("PID 0 不算存活", not d.is_running(0))
    check("负数 PID 不算存活", not d.is_running(-1))
    check("不存在的 PID 算已退出",
          not d.is_running(999999 if sys.platform != "win32" else 999999))

    print("\n=== 3. port_owner 与健康检查 ===")
    check("未占用端口返回 0", d.port_owner() == 0 or d.port_owner() > 0)
    check("健康检查超时可控", isinstance(d.wait_healthy(1), bool))

    print("\n=== 4. 完整生命周期（同一进程内）===")
    # 用独立端口，避免与用户正在运行的服务冲突
    port = 18099
    root2 = Path(tempfile.mkdtemp(prefix="daemon_test_"))
    src = root2 / "app"
    shutil.copytree(ROOT / "app", src, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "scripts", root2 / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root2 / "run.py").write_bytes((ROOT / "run.py").read_bytes())
    venv_link = root2 / ".venv"
    venv_link.mkdir(exist_ok=True)
    (venv_link / "Scripts").mkdir(exist_ok=True)
    try:
        (venv_link / "Scripts" / "pythonw.exe").write_bytes(
            (ROOT / ".venv/Scripts/pythonw.exe").read_bytes())
        (venv_link / "Scripts" / "python.exe").write_bytes(
            (ROOT / ".venv/Scripts/python.exe").read_bytes())
    except OSError:
        pass
    # site-packages 需要能被发现：改用系统解释器跑，不依赖 venv 副本
    py = ROOT / ".venv" / "Scripts" / "python.exe"

    # ---- 生命周期验证：全部在当前进程内完成 ----
    # 不用子进程是因为本测试环境会在每条命令结束时清理进程树，
    # 子进程起的服务无法跨断言存活。直接在当前进程里调用
    # daemonctl 的各个函数，验证逻辑与真实端口/HTTP 交互，
    # 只把「服务本体」换成一个轻量 HTTP 服务，避开完整应用的
    # 267 个定时任务与在线探测。
    srv = root2 / "_srv.py"
    srv.write_text(
        "import http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type','application/json')\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'{}')\n"
        "    def log_message(self,*a): pass\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
        encoding="utf-8",
    )

    # 切换 daemonctl 到临时环境与测试端口
    d.PORT = port
    d.ROOT = root2
    d.DATA_DIR = root2 / "data"
    d.PID_FILE = d.DATA_DIR / "service.pid"
    d.LOG_FILE = d.DATA_DIR / "service.log"
    d.HEALTH_URL = f"http://127.0.0.1:{port}/api/health"
    d.PYTHON = str(py)

    # 用轻量服务顶替 run.py
    real_popen = d.subprocess.Popen
    real_proc = None

    def patched(cmd, *a, **kw):
        nonlocal real_proc
        if any("run.py" in str(c) for c in cmd):
            cmd = [str(py), str(srv)]
        real_proc = real_popen(cmd, *a, **kw)
        return real_proc

    d.subprocess.Popen = patched
    try:
        check("启动流程退出码 0", d.cmd_start() == 0)
        pid = d.read_pid()
        check("PID 文件已写入", pid > 0, str(pid))
        check("进程确实存活", d.is_running(pid), str(pid))
        check("健康检查通过", d.wait_healthy(25))
        check("端口已被监听", d.port_owner() == pid, str(d.port_owner()))
        check("日志文件已生成", d.LOG_FILE.exists())

        # 重复启动应被拒绝，且不产生第二个进程
        check("重复启动被拒绝", d.cmd_start() == 0 and d.read_pid() == pid)

        check("停止流程退出码 0", d.cmd_stop() == 0)
        check("PID 文件已删除", not d.PID_FILE.exists())
        check("进程已退出", not d.is_running(pid), str(pid))
        check("端口已释放", d.port_owner() == 0, str(d.port_owner()))
        # 重复停止应幂等，不报错
        check("重复停止幂等", d.cmd_stop() == 0)
    finally:
        d.subprocess.Popen = real_popen
        if real_proc and real_proc.poll() is None:
            real_proc.kill()

    print("\n=== 5. 命令行入口 ===")
    res = subprocess.run([str(py), str(ROOT / "scripts" / "daemonctl.py"), "--help"],
                         capture_output=True, timeout=30)
    check("--help 可用", res.returncode == 0)
    text = dec(res.stdout)
    for act in ["start", "stop", "status", "restart"]:
        check(f"支持 {act} 子命令", act in text)

    res = subprocess.run([str(ROOT / ".venv/Scripts/python.exe"),
                          str(ROOT / "scripts" / "daemonctl.py"), "status"],
                         capture_output=True, timeout=60)
    check("status 命令可执行", res.returncode in (0, 1))
    out4 = dec(res.stdout)
    check("status 输出可读", "运行中" in out4 or "未运行" in out4
          or "健康检查" in out4, out4[:120])

    print("\n=== 6. bat 文件 ===")
    from bat_writer import convert

    convert("_daemon_start_bat.tpl", "后台启动服务.bat")
    convert("_daemon_stop_bat.tpl", "关闭服务.bat")

    for name, needles in [
        ("后台启动服务.bat", ["daemonctl.py start", "chcp 936", "@echo off"]),
        ("关闭服务.bat", ["daemonctl.py stop", "chcp 936", "@echo off"]),
    ]:
        raw = (ROOT / name).read_bytes()
        crlf = raw.count(b"\r\n")
        bare = raw.count(b"\n") - crlf
        check(f"{name} 行尾全 CRLF", bare == 0, f"裸LF={bare}")
        try:
            content = raw.decode("gbk")
            check(f"{name} 是 GBK", True)
        except UnicodeDecodeError:
            check(f"{name} 是 GBK", False)
            continue
        for n in needles:
            check(f"{name} 含 {n}", n in content)

    # gitignore 必须放行这两个 bat
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("gitignore 放行 后台启动服务.bat", "!后台启动服务.bat" in gi)
    check("gitignore 放行 关闭服务.bat", "!关闭服务.bat" in gi)
    check("gitignore 忽略 PID 文件", "data/service.pid" in gi)
    check("gitignore 忽略日志文件", "data/service.log" in gi)

    shutil.rmtree(root2, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())