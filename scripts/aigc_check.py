# -*- coding: utf-8 -*-
"""AIGC 疑似度分析(启发式统计,离线、秒级)。

原理:AI 生成的中英文文本呈现一些可统计的风格特征——
  * 模板化短语密度高("随着…的快速发展""综上所述""值得注意的是"
    / "With the rapid development of" "it is important to note" 等)
  * 句长过于均匀(burstiness 低),句式起首重复
  * 连接词开头句占比高(然而/此外/总之/However/Moreover …)
  * 模糊限定词堆砌、顿号列举密集,缺少具体数字/日期/专有细节

输出 0~100 的"AIGC 疑似度"以及逐信号明细。

务必如实告知用户:这是启发式统计,不构成 AI 写作的证明;
人写的模板化论文(尤其八股学术腔)同样会得分,商用检测器也有误判。
"""

from __future__ import annotations

import re
import unicodedata

MIN_SENT_CHARS = 8

_NORM_RE = re.compile(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
# 中文句末标点后直接切;英文句点后必须有空白或行尾(避免切开 1.2 这类小数)
_SENT_SPLIT = re.compile(r"(?<=[。!?！?;;])\s*|(?<=[.!?])(?:\s+|$)")


def norm(s: str) -> str:
    return _NORM_RE.sub("", unicodedata.normalize("NFKC", s).casefold())


# ---------------------------------------------------------------- 特征库

# 模板化框架(正则, 权重, 标签)——命中权重越高越具指示性
AI_FRAMES = [
    (r"随着[^。]{2,14}的(?:快速|不断|迅速|日益)?(?:发展|进步|普及|提升|增长)", 1.0, "随着…的快速发展"),
    (r"(值得注意的是|需要指出的是|值得一提的是)", 1.0, "值得注意的是"),
    (r"(综上所述|总而言之|总的来说|总体而言|由此可见|不难发现)", 1.0, "综上所述类总结"),
    (r"首先[^。]{2,40}其次[^。]{2,40}(再次|最后|此外)", 1.2, "首先…其次…最后"),
    (r"一方面[^。]{2,40}另一方面", 1.0, "一方面…另一方面"),
    (r"为[^。]{2,10}提供了[^。]{2,10}(?:支持|保障|基础|参考|依据|思路)", 0.8, "为…提供了…"),
    (r"具有[^。]{2,8}(?:重要|深远|重大|积极)的?(?:意义|价值|影响|作用)", 0.8, "具有重要…意义"),
    (r"(?:取得|取得|获得|带来)[^。]{2,8}显著(?:提升|成效|效果|进展)", 0.8, "取得显著提升"),
    (r"(众所周知|毋庸置疑|不言而喻)", 0.8, "众所周知类"),
    (r"(?:日益|越来越)[^。,，]{0,8}(?:增长|普及|重要|突出)", 0.5, "日益…"),
    (r"(?:全面|深入|进一步)(?:推进|提升|加强|完善|探讨|分析|研究)", 0.5, "全面/深入推进"),
    (r"(?:赋能|抓手|闭环|底层逻辑|顶层设计|多维度|全方位|多层次)", 0.7, "流行语(赋能/抓手/闭环…)"),
    (r"(?:换言之|换句话说|简而言之|具体而言)", 0.5, "换言之类"),
    (r"it is (?:important|worth|crucial) (?:to note|noting|to highlight)", 1.2, "it is important to note"),
    (r"with the rapid development of", 1.2, "with the rapid development of"),
    (r"in (?:recent years|today's|the modern era)", 0.6, "in recent years"),
    (r"plays? an? (?:crucial|vital|pivotal|important|significant) role", 1.0, "plays a crucial role"),
    (r"(?:moreover|furthermore|additionally|in conclusion|overall)", 0.5, "moreover 类连接词"),
    (r"(?:delve(?:s|d)? into|shed light on|pave the way for|a wide (?:range|array) of)", 0.9, "delve into 类"),
    (r"(?:leverage|utilize|harness|comprehensive|holistic|robust|seamless(?:ly)?|pivotal)", 0.4, "AI 高频词(leverage/robust…)"),
]

# 连接词开头的句子
CONNECTIVE_START = re.compile(
    r"^(然而|但|但是|此外|另外|同时|因此|所以|于是|首先|其次|再次|最后|总之|综上|同时,|由此|"
    r"具体来说|具体而言|一般而言|通常|目前|当前|近年来|在此基础上|与此同时|"
    r"however|moreover|furthermore|additionally|therefore|thus|overall|hence|in addition|firstly|secondly|finally)",
    re.I)

# 模糊限定词(逐字计数)
VAGUE_WORDS = [
    "一定程度上", "某种程度", "在一定条件下", "一些", "某些", "许多", "大量", "各种", "多种多样",
    "通常", "一般而言", "一般来说", "大致", "大体", "相对", "较为", "比较", "更加", "日益",
    "广泛", "深入", "各种各樣", "普遍", "往往", "几乎", "相当", "非常", "极其", "十分",
    "various", "numerous", "significant", "considerably", "relatively", "generally",
    "typically", "increasingly", "extremely", "highly", "widely", "deeply",
]

# 顿号/逗号列举模板:A、B、C(至少3项)
LIST_RE = re.compile(r"[^。,，、;;]{2,10}(?:[、,，][^。,，、;;]{1,10}){2,}")

NUM_RE = re.compile(r"[0-9０-９]|[0-9]{4}年|\d+(?:\.\d+)?%")
DATE_RE = re.compile(r"(?:19|20)\d{2}\s*年|\d+\s*月|\d+\s*日|(?:19|20)\d{2}")


def _split(paper) -> list[dict]:
    sents = []
    for pi, para in enumerate(paper.paras):
        idx = 0
        for piece in _SENT_SPLIT.split(para):
            if not piece:
                continue
            start = para.find(piece, idx)
            idx = start + len(piece)
            if len(norm(piece)) < MIN_SENT_CHARS:
                continue
            sents.append({"text": piece.strip(), "para": pi + 1,
                          "start": start, "end": start + len(piece)})
    return sents


# ---------------------------------------------------------------- 分析

def _sentence_score(sent: str):
    """单句 AIGC 疑似分(0~100)与命中的信号标签。"""
    flags, raw = [], 0.0
    for pat, w, label in AI_FRAMES:
        if re.search(pat, sent, re.I):
            flags.append(label)
            raw += w
    m = CONNECTIVE_START.match(sent.strip())
    if m:
        flags.append("连接词开头(%s)" % m.group(1))
        raw += 0.35
    n = len(sent)
    vague = sum(sent.count(w) for w in VAGUE_WORDS)
    if vague:
        flags.append("模糊词×%d" % vague)
        raw += min(0.6, vague * 0.12)
    if not NUM_RE.search(sent) and n > 30:
        flags.append("无具体数字/日期")
        raw += 0.12
    # 均匀的中长句更可疑(过短/过长本身不扣)
    ln = len(norm(sent))
    if 15 <= ln <= 50:
        raw += 0.08
    score = min(100.0, raw / 2.2 * 100)
    return score, flags


def analyze_aigc(paper) -> dict:
    sents = _split(paper)
    if not sents:
        return {"score": None, "error": "论文几乎没有可分析的文本。"}

    for s in sents:
        s["score"], s["flags"] = _sentence_score(s["text"])

    # ---- 文档级特征 ----
    total_chars = sum(len(s["text"]) for s in sents)
    lens = [len(norm(s["text"])) for s in sents]
    mean_len = sum(lens) / len(lens)
    var = sum((x - mean_len) ** 2 for x in lens) / len(lens)
    cv = (var ** 0.5 / mean_len) if mean_len else 0.0
    burst_score = max(0.0, min(1.0, (0.55 - cv) / 0.35))   # cv≈0.2 → 1.0;cv≥0.55 → 0

    text_all = "\n".join(paper.paras)
    kchars = max(1.0, len(text_all) / 1000)

    frame_hits = []
    for pat, w, label in AI_FRAMES:
        for m in re.finditer(pat, text_all, re.I):
            frame_hits.append({"label": label, "text": m.group(0)[:30],
                               "para": None})
    # 定位模板命中的段落
    if frame_hits:
        pi = 0
        for fh in frame_hits:
            while pi < len(paper.paras) and fh["text"][:12] not in paper.paras[pi]:
                pi += 1
            if pi < len(paper.paras):
                fh["para"] = pi + 1
    frame_rate = len(frame_hits) / kchars
    frame_score = min(1.0, frame_rate / 6.0)

    conn_ratio = (sum(1 for s in sents if any(f.startswith("连接词开头") for f in s["flags"]))
                  / len(sents))
    conn_score = min(1.0, conn_ratio / 0.45)

    vague_count = sum(text_all.count(w) for w in VAGUE_WORDS)
    vague_score = min(1.0, vague_count / kchars / 12.0)

    list_count = len(LIST_RE.findall(text_all))
    list_score = min(1.0, list_count / kchars / 5.0)

    # ---- 综合分 ----
    scores = sorted((s["score"] for s in sents), reverse=True)
    top_n = max(1, len(scores) * 3 // 10)
    top_mean = sum(scores[:top_n]) / top_n
    doc = (0.40 * top_mean + 0.22 * frame_score * 100 + 0.14 * burst_score * 100 +
           0.10 * conn_score * 100 + 0.09 * vague_score * 100 + 0.05 * list_score * 100)
    doc = round(max(0.0, min(100.0, doc)), 1)

    def band(v):
        return "高疑似" if v >= 70 else ("中等疑似" if v >= 45 else "低疑似")

    signals = [
        {"name": "句长均匀度(过均匀更可疑)", "value": "变异系数 %.2f" % cv,
         "score": round(burst_score * 100)},
        {"name": "模板短语密度", "value": "%d 处 / 千字" % round(frame_rate, 1),
         "score": round(frame_score * 100)},
        {"name": "连接词开头句占比", "value": "%.0f%%" % (conn_ratio * 100),
         "score": round(conn_score * 100)},
        {"name": "模糊限定词密度", "value": "%d 个 / 千字" % round(vague_count / kchars, 1),
         "score": round(vague_score * 100)},
        {"name": "顿号/逗号列举密度", "value": "%d 处 / 千字" % round(list_count / kchars, 1),
         "score": round(list_score * 100)},
    ]

    # ---- 逐句高亮段落 ----
    spans = []
    for s in sents:
        if s["score"] >= 50:
            spans.append({"para": s["para"], "start": s["start"], "end": s["end"],
                          "level": "high" if s["score"] >= 75 else "mid"})
    paragraphs = _render(paper.paras, spans)

    return {
        "score": doc,
        "band": band(doc),
        "signals": signals,
        "frames": frame_hits,
        "sentences": sorted(sents, key=lambda s: -s["score"])[:30],
        "paragraphs": paragraphs,
        "sentences_total": len(sents),
        "paper": {"name": paper.name, "tokens": len(paper.tokens),
                  "chars": paper.char_count, "paras": len(paper.paras)},
    }


def _render(paras: list[str], spans: list[dict]) -> list[str]:
    """按句子疑似度渲染全文:橙=高疑似,黄=中等疑似。"""
    per: dict[int, list[dict]] = {}
    for s in spans:
        per.setdefault(s["para"] - 1, []).append(s)
    out = []
    for pi, para in enumerate(paras):
        parts, pos = [], 0
        for sp in sorted(per.get(pi, []), key=lambda x: x["start"]):
            st, en = sp["start"], sp["end"]
            if st > pos:
                parts.append(_esc(para[pos:st]))
            cls = "aigc-high" if sp["level"] == "high" else "aigc-mid"
            parts.append('<span class="%s">%s</span>' % (cls, _esc(para[st:en])))
            pos = max(pos, en)
        parts.append(_esc(para[pos:]))
        out.append('<p class="doc" id="p%d">【第 %d 段】%s</p>' % (pi + 1, pi + 1, "".join(parts)))
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_aigc_report(paper, res: dict, now: str) -> str:
    from check_plagiarism import HTML_HEAD
    style = ('<style>span.aigc-high{background:#ffb3a7;border-radius:2px}'
             'span.aigc-mid{background:#ffe082;border-radius:2px}'
             '.run{border-left:3px solid #ffb3a7;padding:8px 14px;margin:12px 0;background:#fff8f5;border-radius:0 8px 8px 0}'
             '.run .meta{color:#777;font-size:.9em}</style>')
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%d</td></tr>" % (s["name"], s["value"], s["score"])
                   for s in res["signals"])
    frames = "".join("<div class='run'><div class='meta'>第 %s 段 · %s</div>%s</div>"
                     % (f["para"] if f["para"] else "?", f["label"], _esc(f["text"]))
                     for f in res["frames"])
    parts = [HTML_HEAD.replace("</style>", style + "</style>")]
    parts.append("<h1>AIGC 疑似度分析报告(启发式)</h1>")
    parts.append(f'<p class="muted">生成时间:{now} | paper-plagiarism-check · 离线统计分析</p>')
    parts.append(f'<div class="verdict">论文 <b>{_esc(paper.name)}</b><br>'
                 f'AIGC 疑似度:<b>{res["score"]}</b> / 100 —— {res["band"]}</div>')
    parts.append("<h2>各信号明细</h2><table><tr><th>信号</th><th>取值</th><th>贡献(0-100)</th></tr>"
                 + rows + "</table>")
    parts.append("<h2>模板短语命中</h2>" + (frames or '<p class="muted">未命中模板短语。</p>'))
    parts.append("<h2>全文标注(橙=高疑似句,黄=中等疑似句)</h2>")
    parts.extend(res["paragraphs"])
    parts.append('<h2>务必阅读</h2><p class="muted">本分析是启发式统计(句长均匀度、模板短语、'
                 "连接词占比、模糊词密度等),不构成 AI 写作的证明:人写的模板化文章也会得分,"
                 "AI 写的朴素句子也可能低分;商用 AIGC 检测系统同样存在误判。分数请仅作为"
                 "写作风格自查参考。</p>")
    parts.append("</body></html>\n")
    return "".join(parts)
