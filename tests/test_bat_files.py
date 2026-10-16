"""Windows 批处理文件格式测试。

cmd 对编码和行尾极其敏感，写错时表现为「双击无反应」或
「'xx' 不是内部或外部命令」，且报错信息完全看不出真实原因。
这个测试把三个致命条件固化下来：

1. 行尾必须是 CRLF —— 写成 LF 时 cmd 会把整个文件当一行解析
2. 编码必须是 GBK —— cmd 默认代码页 936，写成 UTF-8 会乱码并报错
3. 不能有 UTF-8 BOM   —— cmd 会把 BOM 当成命令的一部分
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

PASS, FAIL = 0, 0
BAT_FILES = ["启动服务.bat", "后台启动服务.bat", "安装开机自启.bat", "关闭服务.bat"]
TPL_FILES = ["_start_bat.tpl", "_daemon_start_bat.tpl",
             "_autostart_bat.tpl", "_daemon_stop_bat.tpl"]


def _kill_tree(proc: subprocess.Popen) -> None:
    """结束 bat 及其派生的 python 进程，避免测试后残留占用端口。"""
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    from bat_writer import convert

    print("\n=== 1. 转换器生成正确的 bat ===")
    for tpl, bat in zip(TPL_FILES, BAT_FILES):
        convert(tpl, bat)
        check(f"{bat} 已重新生成", True)

    print("\n=== 2. 行尾必须全为 CRLF ===")
    for name in BAT_FILES:
        raw = (ROOT / name).read_bytes()
        crlf = raw.count(b"\r\n")
        bare_lf = raw.count(b"\n") - crlf
        bare_cr = raw.count(b"\r") - crlf
        check(f"{name} 无裸 LF", bare_lf == 0, f"裸LF={bare_lf}")
        check(f"{name} 无裸 CR", bare_cr == 0, f"裸CR={bare_cr}")
        check(f"{name} CRLF 数量合理", crlf > 20, f"CRLF={crlf}")

    print("\n=== 3. 编码必须是 GBK ===")
    for name in BAT_FILES:
        raw = (ROOT / name).read_bytes()
        try:
            text = raw.decode("gbk")
            check(f"{name} 是有效 GBK", True)
        except UnicodeDecodeError as e:
            check(f"{name} 是有效 GBK", False, str(e))
            continue
        # GBK 内容若同时是合法 UTF-8，说明实际存的是 UTF-8（中文三字节序列
        # 通常不再是合法 GBK 双字节序列）
        try:
            raw.decode("utf-8")
            # 若 UTF-8 解码也成功且不含 BOM，说明可能误存为 UTF-8
            check(f"{name} 未误存为 UTF-8", False, "同时是合法 UTF-8，请确认内容")
        except UnicodeDecodeError:
            check(f"{name} 未误存为 UTF-8", True)

    print("\n=== 4. 不能有 UTF-8 BOM ===")
    for name in BAT_FILES:
        raw = (ROOT / name).read_bytes()
        check(f"{name} 无 BOM", not raw.startswith(b"\xef\xbb\xbf"),
              "存在 UTF-8 BOM，cmd 会把它当命令执行")
        # 首行必须是 @echo off
        first = raw.split(b"\r\n")[0]
        check(f"{name} 首行为 @echo off",
              first.lower().strip() == b"@echo off", repr(first[:30]))

    print("\n=== 5. 关键命令完整性 ===")
    start = (ROOT / "启动服务.bat").read_bytes().decode("gbk")
    for needle, desc in [
        ("@echo off", "关闭命令回显"),
        ("cd /d \"%~dp0\"", "切换到脚本所在目录"),
        (".venv\\Scripts\\python.exe", "使用虚拟环境的 python"),
        ("run.py --port 8077", "启动服务"),
        ("chcp 936", "设置 GBK 代码页"),
    ]:
        check(f"启动脚本含「{desc}」", needle in start, f"缺 {needle}")

    for name, needle, desc in [
        ("后台启动服务.bat", "daemonctl.py start", "调用后台启动"),
        ("后台启动服务.bat", "chcp 936", "设置 GBK 代码页"),
        ("关闭服务.bat", "daemonctl.py stop", "调用停止"),
    ]:
        txt = (ROOT / name).read_bytes().decode("gbk")
        check(f"{name} 含「{desc}」", needle in txt)

    auto = (ROOT / "安装开机自启.bat").read_bytes().decode("gbk")
    for needle, desc in [
        ("schtasks /Create", "创建计划任务"),
        ("ONLOGON", "登录时启动"),
        ("pythonw.exe", "无控制台窗口运行"),
        ("/F", "强制覆盖已有任务"),
    ]:
        check(f"自启脚本含「{desc}」", needle in auto, f"缺 {needle}")

    print("\n=== 6. 括号与变量写法 ===")
    # cmd 里 if/括号块内用 %VAR% 有陷阱，需用 !VAR! 延迟展开
    for name, text in [("启动服务.bat", start), ("安装开机自启.bat", auto)]:
        check(f"{name} 启用延迟展开",
              "setlocal enabledelayedexpansion" in text,
              "括号块内用 !VAR! 需要它")
        # 文件路径含空格与中文，必须加引号
        check(f"{name} cd 带引号", 'cd /d "%~dp0"' in text)

    print("\n=== 7. 实际执行验证（跑到服务启动阶段即终止）===")
    # 脚本会启动服务并常驻，不能用 run() 等待退出，否则永远等不到。
    # 也不走管道（cmd 的输出缓冲 + 管道读取在 Windows 上不可靠），
    # 直接重定向到文件，轮询文件内容。
    out_file = ROOT / "_bat_test_out.txt"
    if out_file.exists():
        out_file.unlink()
    started = False
    with out_file.open("wb") as fh:
        proc = subprocess.Popen(
            ["cmd", "/c", "启动服务.bat"],
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 35
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    raw = out_file.read_bytes()
                except OSError:
                    raw = b""
                if b"Started server process" in raw or b"Uvicorn running" in raw:
                    started = True
                    break
                time.sleep(0.4)
        finally:
            _kill_tree(proc)

    raw = out_file.read_bytes()
    out_file.unlink(missing_ok=True)
    text = raw.decode("gbk", errors="replace")

    check("执行未出现乱码报错",
          "不是内部或外部命令" not in text and "不是内部" not in text,
          text[:200])
    check("识别出运行环境已就绪", "运行环境已就绪" in text, text[:300])
    check("识别出依赖已就绪", "依赖已就绪" in text)
    check("输出服务地址", "127.0.0.1:8077" in text)
    check("服务进程已启动", started, text[-200:])
    check("无残留临时文件", not out_file.exists())

    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())