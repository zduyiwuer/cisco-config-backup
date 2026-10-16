# -*- coding: utf-8 -*-
"""把 bat 模板转成 cmd 可正确执行的编码与行尾。

cmd 默认代码页 936(GBK)，且批处理必须用 CRLF：
- 写成 UTF-8 → 中文乱码，甚至整行被当成命令执行
- 写成 LF   → 整个文件被解析成一行，报 "xx 不是内部或外部命令"

本脚本把 scripts/*.tpl 转成 GBK + CRLF 的 bat。
改了模板后重新跑一次即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def convert(tpl_name: str, bat_name: str) -> None:
    tpl = ROOT / "scripts" / tpl_name
    out = ROOT / bat_name
    text = tpl.read_text(encoding="utf-8")
    # 统一成 CRLF
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    crlf = normalized.replace("\n", "\r\n")

    raw: bytes
    try:
        raw = crlf.encode("gbk")
        enc = "GBK"
    except UnicodeEncodeError:
        # 模板含生僻字时回退 UTF-8；GBK 无法表示的字符通常不在 bat 常用范围内
        raw = crlf.encode("utf-8")
        enc = "UTF-8"

    out.write_bytes(raw)
    n_crlf = raw.count(b"\r\n")
    n_bare = raw.count(b"\n") - n_crlf
    print(f"{bat_name}:编码={enc} CRLF={n_crlf} 裸LF={n_bare} 大小={len(raw)}B")


if __name__ == "__main__":
    convert("_start_bat.tpl", "启动服务.bat")
    convert("_daemon_start_bat.tpl", "后台启动服务.bat")
    convert("_autostart_bat.tpl", "安装开机自启.bat")
    convert("_daemon_stop_bat.tpl", "关闭服务.bat")
    sys.exit(0)