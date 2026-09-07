"""多源学术检索：arXiv / Semantic Scholar / OpenAlex / Crossref。

四个源全部免费、无需 API Key。任一源失败自动跳过，不影响其它源。
统一输出记录结构，便于后续生成对比矩阵与去重。
"""

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import net

ARXIV_API = "http://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"

S2_FIELDS = "title,abstract,year,venue,authors,citationCount,externalIds,url,publicationTypes"

ALL_SOURCES = ("arxiv", "s2", "openalex", "crossref")


def _blank(title):
    return {
        "title": title or "",
        "authors": [],
        "year": None,
        "venue": "",
        "doi": "",
        "url": "",
        "abstract": "",
        "citations": None,
        "source": "",
    }


def _clean(text):
    if not text:
        return ""
    # Crossref 的部分字段带 HTML 实体（如 &amp;），必须还原
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


def _norm_title(title):
    """标题归一化，用于跨源去重。"""
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())
    return t


def _strip_tags(text):
    """Crossref 摘要常裹 JATS 标签（<jats:p> 等），去掉后才是干净文本。"""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text).strip()


def _authors_short(authors, limit=3):
    if not authors:
        return ""
    if len(authors) <= limit:
        return ", ".join(authors)
    return ", ".join(authors[:limit]) + " et al."


# ---------------------------------------------------------------- arXiv

def search_arxiv(query, limit=20, year_from=None, year_to=None):
    q = 'all:"%s"' % query
    if year_from or year_to:
        lo = year_from or 1990
        hi = year_to or 2100
        q += " AND submittedDate:[%d01010000 TO %d12312359]" % (int(lo), int(hi))
    params = {
        "search_query": q,
        "start": 0,
        "max_results": max(1, min(int(limit), 100)),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    raw = net.http_get(ARXIV_API, params, timeout=30)
    if raw is None:
        return []
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []

    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    out = []
    for entry in root.findall("a:entry", ns):
        rec = _blank(_clean(entry.findtext("a:title", "", ns)))
        rec["source"] = "arXiv"
        rec["abstract"] = _clean(entry.findtext("a:summary", "", ns))
        rec["url"] = _clean(entry.findtext("a:id", "", ns))
        published = entry.findtext("a:published", "", ns) or ""
        m = re.match(r"(\d{4})", published)
        if m:
            rec["year"] = int(m.group(1))
        for au in entry.findall("a:author", ns):
            name = _clean(au.findtext("a:name", "", ns))
            if name:
                rec["authors"].append(name)
        doi = entry.findtext("arxiv:doi", "", ns)
        if doi:
            rec["doi"] = _clean(doi)
        jref = entry.findtext("arxiv:journal_ref", "", ns)
        rec["venue"] = _clean(jref) or "arXiv preprint"
        out.append(rec)
    return out


# ------------------------------------------------------ Semantic Scholar

def search_s2(query, limit=20, year_from=None, year_to=None):
    params = {
        "query": query,
        "limit": max(1, min(int(limit), 100)),
        "fields": S2_FIELDS,
    }
    yrs = []
    if year_from:
        yrs.append(str(int(year_from)) + "-")
    if year_to:
        yrs.append("-" + str(int(year_to)))
    if year_from and year_to:
        yrs = ["%d-%d" % (int(year_from), int(year_to))]
    if yrs:
        params["year"] = yrs[0]

    data = net.http_json(S2_API, params, timeout=30)
    if not data or "data" not in data:
        return []
    out = []
    for item in data["data"]:
        rec = _blank(_clean(item.get("title")))
        rec["source"] = "SemanticScholar"
        rec["abstract"] = _clean(item.get("abstract"))
        rec["year"] = item.get("year")
        rec["venue"] = _clean(item.get("venue")) or ""
        rec["citations"] = item.get("citationCount")
        rec["url"] = _clean(item.get("url")) or ""
        ext = item.get("externalIds") or {}
        if ext.get("DOI"):
            rec["doi"] = ext["DOI"]
        if ext.get("ArXiv") and not rec["url"]:
            rec["url"] = "https://arxiv.org/abs/" + ext["ArXiv"]
        for au in (item.get("authors") or []):
            name = _clean(au.get("name"))
            if name:
                rec["authors"].append(name)
        out.append(rec)
    return out


# ------------------------------------------------------------- OpenAlex

def _openalex_abstract(inverted):
    """OpenAlex 的摘要是倒排索引，需要还原成原文。"""
    if not inverted:
        return ""
    slots = []
    for word, positions in inverted.items():
        for pos in positions:
            slots.append((pos, word))
    slots.sort()
    return _clean(" ".join(w for _, w in slots))


def search_openalex(query, limit=20, year_from=None, year_to=None):
    params = {
        "search": query,
        "per-page": max(1, min(int(limit), 100)),
        "select": "title,publication_year,doi,cited_by_count,abstract_inverted_index,primary_location,authorships",
    }
    filters = []
    if year_from:
        filters.append("from_publication_date:%d-01-01" % int(year_from))
    if year_to:
        filters.append("to_publication_date:%d-12-31" % int(year_to))
    if filters:
        params["filter"] = ",".join(filters)

    data = net.http_json(OPENALEX_API, params, timeout=30)
    if not data or "results" not in data:
        return []
    out = []
    for item in data["results"]:
        rec = _blank(_clean(item.get("title")))
        rec["source"] = "OpenAlex"
        rec["year"] = item.get("publication_year")
        rec["citations"] = item.get("cited_by_count")
        rec["abstract"] = _openalex_abstract(item.get("abstract_inverted_index"))
        doi = item.get("doi") or ""
        rec["doi"] = doi.replace("https://doi.org/", "")
        rec["url"] = doi or ""
        loc = item.get("primary_location") or {}
        src = (loc.get("source") or {})
        rec["venue"] = _clean(src.get("display_name")) or ""
        for au in (item.get("authorships") or []):
            name = _clean((au.get("author") or {}).get("display_name"))
            if name:
                rec["authors"].append(name)
        out.append(rec)
    return out


# -------------------------------------------------------------- Crossref

def search_crossref(query, limit=20, year_from=None, year_to=None):
    params = {
        "query": query,
        "rows": max(1, min(int(limit), 100)),
        "select": "title,author,issued,DOI,container-title,abstract,URL,is-referenced-by-count",
    }
    filters = []
    if year_from:
        filters.append("from-pub-date:%d-01-01" % int(year_from))
    if year_to:
        filters.append("until-pub-date:%d-12-31" % int(year_to))
    if filters:
        params["filter"] = ",".join(filters)

    data = net.http_json(CROSSREF_API, params, timeout=30)
    if not data:
        return []
    items = ((data.get("message") or {}).get("items")) or []
    out = []
    for item in items:
        titles = item.get("title") or [""]
        rec = _blank(_clean(titles[0]))
        rec["source"] = "Crossref"
        rec["doi"] = item.get("DOI") or ""
        rec["url"] = item.get("URL") or ""
        rec["citations"] = item.get("is-referenced-by-count")
        rec["abstract"] = _strip_tags(_clean(item.get("abstract") or ""))
        cont = item.get("container-title") or []
        rec["venue"] = _clean(cont[0]) if cont else ""
        issued = (item.get("issued") or {}).get("date-parts") or [[None]]
        if issued and issued[0] and issued[0][0]:
            rec["year"] = issued[0][0]
        for au in (item.get("author") or []):
            name = _clean((au.get("given") or "") + " " + (au.get("family") or ""))
            if name:
                rec["authors"].append(name)
        out.append(rec)
    return out


DISPATCH = {
    "arxiv": search_arxiv,
    "s2": search_s2,
    "openalex": search_openalex,
    "crossref": search_crossref,
}


def dedup(records):
    """跨源去重：标题归一化后保留信息最完整的一条。"""
    best = {}
    order = []
    for rec in records:
        key = _norm_title(rec.get("title"))
        if not key:
            continue
        if key not in best:
            best[key] = rec
            order.append(key)
        else:
            cur = best[key]
            if _completeness(rec) > _completeness(cur):
                merged = dict(cur)
                for field in ("abstract", "venue", "doi", "url", "citations", "year"):
                    if not merged.get(field) and rec.get(field):
                        merged[field] = rec[field]
                if not merged.get("authors") and rec.get("authors"):
                    merged["authors"] = rec["authors"]
                merged["source"] = cur.get("source", "") + "+" + rec.get("source", "")
                best[key] = merged
            else:
                cur["source"] = cur.get("source", "") + "+" + rec.get("source", "")
    return [best[k] for k in order]


def _completeness(rec):
    score = 0
    score += min(len(rec.get("abstract") or ""), 800) / 8.0
    score += 40 if rec.get("doi") else 0
    score += 30 if rec.get("venue") else 0
    score += 20 if rec.get("authors") else 0
    score += 20 if rec.get("year") else 0
    score += min(rec.get("citations") or 0, 200) / 10.0
    return score


def search(query, sources=ALL_SOURCES, limit=20, year_from=None, year_to=None):
    """统一检索入口。返回去重后的记录列表。"""
    collected = []
    for name in sources:
        fn = DISPATCH.get(name)
        if not fn:
            continue
        try:
            got = fn(query, limit=limit, year_from=year_from, year_to=year_to)
        except Exception as exc:  # noqa: BLE001 - 单源异常不影响整体
            print("[warn] %s 检索异常: %s" % (name, exc))
            got = []
        collected.extend(got)
    return dedup(collected)


def to_rows(records):
    """转成对比矩阵的初始行，未填列留空供人工补。"""
    rows = []
    for rec in records:
        rows.append({
            "year": rec.get("year") or "",
            "title": rec.get("title") or "",
            "authors": _authors_short(rec.get("authors")),
            "venue": rec.get("venue") or "",
            "method": "",
            "dataset": "",
            "core_idea": (rec.get("abstract") or "")[:220],
            "limitation": "",
            "relation": "",
            "citations": rec.get("citations") if rec.get("citations") is not None else "",
            "doi": rec.get("doi") or "",
            "url": rec.get("url") or "",
            "source": rec.get("source") or "",
        })
    return rows


def safe_url(query):
    return urllib.parse.quote(query)
