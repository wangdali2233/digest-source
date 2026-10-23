"""探测 DeepSeek 当前可用的模型清单，和上次快照对比，有变化就报警。

用法：
    python3 scripts/check_models.py                 # 人类可读输出
    python3 scripts/check_models.py --json          # 机器可读
    python3 scripts/check_models.py --body out.md   # 生成 issue 正文
    python3 scripts/check_models.py --fixture f.json  # 离线测试

退出码：0 = 无变化 / 首次建立基线；2 = 有变化，需要人看一眼。

为什么要有这个：模型名会变（deepseek-chat 就是这样悄悄从文档里消失的），
但配置里烧死的名字不会自己更新。两周探一次，比等它某天 404 好。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import classify, config  # noqa: E402

SNAPSHOT = ROOT / "data" / "models.json"


def fetch_ids(endpoint: str, key: str) -> list[str]:
    base = endpoint.rsplit("/chat/completions", 1)[0]
    request = urllib.request.Request(
        base + "/models", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        payload = json.loads(resp.read())
    return sorted({m["id"] for m in payload.get("data", []) if m.get("id")})


def load_snapshot() -> list[str] | None:
    if not SNAPSHOT.exists():
        return None
    try:
        return sorted(json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("ids", []))
    except (OSError, ValueError):
        return None


def save_snapshot(ids: list[str]) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps({"ids": ids}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_report(configured: str, previous: list[str] | None, current: list[str]) -> dict:
    prev = set(previous or [])
    cur = set(current)
    return {
        "configured": configured,
        "previous": sorted(prev),
        "current": sorted(cur),
        "added": sorted(cur - prev) if previous else [],
        "removed": sorted(prev - cur) if previous else [],
        "configured_missing": configured not in cur,
        "baseline": previous is None,
    }


def to_markdown(report: dict) -> str:
    lines = ["模型清单发生变化，需要人工确认。", ""]
    if report["baseline"]:
        lines.append("首次建立基线。")
    if report["configured_missing"]:
        lines += [
            f"**配置里的模型 `{report['configured']}` 已不在返回列表中。**",
            "下次定时任务会退回关键词分类（不会报错，但质量和摘要都会倒退）。",
            "",
        ]
    if report["added"]:
        lines += ["新增模型：", *[f"- `{m}`" for m in report["added"]], ""]
    if report["removed"]:
        lines += ["下线模型：", *[f"- `{m}`" for m in report["removed"]], ""]
    lines += [
        "当前可用：",
        *[f"- `{m}`" for m in report["current"]],
        "",
        f"配置在 `sources.toml` 的 `[llm] model`，当前值 `{report['configured']}`。",
        "原则：继续选**低价档**（`-flash`），除非分类质量明显不够用。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--body", help="把 issue 正文写到这个路径")
    parser.add_argument("--fixture", help="用本地 JSON 代替真实请求（离线测试）")
    args = parser.parse_args(argv)

    cfg = config.load()
    plan = classify.profile(cfg)
    endpoint = str(plan["endpoint"])
    configured = str(plan["model"])
    key_env = str(plan["key_env"])

    if args.fixture:
        payload = json.loads(pathlib.Path(args.fixture).read_text(encoding="utf-8"))
        current = sorted({m["id"] for m in payload.get("data", []) if m.get("id")})
    else:
        key = (os.environ.get(key_env) or "").strip()
        if not key:
            print(f"缺少 {key_env}，无法探测", file=sys.stderr)
            return 1
        current = fetch_ids(endpoint, key)

    # 大目录（如 OpenRouter 的 468 个模型）不记快照：新增是常态，记了只会天天报警。
    # 只校验「配置的模型还在不在」。
    previous = load_snapshot() if len(current) <= 100 else None
    report = build_report(configured, previous, current)
    report["catalog_too_large"] = len(current) > 100

    if report["catalog_too_large"]:
        if args.body:
            pathlib.Path(args.body).write_text(to_markdown(report), encoding="utf-8")
        if args.json:
            print(json.dumps({**report, "changed": report["configured_missing"]}, ensure_ascii=False))
        else:
            print("配置的模型:", configured)
            print("目录过大（%d 个），只校验存在性" % len(current))
            print("判定:", "配置的模型不存在" if report["configured_missing"] else "存在，正常")
        return 2 if report["configured_missing"] else 0

    changed = bool(report["baseline"] or report["added"] or report["removed"] or report["configured_missing"])
    if report["baseline"]:
        save_snapshot(current)
        print("首次建立基线：" + ", ".join(current), file=sys.stderr)
        changed = False  # 基线不算变化，不打扰
    elif report["added"] or report["removed"]:
        save_snapshot(current)

    if args.body:
        pathlib.Path(args.body).write_text(to_markdown(report), encoding="utf-8")

    if args.json:
        print(json.dumps({**report, "changed": changed}, ensure_ascii=False))
    else:
        print("配置的模型:", configured)
        print("上次快照  :", ", ".join(report["previous"]) or "（无）")
        print("当前可用  :", ", ".join(current))
        if report["configured_missing"]:
            print("!! 配置的模型不在列表里，需要更新 sources.toml")
        print("判定:", "有变化" if changed else "无变化")

    return 2 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
