#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""论文查重:将一篇论文与若干参考文献做本地词元重叠比对。

命令行用法:
    python check_plagiarism.py --paper 论文.docx --refs 文献目录/ a.pdf b.txt \
        --out 查重报告.md --html 查重报告.html --json 查重结果.json

网页版见同目录 webapp.py(拖拽上传、浏览器内查看高亮结果)。

原理:中文等 CJK 字符逐字、英文/数字按词切成"词元"序列,归一化(忽略大小写、
全角/半角、空白与标点)后,为每篇参考文献建立 k 个连续词元的索引;扫描论文,
凡与某篇参考文献存在 k 个连续相同词元的文字都记为重复,并合并为最大重复片段。
这是常见"连续 13 字相同即判重"思路的本地实现,只针对你提供的参考文献,
不能替代知网 / Turnitin / 维普等商业查重系统。

依赖:.docx / .txt / .md 开箱即用;.pdf 需要 `pip install pypdf`(或 pdfplumber)。
本文件既可独立运行,也作为模块被 webapp.py 导入(doc_from_bytes / analyze 等)。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import io
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:  # Windows 控制台默认 GBK,直接 print 中文可能报错
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".docx", ".pdf"}

_CJK = ("[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
        "\uf900-\ufaff\uac00-\ud7af]")
_WORD = "[0-9A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff_]+"
TOKEN_RE = re.compile(_CJK + "|" + _WORD)
CJK_RE = re.compile(_CJK)


# ---------------------------------------------------------------- 文本抽取

def read_plaintext(data: bytes) -> list[str]:
    """从原始字节解码纯文本并按行切段,自动识别 UTF-8 / GBK / Big5 / UTF-16。"""
    if data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", errors="replace")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        text = None
        for enc in ("utf-8", "gb18030"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = data.decode("latin-1", errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def read_docx(stream, name: str) -> list[str]:
    try:
        with zipfile.ZipFile(stream) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except KeyError as e:
        raise ValueError(
            f"{name} 不是有效的 .docx(可能只是把 .doc 改了扩展名),缺少部件 {e}")
    paras = []
    for p in root.iter(W + "p"):
        buf = []
        for node in p.iter():
            if node.tag == W + "t":
                buf.append(node.text or "")
            elif node.tag in (W + "tab", W + "br", W + "cr"):
                buf.append(" ")
        text = "".join(buf).strip()
        if text:
            paras.append(text)
    return paras


def read_pdf(stream, name: str) -> list[str]:
    text = None
    try:
        from pypdf import PdfReader
        reader = PdfReader(stream)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except ImportError:
        pass
    if text is None:
        try:
            import pdfplumber
        except ImportError:
            raise ValueError(
                "读取 PDF 需要 pypdf 或 pdfplumber(可先 `pip install pypdf`),"
                f"或将 {name} 另存为 .docx / .txt。")
        with pdfplumber.open(stream) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


@dataclass
class Doc:
    name: str
    paras: list[str]
    tokens: list[str]
    meta: list[tuple[int, int, int]]  # 每个词元 -> (段落号, 段内起始字符, 段内结束字符)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paras)


def _doc_from_paras(name: str, paras: list[str]) -> Doc:
    tokens: list[str] = []
    meta: list[tuple[int, int, int]] = []
    for pi, para in enumerate(paras):
        for m in TOKEN_RE.finditer(para):
            tok = unicodedata.normalize("NFKC", m.group(0)).casefold()
            if tok:
                tokens.append(tok)
                meta.append((pi, m.start(), m.end()))
    return Doc(name, paras, tokens, meta)


def doc_from_bytes(name: str, data: bytes, suffix: str) -> Doc:
    """从内存字节构建文档(供网页上传使用,不落盘)。"""
    suffix = suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        paras = read_plaintext(data)
    elif suffix == ".docx":
        paras = read_docx(io.BytesIO(data), name)
    elif suffix == ".pdf":
        paras = read_pdf(io.BytesIO(data), name)
    else:
        raise ValueError(
            f"不支持的文件类型:{name}(支持 {', '.join(sorted(SUPPORTED_EXTS))})")
    return _doc_from_paras(name, paras)


def load_doc(path: Path) -> Doc:
    return doc_from_bytes(path.name, path.read_bytes(), path.suffix.lower())


# ---------------------------------------------------------------- 匹配

def build_index(tokens: list[str], k: int) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i in range(len(tokens) - k + 1):
        idx.setdefault("\x00".join(tokens[i:i + k]), []).append(i)
    return idx


def cover(paper_grams: list[str], index: dict[str, list[int]], k: int, n: int):
    """返回 (每个论文词元是否被覆盖, 每个被覆盖词元对应的参考词元位置)。"""
    covered = bytearray(n)
    ref_pos = [-1] * n
    for i, g in enumerate(paper_grams):
        positions = index.get(g)
        if positions:
            r0 = positions[0]
            for j in range(i, i + k):
                if not covered[j]:
                    covered[j] = 1
                    ref_pos[j] = r0 + (j - i)
    return covered, ref_pos


def find_runs(covered: bytearray, min_run: int) -> list[tuple[int, int]]:
    """连续覆盖区间合并为最大片段(闭区间),长度不足 min_run 的丢弃。"""
    runs = []
    start = None
    for i, c in enumerate(covered):
        if c and start is None:
            start = i
        elif not c and start is not None:
            if i - start >= min_run:
                runs.append((start, i - 1))
            start = None
    if start is not None and len(covered) - start >= min_run:
        runs.append((start, len(covered) - 1))
    return runs


def auto_k(tokens: list[str]) -> int:
    if not tokens:
        return 13
    cjk = sum(1 for t in tokens if CJK_RE.fullmatch(t))
    return 13 if cjk / len(tokens) >= 0.30 else 6


def band(ratio: float) -> str:
    if ratio >= 0.30:
        return "高 🚨"
    if ratio >= 0.10:
        return "中等 ⚠️"
    return "低 ✅"


# ---------------------------------------------------------------- 摘录

def excerpt_md(doc: Doc, a: int, b: int, ctx: int = 6, bold: bool = True) -> str:
    """词元闭区间 [a, b] 对应的原文摘录;匹配部分以 **加粗** 标记,前后带少量上下文。"""
    n = len(doc.meta)
    if n == 0:
        return ""
    a = max(0, min(a, n - 1))
    b = max(a, min(b, n - 1))
    pieces: list[str] = []
    i = a
    while i <= b:
        pi = doc.meta[i][0]
        j = i
        while j < b and doc.meta[j + 1][0] == pi:
            j += 1
        lo, hi = i, j
        t = 0
        while lo - 1 >= 0 and t < ctx and doc.meta[lo - 1][0] == pi:
            lo -= 1
            t += 1
        t = 0
        while hi + 1 < n and t < ctx and doc.meta[hi + 1][0] == pi:
            hi += 1
            t += 1
        s, e = doc.meta[i][1], doc.meta[j][2]
        cs, ce = doc.meta[lo][1], doc.meta[hi][2]
        text = doc.paras[pi]
        mid = text[s:e]
        if len(mid) > 220:
            mid = mid[:110] + f"……(中段略,共 {len(mid)} 字)……" + mid[-70:]
        body = text[cs:s] + (f"**{mid}**" if bold else mid) + text[e:ce]
        if cs > 0:
            body = "……" + body
        if ce < len(text):
            body = body + "……"
        pieces.append(body)
        i = j + 1
    return " ⏎ ".join(pieces)


# ---------------------------------------------------------------- 分析

def analyze(paper: Doc, refs: list[Doc], k: int = 0, min_run: int = 0,
            ctx: int = 6) -> dict:
    """核心检测:计算总重复率、各来源重合度与全部重复片段。

    返回 dict:k / min_run / per_ref(含每个词元覆盖情况)/ union /
    union_count / overall / runs_info / counts。
    """
    n = len(paper.tokens)
    if k <= 0:
        corpus_sample = list(paper.tokens)
        for d in refs:
            corpus_sample.extend(d.tokens[:2000])
        k = auto_k(corpus_sample)
    if min_run <= 0:
        min_run = k

    if n < k:
        print(f"⚠️ 论文只有 {n} 个词元,小于判定窗口 k={k};本次不可能检出重复。"
              "可用 --k 调小窗口。")

    paper_grams = ["\x00".join(paper.tokens[i:i + k]) for i in range(n - k + 1)]
    per_ref = []
    for d in refs:
        index = build_index(d.tokens, k)
        cov, rpos = cover(paper_grams, index, k, n)
        per_ref.append({"doc": d, "cov": cov, "rpos": rpos,
                        "ratio": (sum(cov) / n if n else 0.0)})

    union = bytearray(n)
    for pr in per_ref:
        cov = pr["cov"]
        for i in range(n):
            if cov[i]:
                union[i] = 1
    union_count = sum(union)
    overall = union_count / n if n else 0.0

    runs = find_runs(union, min_run)
    runs_info = []
    for a, b in runs:
        length = b - a + 1
        scored_all = sorted(
            ((sum(pr["cov"][a:b + 1]), i) for i, pr in enumerate(per_ref)),
            reverse=True)
        scored = [(o, i) for o, i in scored_all if o >= 0.4 * length][:3] or scored_all[:1]
        sources = [per_ref[i]["doc"].name for _, i in scored]
        source_idxs = [i for _, i in scored]
        paras = sorted({paper.meta[j][0] for j in range(a, b + 1)})
        paper_ex = excerpt_md(paper, a, b, ctx=ctx)
        primary = scored[0][1]
        j0 = next((j for j in range(a, b + 1) if per_ref[primary]["cov"][j]), a)
        rstart = per_ref[primary]["rpos"][j0]
        ref_doc = per_ref[primary]["doc"]
        if 0 <= rstart < len(ref_doc.tokens):
            rend = min(rstart + length - 1, len(ref_doc.tokens) - 1)
            ref_ex = excerpt_md(ref_doc, rstart, rend, ctx=ctx, bold=False)
        else:
            ref_ex = ""
        runs_info.append({
            "a": a, "b": b, "tokens": length,
            "paras": [p + 1 for p in paras],
            "sources": sources, "source_idxs": source_idxs,
            "paper_excerpt_md": paper_ex,
            "paper_excerpt": paper_ex.replace("**", ""),
            "ref_excerpt": ref_ex.replace("**", ""),
        })
    runs_info.sort(key=lambda r: -r["tokens"])

    counts = [0] * len(per_ref)
    for r in runs_info:
        for i in r["source_idxs"]:
            counts[i] += 1

    return {"k": k, "min_run": min_run, "per_ref": per_ref, "union": union,
            "union_count": union_count, "overall": overall,
            "runs_info": runs_info, "counts": counts}


# ---------------------------------------------------------------- 报告

LIMIT_NOTE = (
    "- 本次仅比对了上述所提供的参考文献;未包含知网 / Turnitin 等数据库,也未联网检索。\n"
    "- 判定为重复要求连续 {k} 个词元完全一致(归一化后);同义改写、语序调整、中英互译后的雷同**不会**被检出。\n"
    "- 段落编号按抽取到的正文段落计数(表格 / 脚注 / 页眉可能不计入)。\n"
    "- 低(<10%)/ 中(10–30%)/ 高(>30%)仅为本地工具的经验档位,不代表任何商业查重系统的结论。\n"
    "- 请对每处片段人工确认:文献综述中的专有名词串、通用方法学表述可能出现误报。"
)


def md_report(paper, per_ref, k, min_run, overall, union_count, runs_info,
              counts, now, max_runs) -> str:
    n = len(paper.tokens)
    total_ref_tokens = sum(len(pr["doc"].tokens) for pr in per_ref)
    L: list[str] = []
    L.append("# 论文查重报告(本地比对)")
    L.append("")
    L.append(f"> 生成时间:{now} | 工具:paper-plagiarism-check(本地词元比对,文件不出本机)")
    L.append("")
    L.append(f"**待检论文**:`{paper.name}`({n:,} 词元 / {paper.char_count:,} 字符 / {len(paper.paras)} 段)")
    L.append(f"**参考文献**:{len(per_ref)} 篇,共 {total_ref_tokens:,} 词元")
    L.append(f"**判定规则**:连续 **{k}** 个词元完全相同(中文≈{k} 字,英文≈{k} 词;"
             "归一化后忽略大小写、全半角、空白与标点)")
    L.append("")
    L.append("## 总体结论")
    L.append("")
    L.append(f"**总重复率:{overall:.1%} —— {band(overall)}**")
    L.append("")
    L.append(f"论文 {n:,} 个词元中有 {union_count:,} 个出现在至少一篇参考文献中(各来源取并集)。")
    L.append("")
    L.append("> 本报告只反映论文与**上述所提供参考文献**的文字重合度,"
             "与知网 / Turnitin 等系统的检测结果没有对应关系。")
    L.append("")
    L.append("| # | 参考文献 | 词元数 | 与论文重合 | 匹配片段 |")
    L.append("|---|---------|--------|-----------|----------|")
    for i, pr in enumerate(per_ref, 1):
        name = pr["doc"].name.replace("|", "/")
        L.append(f"| {i} | `{name}` | {len(pr['doc'].tokens):,} "
                 f"| {pr['ratio']:.1%} | {counts[i - 1]} 处 |")
    L.append("")
    L.append(f"## 重复片段明细(共 {len(runs_info)} 处,按长度降序)")
    if not runs_info:
        L.append("")
        L.append("未发现达到判定窗口的重复内容 🎉")
    shown = runs_info[:max_runs]
    for i, r in enumerate(shown, 1):
        L.append("")
        L.append(f"### {i}. 长度 {r['tokens']} 词元 · 论文第 "
                 f"{', '.join(map(str, r['paras']))} 段 · 来源:{'、'.join(r['sources'])}")
        L.append("")
        L.append(f"- **论文**:{r['paper_excerpt_md']}")
        if r["ref_excerpt"]:
            L.append(f"- **参考**:{r['ref_excerpt']}")
    if len(runs_info) > len(shown):
        L.append("")
        L.append(f"> 其余 {len(runs_info) - len(shown)} 处较短的片段未列出,完整数据见 JSON 输出。")
    L.append("")
    L.append("## 说明与局限")
    L.append("")
    L.append(LIMIT_NOTE.format(k=k))
    return "\n".join(L) + "\n"


HTML_HEAD = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>论文查重报告</title>
<style>
 body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;max-width:920px;margin:24px auto;padding:0 16px;color:#222;line-height:1.75}
 h1{font-size:1.5em} h2{font-size:1.15em;border-bottom:1px solid #eee;padding-bottom:4px;margin-top:28px}
 mark{background:#ffd54f;padding:0 2px;border-radius:2px}
 table{border-collapse:collapse;margin:8px 0} th,td{border:1px solid #ddd;padding:4px 12px}
 .muted{color:#777;font-size:.9em}
 .verdict{font-size:1.1em;background:#f6f8fa;border-left:4px solid #d97706;padding:10px 14px;border-radius:4px}
 p.doc{margin:10px 0}
</style></head><body>
"""


def merge_para_spans(paper: Doc, union) -> dict[int, list[tuple[int, int]]]:
    """把逐词元覆盖标记合并为每段的字符高亮区间;间隔 ≤3 字符(标点)的区间并为一体。"""
    spans: dict[int, list[tuple[int, int]]] = {}
    cur = None  # (pi, s, e)
    for j in range(len(paper.tokens)):
        if not union[j]:
            continue
        pi, s, e = paper.meta[j]
        if cur and pi == cur[0] and s <= cur[2]:
            cur = (pi, cur[1], max(cur[2], e))
        else:
            if cur:
                spans.setdefault(cur[0], []).append((cur[1], cur[2]))
            cur = (pi, s, e)
    if cur:
        spans.setdefault(cur[0], []).append((cur[1], cur[2]))
    for pi, spans_pi in spans.items():
        merged: list[list[int]] = []
        for s, e in spans_pi:
            if merged and s - merged[-1][1] <= 3:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        spans[pi] = [(s, e) for s, e in merged]
    return spans


def render_paras_html(paper: Doc, spans) -> list[str]:
    """渲染论文全文为 HTML 段落列表,重复部分包在 <mark> 中(内容已转义)。"""
    out_paras = []
    for pi, para in enumerate(paper.paras):
        parts, pos = [], 0
        for s, e in spans.get(pi, []):
            if s > pos:
                parts.append(_html.escape(para[pos:s]))
            parts.append(f"<mark>{_html.escape(para[s:e])}</mark>")
            pos = max(pos, e)
        parts.append(_html.escape(para[pos:]))
        out_paras.append(
            f'<p class="doc" id="p{pi + 1}">【第 {pi + 1} 段】' + "".join(parts) + "</p>")
    return out_paras


def html_report(paper, per_ref, k, overall, union_count, union, counts, now) -> str:
    n = len(paper.tokens)
    total_ref_tokens = sum(len(pr["doc"].tokens) for pr in per_ref)
    rows = "".join(
        f"<tr><td>{i}</td><td>{_html.escape(pr['doc'].name)}</td>"
        f"<td>{len(pr['doc'].tokens):,}</td><td><b>{pr['ratio']:.1%}</b></td>"
        f"<td>{counts[i - 1]} 处</td></tr>"
        for i, pr in enumerate(per_ref, 1))
    body_paras = render_paras_html(paper, merge_para_spans(paper, union))

    parts = [HTML_HEAD]
    parts.append("<h1>论文查重报告</h1>")
    parts.append(f'<p class="muted">生成时间:{now} | paper-plagiarism-check(本地词元比对,文件不出本机)</p>')
    parts.append(f'<div class="verdict">论文 <b>{_html.escape(paper.name)}</b>({n:,} 词元)· '
                 f'参考文献 {len(per_ref)} 篇(共 {total_ref_tokens:,} 词元)· '
                 f'判定窗口:连续 {k} 词元相同<br>'
                 f'总重复率:<b>{overall:.1%}</b> —— {band(overall)}</div>')
    parts.append("<h2>各来源重合度</h2><table><tr><th>#</th><th>参考文献</th>"
                 "<th>词元数</th><th>与论文重合</th><th>匹配片段</th></tr>" + rows + "</table>")
    parts.append(f"<h2>论文全文(黄色 = 与参考文献重复,共 {union_count:,} 词元)</h2>")
    parts.extend(body_paras)
    parts.append('<h2>说明</h2><p class="muted">本地预检:仅比对上述所提供的参考文献,'
                 "未包含知网 / Turnitin 等数据库,也未联网检索;同义改写、语序调整后的雷同不会检出。"
                 "重复率为本地工具启发式指标,请逐条人工确认。</p>")
    parts.append("</body></html>\n")
    return "".join(parts)


# ---------------------------------------------------------------- 命令行

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="check_plagiarism",
        description="论文查重(本地词元比对):将论文与你提供的参考文献比对,"
                    "输出重复率与逐处片段对照。",
        epilog="示例:python check_plagiarism.py --paper 论文.docx --refs 文献/ 旧稿.txt "
               "--out 报告.md --html 报告.html")
    ap.add_argument("--paper", required=True, help="待检论文文件(.docx/.pdf/.txt/.md)")
    ap.add_argument("--refs", required=True, nargs="+", help="参考文献:文件或目录,可写多个")
    ap.add_argument("--k", type=int, default=0,
                    help="判定窗口:连续多少个词元相同算重复(默认自动:中文 13 / 英文 6)")
    ap.add_argument("--min-run", type=int, default=0,
                    help="报告片段的最小词元长度(默认等于 k)")
    ap.add_argument("--ctx", type=int, default=6, help="片段摘录的上下文词元数(默认 6)")
    ap.add_argument("--out", help="Markdown 报告路径")
    ap.add_argument("--html", dest="html_out", help="HTML 高亮报告路径")
    ap.add_argument("--json", dest="json_out", help="JSON 结果路径")
    ap.add_argument("--max-runs", type=int, default=200,
                    help="明细最多列出多少处片段(默认 200)")
    return ap.parse_args(argv)


def collect_ref_paths(raw, paper_path: Path, exclude: set) -> list[Path]:
    found: list[Path] = []
    for item in raw:
        p = Path(item)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS \
                        and not f.name.startswith("~$"):
                    found.append(f)
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            found.append(p)
        else:
            print(f"⚠️ 跳过不存在或不支持的引用路径:{item}"
                  "(支持 .docx/.pdf/.txt/.md;.doc 请先在 Word 中另存为 .docx)")
    uniq: list[Path] = []
    seen: set = set()
    paper_key = paper_path.resolve()
    for f in found:
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key == paper_key or key in seen or key in exclude:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq


def main(argv=None) -> None:
    args = parse_args(argv)
    paper_path = Path(args.paper)
    if not paper_path.is_file():
        sys.exit(f"ERROR: 找不到论文文件:{paper_path}")

    exclude = {Path(x).resolve() for x in (args.out, args.html_out, args.json_out) if x}
    ref_paths = collect_ref_paths(args.refs, paper_path, exclude)
    if not ref_paths:
        sys.exit("ERROR: 没有可用的参考文献(支持 .docx/.pdf/.txt/.md)。"
                 "请提供要比对的文献文件或目录。")

    try:
        paper = load_doc(paper_path)
    except Exception as e:
        sys.exit(f"ERROR: 读取论文失败:{e}")
    if len(paper.tokens) < 5:
        sys.exit(f"ERROR: 论文几乎没有可检测的文本({len(paper.tokens)} 词元)。")

    refs = []
    for p in ref_paths:
        try:
            d = load_doc(p)
        except Exception as e:
            print(f"⚠️ 读取失败,已跳过 {p}:{e}")
            continue
        if len(d.tokens) < 8:
            print(f"⚠️ 参考文献 {p.name} 几乎没有文本({len(d.tokens)} 词元,可能是扫描件),已跳过。")
            continue
        if d.tokens == paper.tokens:
            print(f"⚠️ 参考文件 {p.name} 与论文内容完全相同,已跳过"
                  "(请确认不是把论文本身放进了参考文献)。")
            continue
        refs.append(d)
    if not refs:
        sys.exit("ERROR: 所有参考文献都无法使用,终止。")

    res = analyze(paper, refs, k=args.k, min_run=args.min_run, ctx=args.ctx)
    runs_info = res["runs_info"]
    counts = res["counts"]

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out_files = []

    def write_out(path_str: str, text: str) -> None:
        p = Path(path_str)
        if str(p.parent) and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out_files.append(str(p))

    if args.out:
        write_out(args.out, md_report(paper, res["per_ref"], res["k"], res["min_run"],
                                      res["overall"], res["union_count"], runs_info,
                                      counts, now, args.max_runs))
    if args.html_out:
        write_out(args.html_out, html_report(paper, res["per_ref"], res["k"],
                                             res["overall"], res["union_count"],
                                             res["union"], counts, now))
    if args.json_out:
        payload = {
            "tool": "paper-plagiarism-check v1.0",
            "generated_at": now,
            "k": res["k"], "min_run": res["min_run"],
            "paper": {"file": str(paper_path), "name": paper.name,
                      "tokens": len(paper.tokens), "chars": paper.char_count,
                      "paras": len(paper.paras)},
            "overall_ratio": round(res["overall"], 4),
            "overall_band": band(res["overall"]),
            "references": [
                {"name": pr["doc"].name, "tokens": len(pr["doc"].tokens),
                 "ratio": round(pr["ratio"], 4), "runs": counts[i]}
                for i, pr in enumerate(res["per_ref"])],
            "runs": [
                {"start_token": r["a"], "end_token": r["b"],
                 "tokens": r["tokens"], "paras": r["paras"],
                 "sources": r["sources"],
                 "paper_text": r["paper_excerpt"],
                 "reference_text": r["ref_excerpt"]}
                for r in runs_info],
        }
        write_out(args.json_out, json.dumps(payload, ensure_ascii=False, indent=2))

    print_summary(paper, refs, res["k"], res["overall"], res["per_ref"],
                  runs_info, out_files)


def print_summary(paper, refs, k, overall, per_ref, runs_info, out_files) -> None:
    print("=" * 46)
    print("论文查重(本地比对)结果")
    print("=" * 46)
    print(f"论文:{paper.name}({len(paper.tokens):,} 词元)")
    print(f"参考文献:{len(refs)} 篇 | 判定窗口:连续 {k} 词元相同")
    print()
    print(f"总重复率:{overall:.1%}({band(overall)})")
    for i, pr in enumerate(per_ref, 1):
        print(f"  {i}. {pr['doc'].name}  {pr['ratio']:.1%}")
    print()
    if runs_info:
        print(f"共发现 {len(runs_info)} 处重复片段,最长的几处:")
        for r in runs_info[:5]:
            head = r["paper_excerpt"][:60].replace("\n", " ")
            print(f"  [{r['tokens']} 词元|第{','.join(map(str, r['paras']))}段"
                  f"|来源:{r['sources'][0]}] {head}……")
    else:
        print("未发现达到判定窗口的重复内容。")
    if out_files:
        print()
        print("已生成:" + " / ".join(out_files))


if __name__ == "__main__":
    main()
