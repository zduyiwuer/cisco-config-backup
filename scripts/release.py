# -*- coding: utf-8 -*-
"""创建 GitHub Release。

本机没有 gh CLI，改用 GitHub REST API。
令牌从 git credential 取（复用已有的 GitHub 凭据），不落盘。

用法：
    python scripts/release.py v1.1.0
    python scripts/release.py v1.1.0 --dry-run
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com/repos/zduyiwuer/cisco-config-backup"


def get_token() -> str:
    """从 git credential 取 GitHub 令牌。"""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("未找到 GitHub 凭据，请先配置 git credential")


def api_call(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} 失败 [{exc.code}]: {detail[:400]}") from exc


def release_body(tag: str) -> str:
    """取 CHANGELOG 中对应版本的段落。"""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # 以 "# 标题 vX.Y.Z" 开头的那一节
    marker = f"# 多厂商网络设备配置备份中心 {tag}"
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"CHANGELOG 里找不到 {tag} 的段落")
    # 到下一个一级标题（## ）为止
    rest = text[start:]
    end = rest.find("\n# ", 1)
    body = rest[:end] if end > 0 else rest
    return body.strip() + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/release.py <tag> [--dry-run]")
        return 1
    tag = sys.argv[1]
    dry = "--dry-run" in sys.argv

    body = release_body(tag)
    name = f"{tag} · 多厂商网络设备配置备份中心"

    if dry:
        print(f"[dry-run] tag={tag}")
        print(f"[dry-run] name={name}")
        print(f"[dry-run] body 长度={len(body)}")
        print("--- body 前 400 字 ---")
        print(body[:400])
        return 0

    token = get_token()

    # 已存在则更新，不存在则创建
    try:
        existing = api_call("GET", f"{API}/releases/tags/{tag}", token)
        action = "update"
        release_id = existing["id"]
    except RuntimeError:
        action = "create"
        release_id = None

    payload = {
        "tag_name": tag,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": False,
    }
    if action == "update":
        result = api_call("PATCH", f"{API}/releases/{release_id}", token, payload)
    else:
        result = api_call("POST", f"{API}/releases", token, payload)

    print(f"Release {action} 成功")
    print(f"  名称: {result.get('name')}")
    print(f"  地址: {result.get('html_url')}")
    print(f"  Tag : {result.get('tag_name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())