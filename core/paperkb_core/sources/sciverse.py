"""Sciverse — 元数据增强层 / 第二核实源（paperkb_core.sources.sciverse）。

设计依据：docs/sciverse-integration-assessment.md（35 篇真实文献实测）。

两种角色
--------
1. **增强**（主用途）：给已由 BibTeX / CrossRef / PubMed 校验过的记录，补上
   本流水线现在完全没有的四类字段 —— ``keywords`` / 作者 ``orcid`` /
   ``citation_count`` / ``fwci``。**只填空，绝不改写已有值。**
2. **末位兜底**：BibTeX / CrossRef / PubMed 全部失败时，提供
   title / year / venue，让记录不至于空手而归。

硬约束（均来自既定决策，勿在此文件中放宽）
------------------------------------------
* **不替代 JOURNAL_ABBREV_MAP**。``publication_venue_name_unified`` 是期刊
  *全称*，与 CrossRef 缩写实测仅 12/35 一致。本模块从不产出 journal_abbrev。
* **不下载 PDF**。OA 字段只用于*标注可得性*，绝不触发下载。
* **不引入 LLM 判定**。全部为纯规则字段比对。
* **降级优先**。无 key / 401 / 429 / 无结果 → 返回 None 并让主流程继续。
  本模块是增强项，任何情况下都不得成为单点故障。

两个必须遵守的接口行为（实测发现，官方文档未明写）
--------------------------------------------------
1. **影响力字段默认不返回，须显式投影**：不传 ``fields`` 时
   ``citation_count`` / ``fwci`` / ``access_is_oa`` 全部缺省。见 ``FIELDS``。
2. **布尔字段返回字符串** ``'true'`` / ``'false'``，不是 JSON 布尔。
   用 ``v is True`` 判断会静默全落空 —— 一律走 ``_as_bool()``。

凭证
----
由 ``paperkb_core.credentials`` 提供（``[sciverse] api_key``，或旧的
``SCIVERSE_API_KEY`` / ``SCIVERSE_API_TOKEN`` 环境变量）。未配置即整体降级。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .. import credentials, http

log = logging.getLogger("paperkb.sciverse")

# ═══════════════════════════════════════════════════════════════════════
# 接口常量
# ═══════════════════════════════════════════════════════════════════════

BASE_URL = os.environ.get("SCIVERSE_BASE_URL", "https://api.sciverse.space")
SEARCH_PATH = "/meta-search"
TIMEOUT = 45

# 429 处理：尊重 Retry-After，但设上限，避免脚本被挂死
MAX_RETRY_AFTER = 30
MAX_RETRIES = 1          # 一次重试后放弃 —— 文档明确要求不要并发重试
REQ_INTERVAL = 0.35      # 串行请求间隔

# 必须显式投影的字段（见模块 docstring 第 1 条）
FIELDS = [
    "unique_id",
    "doi",
    "title",
    "publication_published_year",
    "publication_venue_name_unified",
    "author",
    "keywords",
    "citation_count",
    "fwci",
    "influential_citation_count",
    "access_is_oa",
    "access_oa_status",
    "access_oa_url",
]
# 注意：``doc_id`` 与 ``is_content_accessible`` **不需要**（也不能）投影 ——
# 实测无论传什么 ``fields``，这两个键都必然返回；而把
# ``is_content_accessible`` 写进 ``fields`` 会被拒：
# ``400 INVALID_REQUEST 未知字段``。它可返回但不可投影。

# 本模块只填这些字段进已校验记录。刻意不含 journal / journal_abbrev /
# title / year —— 那些以 CrossRef（DOI 注册方，一手来源）为准。
ENRICH_FIELDS = ("keywords", "citation_count", "fwci",
                 "influential_citation_count", "author_orcids",
                 "oa_status", "oa_is_oa", "sciverse_doc_id")

# venue 噪声：实测预印本条目会带网页抓取残渣，例如
# 'ChemRxivActionsSearch in PubMedSearch in NLM Catalog'
VENUE_NOISE_RE = re.compile(
    r"search in |actions|add to search|\bnlm catalog\b", re.IGNORECASE)
PREPRINT_RE = re.compile(
    r"biorxiv|medrxiv|chemrxiv|arxiv|research\s*square|preprints?\b",
    re.IGNORECASE)


class _State:
    """整轮运行的降级状态。一旦判定不可用就不再重试，避免逐条刷错误。"""
    disabled: bool = False
    reason: str = ""
    warned_no_token: bool = False
    calls: int = 0


STATE = _State()


# ═══════════════════════════════════════════════════════════════════════
# 基础工具
# ═══════════════════════════════════════════════════════════════════════

def _token() -> str:
    """API key from the credentials module (file first, legacy env vars as fallback)."""
    return credentials.sciverse_api_key()


def _as_bool(v: Any) -> Optional[bool]:
    """布尔字段是字符串 'true'/'false'（见 docstring 第 2 条）。

    返回 True / False / None（未知或未投影）。
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _as_int(v: Any) -> Optional[int]:
    """整数型字段实测返回**浮点**：``publication_published_year`` → ``2021.0``，
    ``citation_count`` → ``239.0``（留档 CSV 亦如此，是一贯行为）。

    不强制取整会让年份写进 BibTeX 变成 ``2020.0``。``fwci`` 是真实小数，
    不走这里。
    """
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def normalize_doi(doi: str) -> str:
    """去前缀 + 转小写。

    服务端本身会归一化（实测三种写法等价），这里做一遍只为让日志与
    比对键稳定，不是接口要求。
    """
    d = (doi or "").strip()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE)
    return d.strip().lower()


def venue_is_trustworthy(venue: str) -> tuple[bool, str]:
    """venue 是否可采信。纯规则。

    实测预印本的 venue 带网页抓取噪声，不可盲信 —— 这类应以 CrossRef 或
    本地 bibtex 为准。
    """
    v = (venue or "").strip()
    if not v:
        return False, "empty"
    if VENUE_NOISE_RE.search(v):
        return False, "scraping_noise"
    if PREPRINT_RE.search(v):
        return False, "preprint"
    if len(v) > 120:
        return False, "suspicious_length"
    return True, "ok"


# ═══════════════════════════════════════════════════════════════════════
# HTTP —— 降级是这一层的第一要务
# ═══════════════════════════════════════════════════════════════════════

def _post(payload: dict) -> tuple[Optional[int], Any]:
    req = urllib.request.Request(
        BASE_URL + SEARCH_PATH,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with http.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body: Any
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        # Retry-After 只在 429 时出现（响应头无 X-RateLimit-*）
        retry_after = e.headers.get("Retry-After") if e.headers else None
        if retry_after is None and isinstance(body, dict):
            retry_after = body.get("retry_after")
        return e.code, {"_error": body, "_retry_after": retry_after}
    except Exception as e:                      # 超时 / DNS / 代理阻断
        return None, {"_error": f"{type(e).__name__}: {e}"}


def sciverse_search(payload: dict) -> Optional[dict]:
    """调用 /meta-search。任何失败都返回 None，绝不抛异常给主流程。"""
    if STATE.disabled:
        return None

    if not _token():
        if not STATE.warned_no_token:
            log.info("  ℹ️ 未设置 SCIVERSE_API_KEY —— 跳过 Sciverse 增强（不影响校验）")
            STATE.warned_no_token = True
        STATE.disabled = True
        STATE.reason = "no_token"
        return None

    attempt = 0
    while True:
        STATE.calls += 1
        status, body = _post(payload)

        if status == 200 and isinstance(body, dict):
            return body

        if status in (401, 403):
            log.warning("  ⚠️ Sciverse 鉴权失败（HTTP %s）—— 本轮停用，校验继续", status)
            STATE.disabled = True
            STATE.reason = f"auth_{status}"
            return None

        if status == 429:
            wait = body.get("_retry_after") if isinstance(body, dict) else None
            try:
                wait = min(float(wait), MAX_RETRY_AFTER) if wait else 5.0
            except (TypeError, ValueError):
                wait = 5.0
            if attempt >= MAX_RETRIES:
                log.warning("  ⚠️ Sciverse 配额受限（429）—— 本轮停用，校验继续")
                STATE.disabled = True
                STATE.reason = "rate_limited"
                return None
            log.info("  ⏳ Sciverse 429，等待 %.0fs 后重试一次", wait)
            time.sleep(wait)
            attempt += 1
            continue

        if status == 400:
            # 请求形状错（字段名/算子名不对）会在每一条上重复出现，
            # 不是"未收录"。立即停用并报明服务端原文，便于定位。
            msg = ""
            if isinstance(body, dict) and isinstance(body.get("_error"), dict):
                msg = body["_error"].get("message", "")
            log.error("  ✗ Sciverse 请求被拒（400）：%s —— 本轮停用（这是代码问题，"
                      "不是文献未收录）", msg or "无消息")
            STATE.disabled = True
            STATE.reason = f"bad_request: {msg}" if msg else "bad_request"
            return None

        log.warning("  ⚠️ Sciverse 查询失败（status=%s）—— 跳过本条", status)
        return None


# ═══════════════════════════════════════════════════════════════════════
# 按 DOI 查询
# ═══════════════════════════════════════════════════════════════════════

def lookup_by_doi(doi: str) -> Optional[dict]:
    """按 DOI 精确查一条。返回归一化后的 dict，未命中/失败返回 None。"""
    nd = normalize_doi(doi)
    if not nd:
        return None

    body = sciverse_search({
        "filters": [{"field": "doi",
                     "operator": "FILTER_OP_EQ",
                     "value": nd}],
        "fields": FIELDS,
        "page_size": 1,
    })
    if not body:
        return None

    results = body.get("results") or []
    if not results:
        log.info("  ℹ️ Sciverse 未收录：%s", nd)
        return None

    return _normalize_record(results[0], nd)


def _normalize_record(r: dict, queried_doi: str) -> dict:
    authors, orcids = [], []
    for a in r.get("author") or []:
        if isinstance(a, dict):
            nm = (a.get("name") or "").strip()
            if nm:
                authors.append(nm)
            oid = (a.get("orcid") or "").strip()
            if oid:
                orcids.append(oid)
        elif isinstance(a, str) and a.strip():
            authors.append(a.strip())

    kws = [k for k in (r.get("keywords") or []) if isinstance(k, str) and k.strip()]
    venue = (r.get("publication_venue_name_unified") or "").strip()
    trust, why = venue_is_trustworthy(venue)

    return {
        "source": "sciverse",
        "queried_doi": queried_doi,
        "doi": normalize_doi(r.get("doi") or queried_doi),
        "unique_id": r.get("unique_id") or "",
        "title": (r.get("title") or "").strip(),
        "year": _as_int(r.get("publication_published_year")),
        # 刻意命名为 venue 而非 journal：提醒调用方它是全称，不是缩写，
        # 且不参与 JOURNAL_ABBREV_MAP 的职责。
        "venue": venue,
        "venue_trustworthy": trust,
        "venue_reject_reason": "" if trust else why,
        "authors": authors,
        "author_orcids": orcids,
        "keywords": kws,
        "citation_count": _as_int(r.get("citation_count")),
        "fwci": r.get("fwci"),          # 真实小数，不取整
        "influential_citation_count": _as_int(r.get("influential_citation_count")),
        "oa_is_oa": _as_bool(r.get("access_is_oa")),
        "oa_status": r.get("access_oa_status") or "",
        # 仅标注可得性，绝不用于下载
        "oa_url": r.get("access_oa_url") or "",
        "sciverse_doc_id": r.get("doc_id") or "",
        "has_fulltext": bool(r.get("doc_id")) or bool(
            _as_bool(r.get("is_content_accessible"))),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ═══════════════════════════════════════════════════════════════════════
# 增强：只填空，不改写
# ═══════════════════════════════════════════════════════════════════════

def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    return False


def enrich_verified(verified: dict, doi: str = "") -> dict:
    """给已校验记录补 Sciverse 独有字段。

    契约：
      * **绝不改写** ``verified`` 中已有非空值 —— CrossRef 是 DOI 注册方，
        一手来源，冲突时以它为准。
      * Sciverse 不可用时原样返回，只在 ``sciverse.status`` 记录原因。
      * 产出的所有新字段都收在 ``verified["sciverse"]`` 之下，
        主记录只增加 ``ENRICH_FIELDS`` 里那几项，便于回溯来源。
    """
    out = dict(verified)
    d = doi or verified.get("doi") or ""

    if not d:
        out["sciverse"] = {"status": "skipped", "reason": "no_doi"}
        return out

    rec = lookup_by_doi(d)
    if rec is None:
        out["sciverse"] = {
            "status": "unavailable" if STATE.disabled else "not_found",
            "reason": STATE.reason or "no_result",
        }
        return out

    filled, skipped, conflicts = [], [], []

    for f in ENRICH_FIELDS:
        val = rec.get(f)
        if _is_blank(val):
            continue
        if _is_blank(out.get(f)):
            out[f] = val
            filled.append(f)
        else:
            skipped.append(f)

    # 交叉校验（只报告，不改写）—— 这是"第三校验源"的实际含义
    if rec.get("year") and verified.get("year"):
        try:
            if int(rec["year"]) != int(verified["year"]):
                conflicts.append(
                    f"year: 本地/CrossRef[{verified['year']}] vs sciverse[{rec['year']}]")
        except (TypeError, ValueError):
            pass

    if rec["venue_trustworthy"] and verified.get("journal"):
        a = rec["venue"].strip().lower().replace("&amp;", "&").replace(",", "")
        b = str(verified["journal"]).strip().lower().replace("&amp;", "&").replace(",", "")
        if a and b and a != b:
            conflicts.append(f"venue: crossref[{verified['journal']}] vs sciverse[{rec['venue']}]")

    out["sciverse"] = {
        "status": "ok",
        "unique_id": rec["unique_id"],
        "filled": filled,
        "skipped_existing": skipped,
        "conflicts": conflicts,
        "venue": rec["venue"],
        "venue_trustworthy": rec["venue_trustworthy"],
        "venue_reject_reason": rec["venue_reject_reason"],
        "has_fulltext": rec["has_fulltext"],
        "verified_at": rec["verified_at"],
    }
    for c in conflicts:
        log.warning("  ⚠️ Sciverse 交叉校验冲突（未改写）：%s", c)
    return out


def verify_by_sciverse(doi: str) -> Optional[dict]:
    """末位兜底：前三源全失败时，构造一条与 verify_by_doi 同形的记录。

    刻意**不填** ``journal_abbrev`` —— 缩写仍由 JOURNAL_ABBREV_MAP 负责。
    venue 不可采信时（预印本 / 抓取噪声）留空而非填入脏值。
    """
    rec = lookup_by_doi(doi)
    if rec is None:
        return None
    return {
        "title": rec["title"],
        "authors": rec["authors"],
        "first_author": rec["authors"][0] if rec["authors"] else "",
        "year": rec["year"],
        "journal": rec["venue"] if rec["venue_trustworthy"] else "",
        "doi": rec["doi"],
        "url": f"https://doi.org/{rec['doi']}" if rec["doi"] else "",
        "source": "sciverse",
        "keywords": rec["keywords"],
        "author_orcids": rec["author_orcids"],
        "citation_count": rec["citation_count"],
        "fwci": rec["fwci"],
        "verified_at": rec["verified_at"],
        "_venue_rejected": rec["venue_reject_reason"],
    }
