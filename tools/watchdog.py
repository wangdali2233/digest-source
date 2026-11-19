"""外部对账巡检：产物缺了就触发 GitHub Actions。

背景见 docs/design/002-external-watchdog.md。GitHub 的计划任务延迟已从 2.3 小时
恶化到 8 小时以上，补跑形同虚设；这个脚本每 10 分钟检查一次产物是否齐备，
缺了才 dispatch，所以不依赖「恰好在某一分钟醒着」。

用法：
    GITHUB_TOKEN=ghp_xxx python3 tools/watchdog.py --dry-run
    GITHUB_TOKEN=ghp_xxx python3 tools/watchdog.py --pull

令牌只需要该仓库的 Actions: read and write，不要给 contents。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import config  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")
API = "https://api.github.com"
REPO = "wangdali2233/digest-source"

# 栏目 → 对应的触发器 workflow 文件
TARGETS = [
    ("tech", None, "publish-tech.yml"),
    ("finance", "morning", "publish-finance-am.yml"),
    ("finance", "evening", "publish-finance-pm.yml"),
]


def _request(path: str, token: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "digest-watchdog/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def remote_exists(path: str, token: str) -> bool:
    """用 Contents API 判断产物是否已在远端。

    故意不用 raw.githubusercontent.com：那是 CDN，最长缓存 5 分钟，
    会把刚提交的文件判成不存在，导致重复触发。
    """
    status, _ = _request(f"/repos/{REPO}/contents/{path}?ref=main", token)
    if status == 200:
        return True
    if status == 404:
        return False
    raise RuntimeError(f"查询 {path} 失败：HTTP {status}")


def artifact_path(profile: str, slot: str | None, day: dt.date) -> str:
    cfg = config.load(profile, slot=slot)
    # config.load 已把 {profile} 占位符展开，这里不能再拼一次栏目名
    content_dir = cfg.get("output", {}).get("content_dir", "source/_posts")
    stem = day.strftime("%d") + (f"-{slot}" if slot else "")
    return f"{content_dir}/{day:%Y/%m}/{stem}.md"


def window_end(profile: str, slot: str | None, day: dt.date) -> dt.datetime:
    cfg = config.load(profile, slot=slot)
    hour, minute = (int(x) for x in cfg["site"]["publish_at"].split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=BEIJING)


def decide(
    now: dt.datetime,
    end: dt.datetime,
    *,
    exists: bool,
    last_dispatch: dt.datetime | None,
    grace_minutes: int,
    max_hours: int,
    cooldown_minutes: int,
) -> tuple[str, str]:
    """返回 (动作, 原因)。动作是 fire / wait / done / expired / cooling。

    抽成纯函数是为了能离线测清楚边界，不用真去动线上。
    """
    if exists:
        return "done", "产物已存在"
    if now < end + dt.timedelta(minutes=grace_minutes):
        return "wait", f"还没到 {end + dt.timedelta(minutes=grace_minutes):%H:%M}"
    if now > end + dt.timedelta(hours=max_hours):
        return "expired", f"已过 {end + dt.timedelta(hours=max_hours):%m-%d %H:%M}，放弃当天"
    if last_dispatch and now - last_dispatch < dt.timedelta(minutes=cooldown_minutes):
        return "cooling", f"{(now - last_dispatch).seconds // 60} 分钟前刚触发过"
    return "fire", "缺失，触发"


def dispatch(workflow: str, day: dt.date, token: str) -> None:
    status, body = _request(
        f"/repos/{REPO}/actions/workflows/{workflow}/dispatches",
        token,
        method="POST",
        body={"ref": "main", "inputs": {"date": day.isoformat()}},
    )
    if status not in (201, 204):
        raise RuntimeError(f"触发 {workflow} 失败：HTTP {status} {body[:200].decode('utf-8', 'replace')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印判断结果，不触发")
    parser.add_argument("--pull", action="store_true", help="先 git pull，拿最新配置")
    parser.add_argument("--grace-minutes", type=int, default=15)
    parser.add_argument("--max-hours", type=int, default=6)
    parser.add_argument("--cooldown-minutes", type=int, default=30)
    parser.add_argument("--state-file", default="/var/lib/digest-watchdog/state.json")
    args = parser.parse_args()

    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token and not args.dry_run:
        print("! 缺少 GITHUB_TOKEN，无法检查远端", file=sys.stderr)
        return 2

    if args.pull and not args.dry_run:
        subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", "--quiet"], check=False)

    state_path = pathlib.Path(args.state_file)
    state: dict[str, str] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    now = dt.datetime.now(BEIJING)
    day = now.date()
    fired = False

    for profile, slot, workflow in TARGETS:
        label = f"{profile}-{slot}" if slot else profile
        end = window_end(profile, slot, day)
        path = artifact_path(profile, slot, day)
        exists = remote_exists(path, token) if token else False
        last = state.get(label)
        last_dt = dt.datetime.fromisoformat(last).astimezone(BEIJING) if last else None

        action, reason = decide(
            now,
            end,
            exists=exists,
            last_dispatch=last_dt,
            grace_minutes=args.grace_minutes,
            max_hours=args.max_hours,
            cooldown_minutes=args.cooldown_minutes,
        )
        print(f"{now:%H:%M} {label:16} {action:8} {reason}  ({path})")

        if action == "fire":
            if args.dry_run:
                print(f"           [dry-run] 会触发 {workflow} date={day}")
            else:
                try:
                    dispatch(workflow, day, token)
                    state[label] = now.isoformat()
                    fired = True
                except Exception as exc:  # noqa: BLE001 - 单条失败不该拖垮其它时段
                    print(f"! 触发失败：{exc}", file=sys.stderr)

    if fired and not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
