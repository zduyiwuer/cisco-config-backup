"""配置差异计算与汇总。"""

from __future__ import annotations

import difflib


def make_diff(old: str, new: str, old_label: str = "上一版本", new_label: str = "当前版本") -> tuple[str, int, int]:
    """返回 (unified diff 文本, 新增行数, 删除行数)。"""
    diff = difflib.unified_diff(
        (old or "").splitlines(),
        (new or "").splitlines(),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
        n=3,
    )
    lines = list(diff)
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return "\n".join(lines) + "\n", added, removed


def diff_stats(old: str, new: str) -> tuple[int, int]:
    _, added, removed = make_diff(old, new)
    return added, removed