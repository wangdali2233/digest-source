"""把选品结果渲染成 Hugo 可吃的 Markdown。

frontmatter 只用 YAML 基础类型，正文只用 CommonMark，
这样换 Hexo / Astro 时内容文件一行都不用改。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from zoneinfo import ZoneInfo

from .merge import OTHER
from .normalize import canonical_url


def _permalink(day: dt.date, profile_cfg: dict | None, slot: str | None) -> str:
    """分类/年/月/日[时段].html。

    不用 Hexo 的 :year/:month 变量：05:30 的稿子在 UTC 是前一天，
    月末会直接错月。这里用本地日期，绕开所有时区运算。
    """
    category = str((profile_cfg or {}).get("category") or (profile_cfg or {}).get("id") or "posts")
    stem = day.strftime("%Y/%m/%d")
    if slot:
        stem = f"{stem}-{slot}"
    return f"{category}/{stem}.html"


def _title(site_cfg: dict, profile_cfg: dict | None) -> str:
    """文章标题用栏目名：两个栏目同一天出稿时，标题不能一样。"""
    base = str(site_cfg.get("title", "信息聚合"))
    if profile_cfg and profile_cfg.get("title"):
        base = str(profile_cfg["title"])
    label = str((profile_cfg or {}).get("slot_label") or "")
    return f"{base}{label}"


def _escape_title(title: str) -> str:
    # 标题里的方括号会打断链接语法
    return title.replace("[", "\\[").replace("]", "\\]")


def _link(title: str, url: str) -> str:
    # 尖括号目标写法，容忍 URL 里的括号（HN 标题常见）
    return f"[{_escape_title(title)}](<{url}>)"


def _yaml_str(value: str) -> str:
    return value.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()


def _meta_line(item: dict) -> str:
    """元信息行保持普通文本：斜体在中文下会渲染成伪斜体，加粗又太抢眼。"""
    parts: list[str] = []
    if item.get("points"):
        parts.append(f"{item['points']} 分")
    if item.get("comments"):
        parts.append(f"{item['comments']} 条评论")
    parts.append(item.get("host") or "链接")
    if item.get("source_name"):
        parts.append(item["source_name"])
    return " · ".join(parts)


def _heading(item: dict) -> str:
    heading = _link(item["title"], item["url"])
    discussion = item.get("discussion")
    # Ask HN 这类没有外链的条目，讨论地址就是正文地址，不必重复给
    if discussion and canonical_url(discussion) != item["canonical"]:
        heading += f" · [讨论](<{discussion}>)"
    return heading


def render(
    day: dt.date,
    picked: list[dict],
    sections_cfg: list[dict],
    site_cfg: dict,
    missing: list[str] | None = None,
    profile_cfg: dict | None = None,
    slot: str | None = None,
) -> str:
    tz = ZoneInfo(site_cfg.get("timezone", "Asia/Shanghai"))
    hour, minute = (int(x) for x in site_cfg.get("publish_at", "07:00").split(":"))
    published = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=tz)
    # Hexo 用 UTC 计算 :year-:month-:day，带 +08:00 的 07:00 会被算成前一天。
    # 写成站点本地时间（无偏移），Hexo 会按 config.timezone 解释，日期才不会漂。
    stamp = published.strftime("%Y-%m-%d %H:%M:%S")

    source_names = {item["source_name"] for item in picked if item.get("source_name")}

    # 分节名按配置顺序取，保证 description 和 tags 每天可复现
    present = {item["section"] for item in picked}
    section_ids = [s["id"] for s in sections_cfg if s["id"] in present]
    section_titles = [s["title"] for s in sections_cfg if s["id"] in present]

    description = f"今日 {len(picked)} 条 · {len(source_names)} 个源"
    if section_titles:
        description += " · " + "、".join(section_titles[:3])

    # 标签优先用模型打的（受控词表），没有就退回分节名
    counts: Counter = Counter()
    for item in picked:
        for tag in item.get("tags") or []:
            counts[tag] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    # 分节排前面：分节是版面结构，读者能从目录看到它，标签页不该缺页
    ordered = list(section_ids) + [tag for tag, _ in ranked]
    limit = int(site_cfg.get("max_front_tags", 10))
    top_tags: list[str] = []
    for tag in ordered:
        if tag not in top_tags:
            top_tags.append(tag)
        if len(top_tags) >= limit:
            break
    if not top_tags:
        top_tags = ["daily"]
    tags = ", ".join(f'"{tag}"' for tag in top_tags) or '"daily"'

    lines = [
        "---",
        f'title: "{day.isoformat()} · {_title(site_cfg, profile_cfg)}"',
        f"date: {stamp}",
        f"permalink: {_permalink(day, profile_cfg, slot)}",
        f'description: "{_yaml_str(description)}"',
        f"tags: [{tags}]",
        f'categories: ["{(profile_cfg or {}).get("category", "daily")}"]',
        "comments: false",
        "---",
        "",
        f"本期收录 {len(picked)} 条，来自 {len(source_names)} 个源。",
        "",
        "## 今日要点",
        "",
    ]

    for index, item in enumerate(picked[:3], start=1):
        lines.append(f"{index}. {_link(item['title'], item['url'])}")
    lines += ["", "<!-- more -->", ""]

    by_section: dict[str, list[dict]] = {}
    for item in picked:
        by_section.setdefault(item["section"], []).append(item)

    ordered = [(s["id"], s["title"]) for s in sections_cfg]
    ordered.append((OTHER, "其他"))

    for section_id, section_title in ordered:
        group = by_section.get(section_id)
        if not group:
            continue
        lines += [f"## {section_title}", ""]
        for item in group:
            lines += [f"### {_heading(item)}", "", _meta_line(item), ""]
            if item.get("summary"):
                lines += [item["summary"], ""]
            if item.get("tags"):
                lines += ["`#" + "` `#".join(item["tags"]) + "`", ""]
            if item.get("see_also"):
                others = "、".join(_link(x["title"], x["url"]) for x in item["see_also"])
                lines += [f"另见：{others}", ""]

    if profile_cfg and profile_cfg.get("footer"):
        lines += [str(profile_cfg["footer"]), ""]

    if missing:
        lines += [f"> 本期缺失源：{'、'.join(missing)}", ""]

    return "\n".join(lines).rstrip() + "\n"
