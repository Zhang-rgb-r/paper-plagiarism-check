# -*- coding: utf-8 -*-
"""在线自动检索查重:把论文切句后,自动到开放学术库与网页检索每句话的出处。

数据源(全部免费、无需密钥):
  * OpenAlex   —— 约 2.5 亿篇文献的标题+摘要;关键词搜索候选,本地逐字核验
  * Europe PMC —— 开放获取文献的摘要与全文,服务端精确短语检索(偏生命科学/医学)
  * 网页检索    —— 中文句子用 360/搜狗 引号短语,英文用 Bing;候选页抓取正文后
                   逐字核验,只统计核验通过的命中(经搜索引擎中转页自动跳转)

命中判定:整句逐字重复(归一化后忽略大小写、全半角、空白与标点)。
同义改写、语序调整不会命中;开放数据源覆盖有限,结果不能替代知网/Turnitin。
"""

from __future__ import annotations

import gzip
import html as _html
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"}
MAX_PAGE_BYTES = 1_500_000
MIN_SENT_CHARS = 15          # 短于该归一化长度的句子不检索(误报高、无检索价值)
MAX_SENTS = 300              # 最多检索的句子数

_NORM_RE = re.compile(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
# 中文句末标点后直接切;英文句点后必须有空白或行尾才切(避免 "1.2" 被切开)
_SENT_SPLIT = re.compile(r"(?<=[。!?！?;;])\s*|(?<=[.!?])(?:\s+|$)")
_CLAUSE_RE = re.compile(r"[,，、;;::]+")
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG2_RE = re.compile(r"<[^>]+>")

_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
         "have", "has", "had", "not", "but", "our", "can", "which", "into", "such",
         "their", "than", "also", "these", "those", "its", "his", "her", "them",
         "based", "using", "results", "show", "conclusion", "propose", "proposed"}

_locks = {k: threading.Lock()
          for k in ("openalex", "europepmc", "bing", "sogou", "so")}
_last_call = {"openalex": 0.0, "europepmc": 0.0, "bing": 0.0, "sogou": 0.0, "so": 0.0}
_sent_cache: dict[str, list] = {}          # 归一化句子 -> 命中列表(会话级缓存)
_cache_lock = threading.Lock()
_web_disabled = threading.Event()          # Bing 连续失败后自动停用


def norm(s: str) -> str:
    return _NORM_RE.sub("", unicodedata.normalize("NFKC", s).casefold())


def match_parts(nkey: str, sentence: str, hay: str):
    """分级核验:整句逐字优先,否则按逗号切出的小句(>=15字)逐字匹配。

    返回 (命中的归一化片段, 对应原文片段) 或 None。用于容忍
    "总之,"、"综上所述," 这类拼接前缀不破坏命中。
    """
    if nkey in hay:
        return nkey, sentence
    for part in _CLAUSE_RE.split(sentence):
        pn = norm(part)
        if len(pn) >= MIN_SENT_CHARS and pn in hay:
            return pn, part.strip()
    return None


# ---------------------------------------------------------------- 基础工具

def http_get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    last_err = None
    for attempt in range(2):  # 429/503 限流时自动退避重试一次
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_PAGE_BYTES)
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503) and attempt == 0:
                time.sleep(2.5)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise last_err


def _pace(source: str, min_interval: float) -> None:
    """同一数据源的请求全局限速。"""
    with _locks[source]:
        wait = _last_call[source] + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[source] = time.time()


def html_to_text(data: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1", errors="replace")
    text = _TAG_RE.sub(" ", text)
    text = _TAG2_RE.sub(" ", text)
    return _html.unescape(text)


def find_window(raw: str, n_raw: str, nkey: str, size: int = 300) -> str:
    """在原文里定位归一化命中的大致位置,返回可读片段。"""
    step = max(1, size // 2)
    for i in range(0, max(1, len(raw) - 1), step):
        chunk = raw[i:i + size * 2]
        if nkey in norm(chunk):
            return chunk.strip()[:size]
    return raw[:size]


def keywords(sentence: str) -> list[str]:
    latin = [w for w in re.findall(r"[A-Za-z]{3,}", sentence) if w.lower() not in _STOP]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", sentence)
    out = []
    if cjk:
        out.append("".join(cjk)[:14])
    out += sorted(set(latin), key=len, reverse=True)[:6]
    return out


# ---------------------------------------------------------------- 切句

def split_sentences(paper) -> list[dict]:
    """按中英文句号切句,记录每句的 (段落号, 段内起止字符)。"""
    sents, seen = [], set()
    for pi, para in enumerate(paper.paras):
        idx = 0
        for piece in _SENT_SPLIT.split(para):
            if not piece:
                continue
            start = para.find(piece, idx)
            idx = start + len(piece)
            nkey = norm(piece)
            if len(nkey) < MIN_SENT_CHARS or nkey in seen:
                continue
            seen.add(nkey)
            sents.append({"text": piece.strip(), "para": pi,
                          "start": start, "end": start + len(piece), "nkey": nkey})
        if len(sents) >= MAX_SENTS:
            break
    return sents[:MAX_SENTS]


# ---------------------------------------------------------------- 数据源

def rebuild_abstract(inv) -> str:
    if not inv:
        return ""
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    text = " ".join(pos[i] for i in sorted(pos))
    return re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", text)


def check_openalex(nkey: str, sentence: str) -> list[dict]:
    words = keywords(sentence)
    if not words:
        return []
    q = urllib.parse.quote(" ".join(words))
    url = ("https://api.openalex.org/works?filter=title_and_abstract.search:" + q +
           "&per-page=25&mailto=local-plagiarism-check@example.com")
    _pace("openalex", 0.15)
    data = json.loads(http_get(url, timeout=12).decode("utf-8", "replace"))
    hits = []
    for w in data.get("results", []):
        title = w.get("display_name") or "(无题名)"
        ab = rebuild_abstract(w.get("abstract_inverted_index"))
        hay = norm(title) + "\x00" + norm(ab)
        m = match_parts(nkey, sentence, hay)
        if m:
            pn, ptext = m
            raw = title + "。" + ab
            hits.append({
                "source": "openalex", "source_label": "OpenAlex 学术库",
                "title": title,
                "url": w.get("doi") or w.get("id"),
                "meta": " / ".join(x for x in [
                    (w.get("publication_year") and str(w["publication_year"])) or "",
                    ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
                ] if x),
                "matched_len": len(pn),
                "snippet": find_window(raw, norm(raw), pn)[:200],
            })
            if len(hits) >= 2:
                break
    return hits


def _pmc_queries(sentence: str) -> list[str]:
    """PMC 短语查询对超长短语/连字符词不可靠:整句前缀短语 + 关键词双策略。"""
    phrase = re.sub(r'["\[\]{}]', " ", sentence).strip()[:110]
    queries = ['"%s"' % phrase]
    words = [w for w in re.findall(r"[A-Za-z]{2,}", sentence) if w.lower() not in _STOP]
    if len(words) >= 4:
        queries.append(" ".join(words[:8]))
    return queries


def check_europepmc(nkey: str, sentence: str) -> list[dict]:
    if len(re.findall(r"[A-Za-z]{2,}", sentence)) < 4:  # 中文句在此库无意义,跳过
        return []
    hits: list[dict] = []
    seen_urls: set[str] = set()
    for qtext in _pmc_queries(sentence):
        if len(hits) >= 2:
            break
        q = urllib.parse.quote(qtext)
        url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=" + q +
               "&format=json&pageSize=6&resultType=core")
        _pace("europepmc", 0.3)
        try:
            data = json.loads(http_get(url, timeout=12).decode("utf-8", "replace"))
        except Exception:
            continue
        for h in data.get("resultList", {}).get("result", [])[:6]:
            title = re.sub(r"<[^>]+>", "", h.get("title") or "(无题名)")
            abstract = re.sub(r"<[^>]+>", "", h.get("abstractText") or "")
            raw = title + "。" + abstract
            hay = norm(raw)
            # PMC 的短语检索带词干匹配,过松;只统计能在题录/摘要中逐字核验的命中
            m = match_parts(nkey, sentence, hay)
            if not m:
                continue
            pn, ptext = m
            url_out = "https://europepmc.org/article/%s/%s" % (h.get("source", "MED"), h.get("id", ""))
            if url_out in seen_urls:
                continue
            seen_urls.add(url_out)
            hits.append({
                "source": "europepmc", "source_label": "Europe PMC 开放全文",
                "title": title,
                "url": url_out,
                "meta": " / ".join(x for x in [h.get("journalTitle", ""), h.get("pubYear", ""), "摘要"] if x),
                "matched_len": len(pn),
                "snippet": find_window(raw, hay, pn)[:200],
            })
            if len(hits) >= 2:
                break
    return hits


_BING_ALGO_RE = re.compile(
    r'<li class="b_algo".*?<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)
_H3_RE = re.compile(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)

_PUNCT_RE = re.compile(r"[。,，、;;:!?！？\s\"'“”‘’()（）【】\[\]<>《》·]+")

_WEB_SEARCH_PACE = {"bing": 0.8, "sogou": 0.8, "so": 0.8}


def _strip_punct(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def _candidates_360(sentence: str) -> list[tuple[str, str, str]]:
    q = urllib.parse.quote('"%s"' % _strip_punct(sentence)[:40])
    _pace("so", _WEB_SEARCH_PACE["so"])
    page = http_get("https://www.so.com/s?q=" + q, timeout=10).decode("utf-8", "replace")
    out = []
    for m in _H3_RE.finditer(page):
        u = m.group(1).replace("&amp;", "&")
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append(("360", title[:120], u if u.startswith("http") else "https://www.so.com" + u))
    return out


def _candidates_sogou(sentence: str) -> list[tuple[str, str, str]]:
    q = urllib.parse.quote('"%s"' % _strip_punct(sentence)[:40])
    _pace("sogou", _WEB_SEARCH_PACE["sogou"])
    page = http_get("https://www.sogou.com/web?query=" + q, timeout=10).decode("utf-8", "replace")
    out = []
    for m in _H3_RE.finditer(page):
        u = m.group(1).replace("&amp;", "&")
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if u.startswith("/link"):
            u = "https://www.sogou.com" + u
        out.append(("sogou", title[:120], u))
    return out


def _candidates_bing(sentence: str) -> list[tuple[str, str, str]]:
    q = urllib.parse.quote('"%s"' % sentence.strip()[:80])
    _pace("bing", _WEB_SEARCH_PACE["bing"])
    page = http_get("https://www.bing.com/search?q=%s&count=15" % q, timeout=10).decode("utf-8", "replace")
    out = []
    for u, t in _BING_ALGO_RE.findall(page):
        title = _html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        out.append(("bing", title[:120], u.replace("&amp;", "&")))
    return out


def _resolve_interstitial(url: str, html_text: str) -> str | None:
    """搜索引擎中转页 -> 真实地址;不是中转页返回 None。"""
    if "so.com/link" in url or "sogou.com/link" in url:
        m = (re.search(r'window\.location\.replace\("([^"]+)"\)', html_text)
             or re.search(r"window\.location\.replace\('([^']+)'\)", html_text)
             or re.search(r'http-equiv="refresh"[^>]*url=(?:\'|&quot;)?([^">]+)', html_text, re.I))
        if m:
            return m.group(1).replace("\\/", "/").replace("&amp;", "&")
    return None


def _fetch_page_text(engine: str, url: str, page_cache: dict) -> str:
    """抓取候选页正文(纯文本);中转页自动跳转;结果按 URL 缓存。"""
    if url in page_cache:
        return page_cache[url]
    raw = ""
    hops = 0
    while url and hops < 3:
        hops += 1
        try:
            data = http_get(url, timeout=9)
            html_text = data.decode("utf-8", "replace")
        except Exception:
            break
        target = _resolve_interstitial(url, html_text)
        if target and target.startswith("http"):
            url = target
            continue
        raw = html_to_text(data)
        break
    page_cache[url] = raw if raw else ""
    return page_cache[url]


def check_web(nkey: str, sentence: str, page_cache: dict, fetch_limit: int = 5) -> list[dict]:
    """多引擎检索候选网页,抓取正文后逐字核验;只返回核验通过的命中。

    中文句子先用整句在 360/搜狗 检索;若无核验命中且句子较长,
    再用句中一段 14 字重查一次 360,尽量提高召回(搜索引擎对中文
    引号短语只是松散匹配,网页渠道为尽力检索)。
    """
    if _web_disabled.is_set():
        return []
    clean = _strip_punct(sentence)

    def sweep(queries: list[tuple[str, str]], limit: int) -> list[dict]:
        hits, seen = [], set()
        for engine, query in queries:
            if len(hits) >= 2:
                break
            try:
                if engine == "360":
                    cands = _candidates_360(query)
                elif engine == "sogou":
                    cands = _candidates_sogou(query)
                else:
                    cands = _candidates_bing(query)
            except Exception:
                continue
            fetched = 0
            for _, title, url in cands:
                if len(hits) >= 2 or fetched >= limit:
                    break
                if not url.startswith("http") or url in seen:
                    continue
                seen.add(url)
                fetched += 1
                text = _fetch_page_text(engine, url, page_cache)
                if not text:
                    continue
                n_raw = norm(text)
                m = match_parts(nkey, sentence, n_raw)
                if m:
                    pn, ptext = m
                    hits.append({
                        "source": "web", "source_label": "网页(%s,已逐字核验)" % engine,
                        "title": title or url[:80],
                        "url": url,
                        "meta": "网页正文含相同文字",
                        "matched_len": len(pn),
                        "snippet": find_window(text, n_raw, pn)[:200],
                    })
        return hits

    if re.search(r"[\u4e00-\u9fff]", sentence):
        hits = sweep([("360", sentence), ("sogou", sentence)], fetch_limit)
        if not hits and len(clean) >= 20:
            mid = clean[len(clean) // 3: len(clean) // 3 + 14]
            hits = sweep([("360", mid)], fetch_limit)
        return hits
    return sweep([("bing", sentence.strip()[:80])], fetch_limit)


# ---------------------------------------------------------------- 主流程

def run_online_check(paper, sources=("openalex", "europepmc", "web"),
                     workers: int = 4, progress=None) -> dict:
    """切句并逐句检索。progress(done, total, hits_count) 由调用方提供。"""
    sents = split_sentences(paper)
    occurrences: dict[str, list[dict]] = {}
    order: list[str] = []
    for s in sents:
        occurrences.setdefault(s["nkey"], []).append(s)
        if s["nkey"] not in order:
            order.append(s["nkey"])
    warnings: list[str] = []
    if len(sents) >= MAX_SENTS:
        warnings.append("论文较长,本次仅检索前 %d 句(可分段检测以获得完整覆盖)。" % MAX_SENTS)
    total_chars = sum(len(s["text"]) for s in sents)
    page_cache: dict[str, str] = {}

    stats_lock = threading.Lock()

    def work(item):
        nkey, first = item
        hits: list[dict] = []
        errs = []
        if "openalex" in sources:
            with stats_lock:
                src_stats["openalex"][0] += 1
            try:
                hits += check_openalex(nkey, first["text"])
            except Exception as e:
                with stats_lock:
                    src_stats["openalex"][1] += 1
                errs.append("openalex: %s" % str(e)[:60])
        if "europepmc" in sources:
            with stats_lock:
                src_stats["europepmc"][0] += 1
            try:
                hits += check_europepmc(nkey, first["text"])
            except Exception as e:
                with stats_lock:
                    src_stats["europepmc"][1] += 1
                errs.append("europepmc: %s" % str(e)[:60])
        if "web" in sources and not _web_disabled.is_set():
            with stats_lock:
                src_stats["web"][0] += 1
            try:
                hits += check_web(nkey, first["text"], page_cache)
            except Exception as e:
                with stats_lock:
                    src_stats["web"][1] += 1
                errs.append("web: %s" % str(e)[:60])
        return nkey, hits, errs

    matched_chars = 0
    matched_sents: list[dict] = []
    hit_details: list[dict] = []
    source_counts: dict[str, int] = {}
    src_stats = {"openalex": [0, 0], "europepmc": [0, 0], "web": [0, 0]}  # [尝试数, 失败数]
    bing_miss = 0
    done = 0
    total = len(order)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, (k, occurrences[k][0])) for k in order]
        for fut in as_completed(futures):
            nkey, hits, errs = fut.result()
            done += 1
            for e in errs:
                src = e.split(":")[0]
                if src in src_stats:
                    src_stats[src][1] += 1
                if src == "web":
                    bing_miss += 1
            if bing_miss >= 8 and not _web_disabled.is_set():
                _web_disabled.set()
                warnings.append("网页检索连续失败,已自动停用网页来源(可能被限流或断网),结果仅含学术库。")
            if hits:
                for h in hits:
                    source_counts[h["source"]] = source_counts.get(h["source"], 0) + 1
                best = max(h.get("matched_len", 0) for h in hits)
                first = occurrences[nkey][0]
                for occ in occurrences[nkey]:
                    matched_sents.append({
                        "text": occ["text"], "para": occ["para"] + 1,
                        "start": occ["start"], "end": occ["end"],
                    })
                    matched_chars += best
                hit_details.append({"text": first["text"], "para": first["para"] + 1,
                                    "matched_len": best, "sources": hits})
            if progress:
                progress(done, total, hit_details[-1] if hits else None)

    labels = {"openalex": "OpenAlex 学术库", "europepmc": "Europe PMC", "web": "网页检索"}
    for src, (att, fail) in src_stats.items():
        if src in sources and att >= 3 and fail == att:
            warnings.append("%s 本次检索的全部请求都失败了(可能被限流或断网),该来源的结果不完整,建议稍后重试。" % labels[src])

    matched_sents.sort(key=lambda s: (s["para"], s["start"]))
    ratio = (matched_chars / total_chars) if total_chars else 0.0
    return {
        "sentences_total": len(sents),
        "sentences_unique": len(order),
        "sentences_matched": len(matched_sents),
        "matched_chars": matched_chars,
        "total_chars": total_chars,
        "ratio": round(ratio, 4),
        "source_counts": source_counts,
        "matched": matched_sents,
        "hit_details": hit_details,
        "warnings": warnings,
    }
