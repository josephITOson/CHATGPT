#!/usr/bin/env python3
"""
Search similar Chinese literature by title and year using Crossref API.
Outputs simple GB/T 7714-like references.

Usage:
  python literature_search.py --title "文章标题" --year 2020 --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Iterable, List, Optional

CROSSREF_ENDPOINT = "https://api.crossref.org/works"


def build_query(title: str, year: int, limit: int) -> str:
    params = {
        "query.title": title,
        "rows": str(limit),
        "filter": f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31",
        "select": "title,author,issued,container-title,type,URL",
    }
    return f"{CROSSREF_ENDPOINT}?{urllib.parse.urlencode(params)}"


def fetch_results(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "literature-search/1.0 (mailto:example@example.com)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def format_authors(authors: Optional[List[dict]]) -> str:
    if not authors:
        return "佚名"
    names = []
    for author in authors:
        given = author.get("given", "").strip()
        family = author.get("family", "").strip()
        if given and family:
            names.append(f"{family}{given}")
        elif family:
            names.append(family)
        elif given:
            names.append(given)
    return "，".join(names) if names else "佚名"


def format_reference(item: dict) -> str:
    title_list = item.get("title") or []
    title = title_list[0] if title_list else "(无标题)"
    authors = format_authors(item.get("author"))
    container_title = (item.get("container-title") or [""])[0]
    issued = item.get("issued", {}).get("date-parts", [[""]])[0]
    year = issued[0] if issued else ""
    url = item.get("URL", "")

    parts = [f"{authors}. {title}"]
    if container_title:
        parts.append(f"{container_title}")
    if year:
        parts.append(str(year))
    if url:
        parts.append(url)
    return ". ".join(parts)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search similar Chinese literature by title and year.")
    parser.add_argument("--title", required=True, help="文章标题")
    parser.add_argument("--year", type=int, required=True, help="年份，例如 2020")
    parser.add_argument("--limit", type=int, default=10, help="最多返回条数")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    url = build_query(args.title, args.year, args.limit)
    try:
        data = fetch_results(url)
    except Exception as exc:  # noqa: BLE001
        print(f"请求失败: {exc}", file=sys.stderr)
        return 1

    items = data.get("message", {}).get("items", [])
    if not items:
        print("未找到符合条件的文献。")
        return 0

    for index, item in enumerate(items, start=1):
        print(f"[{index}] {format_reference(item)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
