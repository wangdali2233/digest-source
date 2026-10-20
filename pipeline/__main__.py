"""命令行入口：python -m pipeline build [--date] [--offline] [--stdout]"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections import Counter
from zoneinfo import ZoneInfo

from . import classify, config, fetch, merge, render, state
from .config import ROOT

UTC = dt.timezone.utc


def _collect(cfg: dict, start: dt.datetime, end: dt.datetime, offline: bool):
    if offline:
        path = ROOT / "data" / "fixtures" / "sample.json"
        return json.loads(path.read_text(encoding="utf-8")), []

    rows: list[dict] = []
    missing: list[str] = []
    for src in config.sources(cfg):
        try:
            rows.extend(fetch.fetch(src, start.astimezone(UTC), end.astimezone(UTC)))
        except Exception as exc:  # 单源失败不该拖垮整期
            name = src.get("name", src.get("id", "?"))
            missing.append(f"{name}（{type(exc).__name__}）")
            print(f"! 源 {src.get('id')} 抓取失败: {exc}", file=sys.stderr)
    return rows, missing


def _log_source_stats(rows: list[dict], items: list[dict]) -> None:
    """每个源贡献多少条必须可见：某源静默归零是最难发现的故障。"""
    got = Counter(row.get("source") for row in rows)
    kept = Counter(item["source"] for item in items)
    for source_id, total in sorted(got.items()):
        kept_count = kept.get(source_id, 0)
        note = "  ← 窗口内 0 条，检查时间窗或是否跑晚了" if kept_count == 0 else ""
        print(
            f"  · {source_id}: feed {total} 条 → 窗口内 {kept_count} 条{note}",
            file=sys.stderr,
        )


def build(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    site_cfg = config.site(cfg)
    quota = config.quota(cfg)
    tz = ZoneInfo(site_cfg.get("timezone", "Asia/Shanghai"))

    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz).date()
    hour, minute = (int(x) for x in site_cfg.get("publish_at", "07:00").split(":"))
    end = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=tz)
    start = end - dt.timedelta(hours=int(site_cfg.get("window_hours", 24)))
    slack = dt.timedelta(hours=int(site_cfg.get("window_slack_hours", 0)))
    floor = start - slack

    # 幂等保护：同一天只生成一次。否则 bot 提交触发的 push 会让管道重跑，
    # 而这时 seen.json 已把选过的条目记为已读，第二轮会选出另一批并覆盖当天版面。
    content_dir = ROOT / cfg.get("output", {}).get("content_dir", "content/posts")
    out_path = content_dir / f"{day.isoformat()}.md"
    if out_path.exists() and not args.force and not args.stdout:
        print(f"= {out_path.relative_to(ROOT)} 已存在，跳过生成（--force 可覆盖）", file=sys.stderr)
        return 0

    rows, missing = _collect(cfg, floor, end, args.offline)

    sources_by_id = {src["id"]: src for src in config.sources(cfg)}
    items = merge.prepare(
        rows,
        config.sections(cfg),
        floor.astimezone(UTC),
        end.astimezone(UTC),
        sources_by_id,
    )
    _log_source_stats(rows, items)
    raw_count = len(items)

    items = merge.dedupe(items)
    deduped_count = len(items)

    state_file = ROOT / cfg.get("output", {}).get("state_file", "data/seen.json")
    seen = state.load(state_file)
    items = [
        item
        for item in items
        if state.is_fresh(
            seen,
            item["canonical"],
            day,
            item["points"],
            int(quota["seen_days"]),
            ignore_same_day=args.force,
        )
    ]

    weights = {src["id"]: float(src.get("weight", 0.7)) for src in config.sources(cfg)}
    weighted = merge.score(items, weights)

    # 只把「有机会入选」的头部候选送去分类，控制 token 上限
    if classify.enabled(cfg):
        plan = classify.budget(cfg)
        batch = sorted(weighted, key=lambda row: -row["score"])[: plan["limit"]]
        changed, note = classify.classify(batch, config.sections(cfg), cfg)
        print(f"  {note}", file=sys.stderr)
    else:
        print(
            "  · 没有可用的模型凭据（DEEPSEEK_API_KEY），使用关键词分类",
            file=sys.stderr,
        )

    picked = merge.select(weighted, config.sections(cfg), quota)

    if len(picked) < int(quota["min_items"]):
        print(
            f"! 只选出 {len(picked)} 条，低于下限 {quota['min_items']}，中止发布",
            file=sys.stderr,
        )
        return 2

    markdown = render.render(day, picked, config.sections(cfg), site_cfg, missing)

    if args.stdout:
        sys.stdout.write(markdown)
    else:
        content_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"✓ 写入 {out_path.relative_to(ROOT)}")

    if not args.no_state:
        for item in picked:
            state.touch(seen, item["canonical"], day, item["points"])
        state.save(state_file, state.prune(seen, day))

    print(
        f"  采集 {raw_count} 条 → 去重后 {deduped_count} 条 → 选用 {len(picked)} 条",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description="每日信息聚合")
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="生成一天的 Markdown")
    build_cmd.add_argument("--date", help="出稿日期 YYYY-MM-DD，默认今天")
    build_cmd.add_argument("--offline", action="store_true", help="用本地样本数据，不联网")
    build_cmd.add_argument("--stdout", action="store_true", help="输出到 stdout，不写文件")
    build_cmd.add_argument("--no-state", action="store_true", help="不更新 seen.json")
    build_cmd.add_argument("--force", action="store_true", help="即使当天文件已存在也重新生成")
    build_cmd.add_argument("--config", default=None, help="配置文件路径")

    args = parser.parse_args(argv)
    if args.command == "build":
        return build(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
