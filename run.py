"""启动脚本。

用法：
    python run.py                        # 默认 127.0.0.1:8077，自动开浏览器
    python run.py --host 0.0.0.0         # 允许外部访问（服务器部署需用）
    python run.py --port 9000 --no-browser

也支持环境变量（容器部署更方便）：
    CBS_HOST / CBS_PORT / CBS_NO_BROWSER
    CBS_ADMIN_PASSWORD                    # 预设初始管理员密码
"""

from __future__ import annotations

import argparse
import os
import webbrowser
from threading import Timer


def main() -> None:
    parser = argparse.ArgumentParser(description="网络配置备份中心")
    parser.add_argument(
        "--host",
        default=os.environ.get("CBS_HOST", "127.0.0.1"),
        help="监听地址，服务器部署用 0.0.0.0",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CBS_PORT", "8077")),
        help="监听端口",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        default=os.environ.get("CBS_NO_BROWSER", "").lower() in ("1", "true", "yes"),
        help="不自动打开浏览器",
    )
    parser.add_argument("--reload", action="store_true", help="开发模式，代码变更自动重载")
    args = parser.parse_args()

    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{browser_host}:{args.port}/"

    print(f"\n  网络配置备份中心已启动->  {url}")
    if args.host == "0.0.0.0":
        print("  监听 0.0.0.0，可通过服务器 IP 访问")
        print("  生产环境建议前置 Nginx + HTTPS，并设置 CBS_ADMIN_PASSWORD")
    print("  按 Ctrl+C 停止\n")

    if not args.no_browser and not args.reload:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()