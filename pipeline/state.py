"""跨天去重的状态：data/seen.json。

结构：{canonical: {"first": "2026-10-01", "last": "2026-10-19", "max_points": 312}}
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib


def load(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # 手误写成 [] 或 null 时不要让整条管道崩掉
    return data if isinstance(data, dict) else {}


def save(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )


def is_fresh(
    seen: dict,
    canonical: str,
    day: dt.date,
    points: int,
    days: int,
    ignore_same_day: bool = False,
) -> bool:
    """7 天内出现过就不再收，除非分数翻倍（说明有新进展）。

    ``ignore_same_day`` 用于 --force 重生成：当天这批记录是上一轮自己写的，
    必须忽略，否则强制重跑会一条都选不出来。
    """
    rec = seen.get(canonical)
    if not rec:
        return True
    try:
        last = dt.date.fromisoformat(rec.get("last", ""))
    except ValueError:
        return True
    if ignore_same_day and last == day:
        return True
    if (day - last).days >= days:
        return True
    return points > 2 * int(rec.get("max_points") or 0)


def touch(seen: dict, canonical: str, day: dt.date, points: int) -> None:
    rec = seen.get(canonical)
    if rec:
        rec["last"] = day.isoformat()
        rec["max_points"] = max(int(rec.get("max_points") or 0), points)
    else:
        seen[canonical] = {
            "first": day.isoformat(),
            "last": day.isoformat(),
            "max_points": points,
        }


def prune(seen: dict, day: dt.date, keep_days: int = 180) -> dict:
    """丢掉太久没出现的记录，防止文件无限膨胀。"""
    out = {}
    for key, rec in seen.items():
        try:
            last = dt.date.fromisoformat(rec.get("last", ""))
        except ValueError:
            continue
        if (day - last).days <= keep_days:
            out[key] = rec
    return out
