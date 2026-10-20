"""配置加载：全局 config.toml + 栏目 profiles/<id>.toml，合并成管道使用的视图。

设计要点
--------
* 全局只放「所有栏目一样」的东西：站点、输出、LLM、核心词表。
* 栏目只放「每家不一样」的东西：发布时刻、窗口、分节、源、配额、排序策略。
* 合并后的结构与改造前完全一致（site/output/quota/sections/sources/tags/llm），
  这样 pipeline 里的其他模块一行都不用改。
"""

from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GLOBAL_CONFIG = ROOT / "config.toml"
PROFILE_DIR = ROOT / "profiles"

QUOTA_DEFAULTS = {"daily": 12, "per_section": 4, "min_items": 3, "seen_days": 7}


def _read(path: pathlib.Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def available_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.toml"))


def load(profile: str = "tech", path: str | pathlib.Path | None = None, slot: str | None = None) -> dict:
    """返回合并视图。path 仅用于测试时指定全局配置。"""
    global_cfg = _read(pathlib.Path(path) if path else GLOBAL_CONFIG)

    profile_path = PROFILE_DIR / f"{profile}.toml"
    if not profile_path.exists():
        raise SystemExit(
            f"找不到栏目配置 {profile_path.relative_to(ROOT)}；"
            f"可用栏目：{', '.join(available_profiles()) or '（无）'}"
        )
    prof = _read(profile_path)

    cfg: dict = dict(global_cfg)

    # --- site：全局站点设置 + 栏目自己的时间轴 ---
    site = dict(global_cfg.get("site", {}))
    for key in ("publish_at", "window_hours", "window_slack_hours"):
        if key in prof:
            site[key] = prof[key]

    # 有时段的栏目（财经一天两期），发布时刻由时段决定
    slots = prof.get("slots", {}) or {}
    slot_label = ""
    if slots and not slot:
        raise SystemExit(
            f"栏目 {profile} 需要指定时段；可用：{', '.join(slots)}"
        )
    if slot:
        if slot not in slots:
            raise SystemExit(
                f"栏目 {profile} 没有时段 {slot}；可用：{', '.join(slots) or '（无）'}"
            )
        site["publish_at"] = slots[slot].get("publish_at", site.get("publish_at"))
        slot_label = str(slots[slot].get("label", ""))
    cfg["site"] = site

    # --- output：{profile} 占位符让每个栏目落到自己的子目录 ---
    output = dict(global_cfg.get("output", {}))
    output["content_dir"] = str(output.get("content_dir", "source/_posts")).replace(
        "{profile}", profile
    )
    cfg["output"] = output

    # --- sections / sources：栏目独有 ---
    cfg["sections"] = list(prof.get("sections", []))
    cfg["sources"] = list(prof.get("sources", []))
    cfg["other"] = dict(prof.get("other", {"title": "其他"}))

    # --- quota：全局默认 + 栏目覆盖 ---
    quota = dict(QUOTA_DEFAULTS)
    quota.update(global_cfg.get("quota", {}))
    quota.update(prof.get("quota", {}))
    cfg["quota"] = quota

    # --- tags：核心词表 + 栏目扩展 ---
    tag_cfg = dict(global_cfg.get("tags", {}))
    extra = list((prof.get("tags", {}) or {}).get("extra", []))
    tag_cfg["vocab"] = list(tag_cfg.get("vocab", [])) + extra
    cfg["tags"] = tag_cfg

    # --- 栏目元信息 ---
    cfg["profile"] = {
        "id": profile,
        "title": prof.get("title", profile),
        "category": prof.get("category", profile),
        "rank": prof.get("rank", "percentile"),
        "footer": prof.get("footer", ""),
        "summary_rules": prof.get("summary_rules", ""),
        "slot": slot,
        "slot_label": slot_label,
    }
    return cfg


def sections(cfg: dict) -> list[dict]:
    return list(cfg.get("sections", []))


def sources(cfg: dict) -> list[dict]:
    return list(cfg.get("sources", []))


def site(cfg: dict) -> dict:
    return dict(cfg.get("site", {}))


def quota(cfg: dict) -> dict:
    return dict(cfg.get("quota", {}))


def profile(cfg: dict) -> dict:
    return dict(cfg.get("profile", {}))
