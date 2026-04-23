#!/usr/bin/env python3
"""
检查参考文献格式并验证文献是否真实存在。

功能：
1) 基础格式检查（作者-年份-标题-来源，启发式）
2) 自动提取 DOI / PMID / arXiv 并联网验证
3) 若无标识符，使用标题在 Crossref 检索
4) 输出 JSON 或表格结果

示例：
  python check_references.py refs.txt
  python check_references.py refs.txt --out result.json --json
  python check_references.py refs.txt --mailto your@email.com
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{5,9})\b", re.I)
ARXIV_RE = re.compile(r"\barXiv\s*:?\s*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b", re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class ReferenceResult:
    line_no: int
    raw: str
    format_score: int
    format_warnings: list[str]
    detected_id_type: str | None
    detected_id_value: str | None
    exists: bool
    confidence: str
    evidence: str
    matched_title: str | None
    matched_source: str | None
    matched_year: str | None


def http_get_json(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> Any:
    req_headers = {
        "User-Agent": "RefChecker/1.0 (+https://example.org)",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def quick_format_check(ref: str) -> tuple[int, list[str]]:
    score = 100
    warnings: list[str] = []
    text = normalize_text(ref)

    if len(text) < 20:
        score -= 50
        warnings.append("长度过短，可能不是完整参考文献")

    if not YEAR_RE.search(text):
        score -= 20
        warnings.append("未检测到年份")

    if text.count(".") < 2 and text.count(",") < 2:
        score -= 20
        warnings.append("标点结构较弱，格式可能不完整")

    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        score -= 30
        warnings.append("未检测到有效文字")

    # 简单作者线索：逗号前有英文姓氏或中文名
    if not re.match(r"^[\s\[\(]*(?:[A-Z][a-z\-']+|[\u4e00-\u9fff]{2,4})", text):
        score -= 10
        warnings.append("开头不像作者字段（启发式）")

    return max(score, 0), warnings


def check_doi(doi: str, mailto: str | None) -> tuple[bool, str, str | None, str | None, str | None]:
    doi_q = urllib.parse.quote(doi)
    url = f"https://api.crossref.org/works/{doi_q}"
    headers = {"User-Agent": f"RefChecker/1.0 (mailto:{mailto})"} if mailto else None
    try:
        data = http_get_json(url, headers=headers)
        item = data.get("message", {})
        title = (item.get("title") or [None])[0]
        source = item.get("container-title", [None])[0] if isinstance(item.get("container-title"), list) else None
        year = None
        for fld in ("published-print", "published-online", "created"):
            parts = item.get(fld, {}).get("date-parts", [])
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        return True, "Crossref DOI 命中", title, source, year
    except Exception as e:
        return False, f"DOI 查询失败: {e}", None, None, None


def check_pmid(pmid: str) -> tuple[bool, str, str | None, str | None, str | None]:
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={urllib.parse.quote(pmid)}&retmode=json"
    )
    try:
        data = http_get_json(url)
        result = data.get("result", {})
        rec = result.get(str(pmid))
        if not rec:
            return False, "PubMed 未找到 PMID", None, None, None
        title = rec.get("title")
        source = rec.get("fulljournalname") or rec.get("source")
        year = (rec.get("pubdate") or "")[:4] if rec.get("pubdate") else None
        return True, "PubMed PMID 命中", title, source, year
    except Exception as e:
        return False, f"PMID 查询失败: {e}", None, None, None


def check_arxiv(arxiv_id: str) -> tuple[bool, str, str | None, str | None, str | None]:
    # arXiv API 返回 Atom XML，这里做轻量字符串解析（避免额外依赖）
    qid = urllib.parse.quote(arxiv_id)
    url = f"https://export.arxiv.org/api/query?search_query=id:{qid}&max_results=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RefChecker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
        if "<entry>" not in xml:
            return False, "arXiv 未找到记录", None, None, None
        title_m = re.search(r"<title>(.*?)</title>", xml, re.S)
        pub_m = re.search(r"<published>(\d{4})-", xml)
        title = normalize_text(title_m.group(1)) if title_m else None
        year = pub_m.group(1) if pub_m else None
        return True, "arXiv ID 命中", title, "arXiv", year
    except Exception as e:
        return False, f"arXiv 查询失败: {e}", None, None, None


def infer_title_for_search(ref: str) -> str:
    # 去掉 DOI/PMID/arXiv 后，取中间较长片段作为标题线索
    text = DOI_RE.sub("", ref)
    text = PMID_RE.sub("", text)
    text = ARXIV_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" .;,")
    # 按句号切分，优先选长度较长片段
    parts = [p.strip() for p in re.split(r"[.;]", text) if p.strip()]
    if not parts:
        return text
    parts.sort(key=len, reverse=True)
    return parts[0][:250]


def check_by_title(ref: str, mailto: str | None) -> tuple[bool, str, str | None, str | None, str | None]:
    title_query = infer_title_for_search(ref)
    if len(title_query) < 8:
        return False, "标题线索不足，无法检索", None, None, None

    q = urllib.parse.quote(title_query)
    url = f"https://api.crossref.org/works?query.title={q}&rows=3"
    headers = {"User-Agent": f"RefChecker/1.0 (mailto:{mailto})"} if mailto else None
    try:
        data = http_get_json(url, headers=headers)
        items = data.get("message", {}).get("items", [])
        if not items:
            return False, "Crossref 标题检索无结果", None, None, None
        best = items[0]
        title = (best.get("title") or [None])[0]
        source = best.get("container-title", [None])[0] if isinstance(best.get("container-title"), list) else None
        year = None
        for fld in ("published-print", "published-online", "created"):
            parts = best.get(fld, {}).get("date-parts", [])
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        return True, "Crossref 标题检索疑似命中", title, source, year
    except Exception as e:
        return False, f"标题检索失败: {e}", None, None, None


def check_reference(line: str, line_no: int, mailto: str | None = None, sleep_s: float = 0.0) -> ReferenceResult:
    raw = line.rstrip("\n")
    score, warnings = quick_format_check(raw)

    detected_type = None
    detected_value = None
    exists = False
    confidence = "low"
    evidence = ""
    m_title = m_source = m_year = None

    doi_m = DOI_RE.search(raw)
    pmid_m = PMID_RE.search(raw)
    arxiv_m = ARXIV_RE.search(raw)

    if doi_m:
        detected_type, detected_value = "DOI", doi_m.group(0)
        exists, evidence, m_title, m_source, m_year = check_doi(detected_value, mailto)
        confidence = "high" if exists else "medium"
    elif pmid_m:
        detected_type, detected_value = "PMID", pmid_m.group(1)
        exists, evidence, m_title, m_source, m_year = check_pmid(detected_value)
        confidence = "high" if exists else "medium"
    elif arxiv_m:
        detected_type, detected_value = "arXiv", arxiv_m.group(1)
        exists, evidence, m_title, m_source, m_year = check_arxiv(detected_value)
        confidence = "high" if exists else "medium"
    else:
        exists, evidence, m_title, m_source, m_year = check_by_title(raw, mailto)
        confidence = "medium" if exists else "low"

    if sleep_s > 0:
        time.sleep(sleep_s)

    return ReferenceResult(
        line_no=line_no,
        raw=raw,
        format_score=score,
        format_warnings=warnings,
        detected_id_type=detected_type,
        detected_id_value=detected_value,
        exists=exists,
        confidence=confidence,
        evidence=evidence,
        matched_title=m_title,
        matched_source=m_source,
        matched_year=m_year,
    )


def iter_references(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    # 忽略空行和注释行
    return [ln for ln in lines if ln and not ln.startswith("#")]


def print_table(results: list[ReferenceResult]) -> None:
    header = f"{'#':<4} {'格式分':<6} {'存在性':<6} {'置信度':<8} {'ID':<22} 证据"
    print(header)
    print("-" * len(header))
    for r in results:
        id_text = f"{r.detected_id_type}:{r.detected_id_value}" if r.detected_id_type else "(title search)"
        id_text = id_text[:22]
        ok = "是" if r.exists else "否"
        print(f"{r.line_no:<4} {r.format_score:<6} {ok:<6} {r.confidence:<8} {id_text:<22} {r.evidence}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查参考文献格式并联网验证是否存在")
    parser.add_argument("input", help="输入文件路径（每行1条参考文献）")
    parser.add_argument("--out", help="将结果写入 JSON 文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印到标准输出")
    parser.add_argument("--mailto", help="用于 Crossref 的联系邮箱（建议提供）")
    parser.add_argument("--sleep", type=float, default=0.1, help="每条请求后休眠秒数，默认 0.1")
    args = parser.parse_args()

    refs = iter_references(args.input)
    if not refs:
        print("未读取到参考文献条目。")
        return 1

    results: list[ReferenceResult] = []
    for i, ref in enumerate(refs, start=1):
        results.append(check_reference(ref, i, mailto=args.mailto, sleep_s=max(args.sleep, 0.0)))

    payload = [asdict(r) for r in results]

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_table(results)

    # 简单统计
    total = len(results)
    exists_n = sum(1 for r in results if r.exists)
    avg_score = sum(r.format_score for r in results) / total
    print(f"\n总数: {total} | 验证存在: {exists_n} | 平均格式分: {avg_score:.1f}")

    # 返回码：全部未命中时返回 2，方便在 CI 里拦截
    return 0 if exists_n > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
