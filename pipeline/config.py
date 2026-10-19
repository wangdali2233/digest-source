"""配置加载：sources.toml → dict。"""

from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "sources.toml"


def load(path: str | pathlib.Path | None = None) -> dict:
    cfg_path = pathlib.Path(path) if path else DEFAULT_CONFIG
    with cfg_path.open("rb") as fh:
        return tomllib.load(fh)


def sections(cfg: dict) -> list[dict]:
    return list(cfg.get("sections", []))


def sources(cfg: dict) -> list[dict]:
    return list(cfg.get("sources", []))


def site(cfg: dict) -> dict:
    return dict(cfg.get("site", {}))


def quota(cfg: dict) -> dict:
    # 有默认值，配置里漏写也能跑
    base = {"daily": 12, "per_section": 4, "min_items": 3, "seen_days": 7}
    base.update(cfg.get("quota", {}))
    return base
