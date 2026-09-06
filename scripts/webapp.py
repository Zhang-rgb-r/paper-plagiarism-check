#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""论文查重网页版:在线自动检索 + 本地文献比对两种模式。

启动:
    python webapp.py [--port 8765] [--host 127.0.0.1] [--no-open]

仅依赖 Python 标准库(读取 PDF 仍需 pypdf)。全部在本机运行:在线模式的检索
请求只会把"句子片段"发给公开的检索数据源(OpenAlex / Europe PMC / 搜索引擎),
不会上传整篇文档到任何单一服务器。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import re
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_plagiarism import (  # noqa: E402
    SUPPORTED_EXTS,
    analyze,
    band,
    doc_from_bytes,
    html_report,
    merge_para_spans,
    render_paras_html,
)
import online_check  # noqa: E402
import aigc_check  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

MAX_BODY = 80 * 1024 * 1024
MAX_FILE = 60 * 1024 * 1024

ONLINE_NOTE = (
    "在线检索模式说明:自动把论文切句后,逐句到开放学术库(OpenAlex 约 2.5 亿篇的题录与摘要、"
    "Europe PMC 开放获取全文)和搜索引擎(360 / 搜狗 / Bing)检索原句出处,命中后抓取原文逐字核验,"
    "只把核验通过的句子计入重复率。判定标准是整句(或较长小句)逐字重复,同义改写不会命中;"
    "网页检索受搜索引擎索引与排序限制,为尽力检索,无法保证发现所有出处;知网等商业库的"
    "授权全文无法检索。结果为本地工具的参考指标,不能替代知网 / Turnitin 的全库比对。"
)


# ---------------------------------------------------------------- multipart 解析

_PARAM_RE = re.compile(r'([a-zA-Z0-9_\-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^";\s]+))')


def _parse_cd(value: str) -> dict:
    params = {}
    for m in _PARAM_RE.finditer(value):
        key = m.group(1).lower()
        if m.group(2) is not None:
            params[key] = m.group(2).replace('\\"', '"')
        else:
            params[key] = m.group(3)
    return params


def _decode_filename(params: dict) -> str:
    if "filename*" in params:
        raw = params["filename*"]
        charset, _, rest = raw.partition("''")
        try:
            return urllib.parse.unquote(rest, encoding=charset or "utf-8", errors="replace")
        except LookupError:
            return urllib.parse.unquote(rest, errors="replace")
    return params.get("filename", "")


def parse_multipart(body: bytes, boundary: str) -> list[dict]:
    delim = b"\r\n--" + boundary.encode("latin-1")
    fields = []
    for seg in (b"\r\n" + body).split(delim)[1:]:
        if seg.startswith(b"--"):
            break
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        head, sep, data = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            k, _, v = line.partition(":")
            if k.strip():
                headers[k.strip().lower()] = v.strip()
        cd = _parse_cd(headers.get("content-disposition", ""))
        fields.append({"name": cd.get("name", ""),
                       "filename": _decode_filename(cd),
                       "data": data})
    return fields


def _get_field(fields: list[dict], name: str) -> str:
    for f in fields:
        if f["name"] == name and not f["filename"]:
            return f["data"].decode("utf-8", errors="replace").strip()
    return ""


def _load_paper(fields: list[dict]):
    """从上传字段中取出并解析论文;返回 (paper, warnings, error)。"""
    uploads = [f for f in fields if f["filename"]]
    paper_items = [f for f in uploads if f["name"] == "paper"] or uploads[:1]
    if not paper_items:
        return None, [], "请先上传待检论文(.docx / .pdf / .txt / .md)。"
    warnings = []
    if len(paper_items) > 1:
        warnings.append("论文位置上传了多个文件,已只使用第一个。")
    item = paper_items[0]
    name = item["filename"]
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in SUPPORTED_EXTS:
        return None, warnings, f"{name}:不支持的格式(支持 .docx/.pdf/.txt/.md)"
    if len(item["data"]) > MAX_FILE:
        return None, warnings, f"{name}:文件超过 60MB 上限"
    try:
        paper = doc_from_bytes(name, item["data"], suffix)
    except Exception as e:
        return None, warnings, f"读取论文失败:{e}"
    if len(paper.tokens) < 5:
        return None, warnings, f"论文几乎没有可检测的文本({len(paper.tokens)} 词元)。"
    return paper, warnings, None


# ---------------------------------------------------------------- 本地比对接口

def api_check_local(body: bytes, content_type: str) -> tuple[int, dict]:
    m = re.search(r'boundary="?([^";,]+)"?', content_type)
    if not m:
        return 400, {"ok": False, "error": "请求格式错误(缺少 multipart boundary)。"}
    fields = parse_multipart(body, m.group(1))
    uploads = [f for f in fields if f["filename"]]
    ref_items = [f for f in uploads if f["name"] == "refs"]
    if not uploads:
        return 400, {"ok": False, "error": "没有收到任何文件。"}
    if not ref_items:
        return 400, {"ok": False, "error": "本地比对需要参考文献:请在 ② 中上传文献、往稿或语料。"
                                            "若想全自动检索,请切换到「在线检索查重」模式。"}
    paper, warnings, err = _load_paper(fields)
    if err:
        return 400, {"ok": False, "error": err}

    refs = []
    for item in ref_items:
        name = item["filename"]
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if suffix not in SUPPORTED_EXTS:
            warnings.append(f"参考文献 {name}:不支持的格式,已跳过")
            continue
        try:
            d = doc_from_bytes(name, item["data"], suffix)
        except Exception as e:
            warnings.append(f"参考文献读取失败,已跳过:{e}")
            continue
        if len(d.tokens) < 8:
            warnings.append(f"参考文献 {d.name} 几乎没有文本(可能是扫描件),已跳过。")
            continue
        if d.tokens == paper.tokens:
            warnings.append(f"参考文件 {d.name} 与论文内容完全相同,已跳过。")
            continue
        refs.append(d)
    if not refs:
        return 400, {"ok": False, "error": "所有参考文献都无法使用:" + (";".join(warnings) or "未知原因")}

    k_arg = int(_get_field(fields, "k") or 0)
    min_run_arg = int(_get_field(fields, "min_run") or 0)
    res = analyze(paper, refs, k=k_arg, min_run=min_run_arg)
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    runs_payload = []
    for r in res["runs_info"]:
        paper_html = re.sub(r"\*\*(.+?)\*\*", r"<mark>\1</mark>",
                            html.escape(r["paper_excerpt_md"])).replace(" ⏎ ", " ¶ ")
        ref_html = html.escape(r["ref_excerpt"]).replace(" ⏎ ", " ¶ ")
        runs_payload.append({
            "tokens": r["tokens"], "paras": r["paras"], "sources": r["sources"],
            "paper_html": paper_html, "ref_html": ref_html,
        })
    report_html = html_report(paper, res["per_ref"], res["k"], res["overall"],
                              res["union_count"], res["union"], res["counts"], now)
    payload = {
        "ok": True,
        "warnings": warnings,
        "k": res["k"],
        "overall": res["overall"],
        "band": band(res["overall"]),
        "paper": {"name": paper.name, "tokens": len(paper.tokens),
                  "chars": paper.char_count, "paras": len(paper.paras)},
        "refs": [{"name": pr["doc"].name, "tokens": len(pr["doc"].tokens),
                  "ratio": pr["ratio"], "runs": res["counts"][i]}
                 for i, pr in enumerate(res["per_ref"])],
        "runs": runs_payload,
        "paragraphs": render_paras_html(paper, merge_para_spans(paper, res["union"])),
        "report_html": report_html,
    }
    print(f"[local] {paper.name} × {len(refs)} 篇参考文献 -> 重复率 {res['overall']:.1%}")
    return 200, payload


# ---------------------------------------------------------------- 在线检索接口(任务制)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _render_online_paras(paper, matched_spans: list[dict]) -> list[str]:
    """按命中句子的字符区间渲染全文高亮。"""
    per_para: dict[int, list[tuple[int, int]]] = {}
    for s in matched_spans:
        per_para.setdefault(s["para"] - 1, []).append((s["start"], s["end"]))
    spans = {}
    for pi, lst in per_para.items():
        lst.sort()
        merged: list[list[int]] = []
        for st, en in lst:
            if merged and st - merged[-1][1] <= 3:
                merged[-1][1] = en
            else:
                merged.append([st, en])
        spans[pi] = [(a, b) for a, b in merged]
    return render_paras_html(paper, spans)


def build_online_report(paper, res: dict, now: str) -> str:
    from check_plagiarism import HTML_HEAD
    labels = {"openalex": "OpenAlex 学术库", "europepmc": "Europe PMC 开放全文", "web": "网页检索"}
    src_rows = "".join(
        f"<tr><td>{labels.get(k, k)}</td><td><b>{v}</b> 处</td></tr>"
        for k, v in res.get("source_counts", {}).items() if v)
    hits_html = []
    for h in res.get("hit_details", []):
        srcs = "".join(
            f'<div class="src">↳ <a href="{html.escape(s.get("url") or "", True)}" target="_blank" rel="noreferrer">'
            f'{html.escape(s.get("title") or "")}</a> <span class="muted">({html.escape(s.get("source_label", ""))}'
            f'{" / " + html.escape(s["meta"]) if s.get("meta") else ""})</span></div>'
            f'<div class="quote">{html.escape(s.get("snippet") or "")}</div>'
            for s in h.get("sources", []))
        hits_html.append(
            f'<div class="run"><div class="meta">第 {h["para"]} 段 · 命中 {h.get("matched_len", 0)} 字</div>'
            f'<div>{html.escape(h["text"])}</div>{srcs}</div>')
    parts = [HTML_HEAD]
    parts.append("<h1>论文在线检索查重报告</h1>")
    parts.append(f'<p class="muted">生成时间:{now} | paper-plagiarism-check(本地词元比对 + 开放数据源检索)</p>')
    parts.append(f'<div class="verdict">论文 <b>{html.escape(paper.name)}</b>({len(paper.tokens):,} 词元)<br>'
                 f'在线重复率:<b>{res["ratio"]:.1%}</b> —— 检索句子 {res["sentences_total"]} 句,'
                 f'命中 {res["sentences_matched"]} 句(重复文字 {res["matched_chars"]:,} 字)</div>')
    if src_rows:
        parts.append("<h2>各来源命中</h2><table>" + src_rows + "</table>")
    parts.append("<h2>命中片段明细</h2>" + ("".join(hits_html) or '<p class="muted">未发现逐字重复的句子。</p>'))
    parts.append("<h2>论文全文(黄色 = 检索命中的句子)</h2>")
    parts.extend(_render_online_paras(paper, res.get("matched", [])))
    parts.append(f'<h2>说明与局限</h2><p class="muted">{html.escape(ONLINE_NOTE)}</p>')
    parts.append("</body></html>\n")
    return "".join(parts)


def api_check_online_start(body: bytes, content_type: str) -> tuple[int, dict]:
    m = re.search(r'boundary="?([^";,]+)"?', content_type)
    if not m:
        return 400, {"ok": False, "error": "请求格式错误(缺少 multipart boundary)。"}
    fields = parse_multipart(body, m.group(1))
    paper, warnings, err = _load_paper(fields)
    if err:
        return 400, {"ok": False, "error": err}
    sources = [s.strip() for s in _get_field(fields, "sources").split(",") if s.strip()]
    sources = [s for s in sources if s in ("openalex", "europepmc", "web")] or \
              ["openalex", "europepmc", "web"]

    job_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "done": 0, "total": 0, "hits": [],
           "result": None, "error": None, "started": _dt.datetime.now()}
    with JOBS_LOCK:
        if len(JOBS) > 20:   # 只保留最近的任务
            for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-10]:
                JOBS.pop(old, None)
        JOBS[job_id] = job
    print(f"[online] 开始检索 {paper.name}(来源:{','.join(sources)})")

    def progress(done, total, hit):
        with JOBS_LOCK:
            job["done"], job["total"] = done, total
            if hit:
                job["hits"].append(hit)

    def run():
        try:
            res = online_check.run_online_check(paper, sources=tuple(sources),
                                                progress=progress)
            now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            res["paper"] = {"name": paper.name, "tokens": len(paper.tokens),
                            "chars": paper.char_count, "paras": len(paper.paras)}
            res["paragraphs"] = _render_online_paras(paper, res.get("matched", []))
            res["report_html"] = build_online_report(paper, res, now)
            with JOBS_LOCK:
                job["result"] = res
                job["status"] = "done"
            print(f"[online] {paper.name} 完成:在线重复率 {res['ratio']:.1%}")
        except Exception as e:
            with JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(e)[:300]
            print(f"[online] 失败:{e}")

    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True, "job_id": job_id, "warnings": warnings,
                 "paper": {"name": paper.name, "tokens": len(paper.tokens)}}


def api_job_status(job_id: str) -> tuple[int, dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return 404, {"ok": False, "error": "任务不存在或服务已重启,请重新提交。"}
        payload = {"ok": True, "status": job["status"],
                   "done": job["done"], "total": job["total"],
                   "hits_count": len(job["hits"]),
                   "recent_hits": job["hits"][-3:]}
        if job["status"] == "done":
            payload["result"] = job["result"]
        if job["status"] == "error":
            payload["error"] = job["error"]
    return 200, payload


# ---------------------------------------------------------------- AIGC 疑似分析接口

AIGC_NOTE = (
    "AIGC 疑似度是启发式统计(句长均匀度、模板短语密度、连接词开头句占比、模糊词密度、"
    "列举密度),不构成 AI 写作的证明:人写的模板化文章也会得分,AI 写的朴素句子也可能低分,"
    "商用 AIGC 检测系统同样存在误判。分数请仅作为写作风格自查参考。"
)


def api_check_aigc(body: bytes, content_type: str) -> tuple[int, dict]:
    m = re.search(r'boundary="?([^";,]+)"?', content_type)
    if not m:
        return 400, {"ok": False, "error": "请求格式错误(缺少 multipart boundary)。"}
    fields = parse_multipart(body, m.group(1))
    paper, warnings, err = _load_paper(fields)
    if err:
        return 400, {"ok": False, "error": err}
    res = aigc_check.analyze_aigc(paper)
    if res.get("error"):
        return 400, {"ok": False, "error": res["error"]}
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    res["report_html"] = aigc_check.build_aigc_report(paper, res, now)
    res["ok"] = True
    res["warnings"] = warnings
    print(f"[aigc] {paper.name} -> AIGC 疑似度 {res['score']}")
    return 200, res


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "PaperCheck/1.1"

    def _send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, text: str, code: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(PAGE)
            return
        m = re.match(r"^/api/job/([0-9a-f]+)$", path)
        if m:
            code, payload = api_job_status(m.group(1))
            self._send_json(code, payload)
            return
        self._send_html("<h1>404 Not Found</h1>", code=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/check", "/api/check_online", "/api/check_aigc"):
            self._send_json(404, {"ok": False, "error": "未知接口。"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(413, {"ok": False, "error": "请求体为空或超过 80MB 上限。"})
            return
        body = self.rfile.read(length)
        try:
            if path == "/api/check":
                code, payload = api_check_local(body, self.headers.get("Content-Type", ""))
            elif path == "/api/check_aigc":
                code, payload = api_check_aigc(body, self.headers.get("Content-Type", ""))
            else:
                code, payload = api_check_online_start(body, self.headers.get("Content-Type", ""))
        except Exception as e:
            code, payload = 500, {"ok": False, "error": f"服务器内部错误:{e}"}
        self._send_json(code, payload)

    def log_message(self, fmt, *args):
        pass


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>论文查重 · 本地检测</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--line:#e5e9f0;--ink:#1f2430;--sub:#6b7280;--accent:#2563eb;--ok:#16a34a;--mid:#d97706;--high:#dc2626}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;background:var(--bg);color:var(--ink);line-height:1.7}
.wrap{max-width:880px;margin:0 auto;padding:28px 16px 60px}
header h1{margin:0;font-size:1.6em}
header .sub{margin:4px 0 0;color:var(--sub)}
.tabs{display:flex;gap:10px;margin-top:20px}
.tab{flex:1;text-align:center;padding:10px;border:1px solid var(--line);background:var(--card);border-radius:10px;cursor:pointer;color:var(--sub);font-weight:600}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.pane{display:none}.pane.on{display:block}
.zone{background:var(--card);border:2px dashed #c9d2e0;border-radius:12px;padding:18px;text-align:center;cursor:pointer;transition:.15s;min-height:140px;margin-top:14px}
.zone.drag{border-color:var(--accent);background:#eff6ff}
.zone h3{margin:0 0 4px;font-size:1.02em}
.zone .hint{color:var(--sub);font-size:.88em}
.chips{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:10px}
.chip{display:inline-flex;align-items:center;gap:6px;background:#eef2f8;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:.85em;max-width:100%}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip button{border:0;background:none;color:var(--sub);cursor:pointer;font-size:1em;padding:0}
.chip button:hover{color:var(--high)}
.srces{margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.srces label{display:inline-flex;align-items:center;gap:6px;margin:4px 18px 4px 0;cursor:pointer}
.opts{margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px}
.opts summary{cursor:pointer;color:var(--sub)}
.opts label{display:inline-flex;align-items:center;gap:8px;margin:8px 18px 4px 0}
.opts input{width:120px;padding:4px 8px;border:1px solid var(--line);border-radius:6px}
.actions{margin-top:16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.btn{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 26px;font-size:1.05em;cursor:pointer}
.btn:disabled{background:#a8b8d8;cursor:not-allowed}
#status{color:var(--sub)}
#status.err{color:var(--high)}
.progress{display:none;margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.bar{height:10px;background:#e8edf5;border-radius:999px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--accent);transition:width .4s}
.ptext{color:var(--sub);font-size:.9em;margin-top:6px}
.livehit{background:#fffdf2;border-left:3px solid #ffd54f;border-radius:0 8px 8px 0;padding:6px 12px;margin-top:8px;font-size:.9em}
.livehit .m{color:var(--sub);font-size:.85em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:18px}
.card h3{margin:0 0 10px;font-size:1.05em}
.verdict{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.verdict .pct{font-size:2.4em;font-weight:700;line-height:1.2}
.pct.ok{color:var(--ok)}.pct.mid{color:var(--mid)}.pct.high{color:var(--high)}
.badge{padding:2px 14px;border-radius:999px;color:#fff}
.badge.ok{background:var(--ok)}.badge.mid{background:var(--mid)}.badge.high{background:var(--high)}
.meta2{color:var(--sub);font-size:.9em;margin-top:6px}
table{border-collapse:collapse;width:100%;margin-top:6px}
th,td{border-bottom:1px solid var(--line);padding:6px 10px;text-align:left;font-size:.95em}
mark{background:#ffd54f;padding:0 2px;border-radius:3px}
.run{border-left:3px solid #ffd54f;padding:8px 14px;margin:12px 0;background:#fffdf2;border-radius:0 8px 8px 0}
.run .meta{color:var(--sub);font-size:.85em;margin-bottom:4px}
.run .lbl{font-weight:600;margin-right:6px}
.run .src{margin-top:6px;font-size:.92em;word-break:break-all}
.run .src a{color:var(--accent)}
.run .quote{background:#f6f8fa;border-radius:6px;padding:6px 10px;margin-top:4px;font-size:.88em;color:#374151}
.warn{background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:6px 12px;margin-top:8px;font-size:.9em}
.fulltext p{margin:10px 0}
span.aigc-high{background:#ffb3a7;padding:0 2px;border-radius:3px}
span.aigc-mid{background:#ffe082;padding:0 2px;border-radius:3px}
.dl button{background:#fff;border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:6px 16px;cursor:pointer}
footer{margin-top:30px;color:var(--sub);font-size:.85em;text-align:center}
.empty{color:var(--sub)}
</style>
</head>
<body>
<div class="wrap">
<header>
 <h1>📄 论文查重 <span style="font-size:.55em;color:var(--sub)">本地检测</span></h1>
 <p class="sub">拖入论文即可 · 全程在本机运行,不会上传整篇文档到任何外部服务器</p>
</header>

<div class="tabs">
 <div class="tab on" id="tabBtnOnline">🌐 在线检索查重(自动)</div>
 <div class="tab" id="tabBtnLocal">📁 与本地文献比对</div>
 <div class="tab" id="tabBtnAigc">🔍 AIGC 疑似分析</div>
</div>

<div class="pane on" id="tabOnline">
 <div class="zone" id="onlinePaperZone">
  <input type="file" id="onlinePaperInput" accept=".docx,.pdf,.txt,.md" hidden>
  <h3>① 拖入待检论文</h3>
  <div class="hint">不需要准备参考文献 —— 工具会自动切句并联网检索每句话的出处</div>
  <div class="chips" id="onlinePaperChips"></div>
 </div>
 <div class="srces">检索来源:
  <label><input type="checkbox" class="srcck" value="openalex" checked>OpenAlex 学术库(2.5 亿篇题录摘要)</label>
  <label><input type="checkbox" class="srcck" value="europepmc" checked>Europe PMC 开放全文</label>
  <label><input type="checkbox" class="srcck" value="web" checked>搜索引擎(360/搜狗/Bing,结果逐字核验)</label>
 </div>
 <div class="actions">
  <button class="btn" id="onlineGo" disabled>开始在线检索</button>
  <span id="status">请先拖入论文</span>
 </div>
 <div class="progress" id="prog">
  <div class="bar"><div id="progBar"></div></div>
  <div class="ptext" id="progText"></div>
  <div id="progHits"></div>
 </div>
 <div id="onlineWarnings"></div>
 <section id="onlineResult" hidden></section>
</div>

<div class="pane" id="tabLocal">
 <div class="zones" style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
  <div class="zone" id="paperZone" style="margin-top:0">
   <input type="file" id="paperInput" accept=".docx,.pdf,.txt,.md" hidden>
   <h3>① 待检论文</h3><div class="hint">拖入文件,或点击选择</div>
   <div class="chips" id="paperChips"></div>
  </div>
  <div class="zone" id="refsZone" style="margin-top:0">
   <input type="file" id="refsInput" multiple accept=".docx,.pdf,.txt,.md" hidden>
   <h3>② 参考文献(可多个)</h3><div class="hint">拖入文件或整个文件夹</div>
   <div class="chips" id="refsChips"></div>
  </div>
 </div>
 <details class="opts"><summary>高级选项</summary>
  <label>判定窗口 k<input type="number" id="kInput" min="2" max="60" placeholder="自动:中文 13 / 英文 6"></label>
  <label>最小片段长度<input type="number" id="minRunInput" min="2" max="200" placeholder="默认等于 k"></label>
 </details>
 <div class="actions">
  <button class="btn" id="goBtn" disabled>开始比对</button>
  <span id="statusLocal">请先选择论文与参考文献</span>
 </div>
 <div id="warnings"></div>
 <section id="result" hidden></section>
</div>

<div class="pane" id="tabAigc">
 <div class="zone" id="aigcPaperZone">
  <input type="file" id="aigcPaperInput" accept=".docx,.pdf,.txt,.md" hidden>
  <h3>① 拖入待分析的论文</h3>
  <div class="hint">离线统计分析,秒级完成 · 估计的是"AI 生成风格疑似度"</div>
  <div class="chips" id="aigcPaperChips"></div>
 </div>
 <div class="actions">
  <button class="btn" id="aigcGo" disabled>开始分析</button>
  <span id="statusAigc">请先拖入论文</span>
 </div>
 <div id="aigcWarnings"></div>
 <section id="aigcResult" hidden></section>
</div>

<footer>查重判定:整句(或较长小句)逐字重复(忽略大小写、全半角、空白与标点),同义改写无法检出;
开放数据源覆盖有限,结果不代表知网 / Turnitin 等系统的结论。AIGC 疑似度为启发式统计,不构成 AI 写作的证明。</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const ACCEPT = [".docx", ".pdf", ".txt", ".md", ".markdown"];
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtSize = n => n < 1024 ? n + " B" : n < 1048576 ? (n/1024).toFixed(0) + " KB" : (n/1048576).toFixed(1) + " MB";

/* ---------- 拖拽区 ---------- */
function makeZone(zoneId, inputId, chipsId, opts){
  const api = { files: opts.multiple ? [] : null };
  const chips = $(chipsId);
  function render(){
    if (opts.multiple){
      chips.innerHTML = api.files.map((f,i)=>
        `<span class="chip"><span>${esc(f.name)}(${fmtSize(f.size)})</span><button data-i="${i}">✕</button></span>`).join("");
      chips.querySelectorAll("button").forEach(b=>b.onclick=()=>{api.files.splice(+b.dataset.i,1);render();opts.onChange();});
    } else {
      chips.innerHTML = api.files
        ? `<span class="chip"><span>${esc(api.files.name)}(${fmtSize(api.files.size)})</span><button>✕</button></span>` : "";
      if (api.files) chips.querySelector("button").onclick=()=>{api.files=null;render();opts.onChange();};
    }
    opts.onChange();
  }
  function setFiles(list){
    const ok = list.filter(f=>ACCEPT.includes("."+(f.name.split(".").pop()||"").toLowerCase()));
    if (ok.length < list.length) setStatus(`已忽略 ${list.length-ok.length} 个格式不支持的文件`, true, opts.statusId);
    if (opts.multiple){
      for (const f of ok) if (!api.files.some(g=>g.name===f.name&&g.size===f.size)) api.files.push(f);
    } else if (ok.length) api.files = ok[0];
    render();
  }
  const input = $(inputId);
  $(zoneId).addEventListener("click", e=>{ if(e.target.tagName!=="BUTTON") input.click(); });
  input.addEventListener("change", ()=>{ setFiles([...input.files]); input.value=""; });
  const zone = $(zoneId);
  zone.addEventListener("dragover", e=>{ e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", ()=>zone.classList.remove("drag"));
  zone.addEventListener("drop", async e=>{
    e.preventDefault(); zone.classList.remove("drag");
    setFiles(await collect(e.dataTransfer));
  });
  return api;
}
async function collect(dt){
  const out = [];
  const entries = (dt.items ? [...dt.items] : []).map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  if (entries.length){
    await Promise.all(entries.map(en=>walk(en,out,"")));
    if (out.length) return out;
  }
  return [...dt.files];
  function walk(entry,out,prefix){
    return new Promise(res=>{
      if (entry.isFile) entry.file(f=>{f._rel=prefix+f.name;out.push(f);res();},()=>res());
      else if (entry.isDirectory){
        const r = entry.createReader(); const jobs=[];
        const batch=()=>r.readEntries(async es=>{
          if(!es.length){ await Promise.all(jobs.map(j=>j())); res(); }
          else { es.forEach(e=>jobs.push(()=>walk(e,out,prefix+entry.name+"/"))); batch(); }
        },()=>res());
        batch();
      } else res();
    });
  }
}

/* ---------- 状态与模式 ---------- */
let onlinePaper = null, localPaper = null, localRefs = [];
function setStatus(msg, err, id){
  const el = $(id || "status"); el.textContent = msg; el.classList.toggle("err", !!err);
}
const TABS = {online: ["tabBtnOnline", "tabOnline"], local: ["tabBtnLocal", "tabLocal"],
              aigc: ["tabBtnAigc", "tabAigc"]};
function switchTab(which){
  for (const [name, ids] of Object.entries(TABS)){
    $(ids[0]).classList.toggle("on", name === which);
    $(ids[1]).classList.toggle("on", name === which);
  }
}
$("tabBtnOnline").onclick = ()=>switchTab("online");
$("tabBtnLocal").onclick = ()=>switchTab("local");
$("tabBtnAigc").onclick = ()=>switchTab("aigc");

/* ---------- 在线检索 ---------- */
const onlineApi = makeZone("onlinePaperZone","onlinePaperInput","onlinePaperChips",{
  multiple:false, statusId:"status",
  onChange:()=>{ onlinePaper = onlineApi.files; $("onlineGo").disabled = !onlinePaper;
                 if (onlinePaper) setStatus("已就绪,点击开始在线检索"); }
});
$("onlineGo").onclick = async ()=>{
  if (!onlinePaper) return;
  const sources = [...document.querySelectorAll(".srcck:checked")].map(c=>c.value);
  if (!sources.length){ setStatus("请至少勾选一个检索来源", true); return; }
  const fd = new FormData();
  fd.append("paper", onlinePaper, onlinePaper.name);
  fd.append("sources", sources.join(","));
  $("onlineGo").disabled = true; setStatus("正在提交…");
  $("prog").style.display = "block"; $("progBar").style.width = "4%";
  $("progText").textContent = "正在切句…"; $("progHits").innerHTML = "";
  $("onlineResult").hidden = true;
  try {
    const r = await fetch("/api/check_online", {method:"POST", body:fd});
    const d = await r.json();
    if (!d.ok){ setStatus(d.error||"提交失败", true); $("onlineGo").disabled=false; $("prog").style.display="none"; return; }
    pollJob(d.job_id);
  } catch(e){ setStatus("请求失败:"+e.message, true); $("onlineGo").disabled=false; $("prog").style.display="none"; }
};
function pollJob(id){
  const timer = setInterval(async ()=>{
    let d;
    try { d = await (await fetch("/api/job/"+id)).json(); }
    catch(e){ return; }
    if (!d.ok){ clearInterval(timer); setStatus(d.error, true); $("onlineGo").disabled=false; return; }
    const pct = d.total ? Math.round(d.done/d.total*100) : 4;
    $("progBar").style.width = Math.max(pct,4) + "%";
    $("progText").textContent = d.status==="done" ? "检索完成" :
      `已检索 ${d.done}/${d.total||"…"} 句 · 命中 ${d.hits_count} 处`;
    if (d.status==="running" && d.recent_hits && d.recent_hits.length){
      $("progHits").innerHTML = d.recent_hits.slice(-2).map(h=>
        `<div class="livehit">🔎 <b>${esc(h.text.slice(0,36))}…</b> <span class="m">← ${esc((h.sources[0]||{}).source_label||"")} · ${esc(((h.sources[0]||{}).title||"").slice(0,40))}</span></div>`).join("");
    }
    if (d.status==="done"){
      clearInterval(timer);
      $("prog").style.display = "none";
      $("onlineGo").disabled = false;
      setStatus("检索完成");
      renderOnline(d.result);
    } else if (d.status==="error"){
      clearInterval(timer);
      $("prog").style.display = "none";
      $("onlineGo").disabled = false;
      setStatus("检索失败:"+(d.error||"未知错误"), true);
    }
  }, 1000);
}
function renderOnline(d){
  $("onlineWarnings").innerHTML = (d.warnings||[]).map(w=>`<div class="warn">⚠️ ${esc(w)}</div>`).join("");
  const cls = d.ratio>=0.3?"high":d.ratio>=0.1?"mid":"ok";
  const bandTxt = d.ratio>=0.3?"高":d.ratio>=0.1?"中":"低";
  const labels = {openalex:"OpenAlex 学术库", europepmc:"Europe PMC 开放全文", web:"网页检索(已核验)"};
  const srcRows = Object.entries(d.source_counts||{}).filter(([k,v])=>v)
    .map(([k,v])=>`<tr><td>${labels[k]||k}</td><td><b>${v}</b> 处</td></tr>`).join("");
  const hitsHtml = (d.hit_details||[]).length ? d.hit_details.map(h=>`
    <div class="run">
      <div class="meta">第 ${h.para} 段 · 命中 ${h.matched_len} 字</div>
      <div>${esc(h.text)}</div>
      ${h.sources.map(s=>`<div class="src">↳ <a href="${esc(s.url||"#")}" target="_blank" rel="noreferrer">${esc(s.title||"(无标题)")}</a>
        <span style="color:var(--sub)">(${esc(s.source_label||"")}${s.meta?" / "+esc(s.meta):""})</span></div>
        ${s.snippet?`<div class="quote">原文片段:${esc(s.snippet)}</div>`:""}`).join("")}
    </div>`).join("")
    : `<p class="empty">未发现逐字重复的句子 🎉(注意:同义改写无法检出)</p>`;
  $("onlineResult").innerHTML = `
   <div class="card verdict">
     <div><div class="pct ${cls}">${(d.ratio*100).toFixed(1)}%</div><div style="color:var(--sub)">在线重复率</div></div>
     <div><span class="badge ${cls}">${bandTxt}</span>
       <div class="meta2">论文 ${esc(d.paper.name)}(${d.paper.tokens.toLocaleString()} 词元)·
       检索 ${d.sentences_total} 句,命中 ${d.sentences_matched} 句(重复文字 ${d.matched_chars.toLocaleString()} 字)</div></div>
   </div>
   ${srcRows?`<div class="card"><h3>各来源命中</h3><table><tr><th>来源</th><th>命中</th></tr>${srcRows}</table></div>`:""}
   <div class="card"><h3>命中片段(共 ${d.hit_details.length} 处)</h3>${hitsHtml}</div>
   <div class="card"><h3>论文全文(黄色 = 检索命中)</h3><div class="fulltext">${d.paragraphs.join("")}</div></div>
   <div class="card dl"><button id="dlBtnOnline">⬇ 下载完整 HTML 报告</button></div>`;
  $("onlineResult").hidden = false;
  $("dlBtnOnline").onclick = ()=>{
    const blob = new Blob([d.report_html], {type:"text/html;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "在线查重报告_" + d.paper.name.replace(/\\.[^.]+$/, "") + ".html";
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 5000);
  };
  $("onlineResult").scrollIntoView({behavior:"smooth"});
}

/* ---------- AIGC 疑似分析 ---------- */
let aigcPaper = null;
function setStatusAigc(msg, err){ setStatus(msg, err, "statusAigc"); }
const aigcApi = makeZone("aigcPaperZone","aigcPaperInput","aigcPaperChips",{
  multiple:false, statusId:"statusAigc",
  onChange:()=>{ aigcPaper = aigcApi.files; $("aigcGo").disabled = !aigcPaper;
                 if (aigcPaper) setStatusAigc("已就绪,点击开始分析"); }
});
$("aigcGo").onclick = async ()=>{
  if (!aigcPaper) return;
  const fd = new FormData();
  fd.append("paper", aigcPaper, aigcPaper.name);
  $("aigcGo").disabled = true; setStatusAigc("正在分析…");
  try {
    const r = await fetch("/api/check_aigc", {method:"POST", body:fd});
    const d = await r.json();
    if (!d.ok){ setStatusAigc(d.error||"分析失败", true); return; }
    setStatusAigc("分析完成"); renderAigc(d);
  } catch(e){ setStatusAigc("请求失败:"+e.message, true); }
  finally { $("aigcGo").disabled = !aigcPaper; }
};
function renderAigc(d){
  $("aigcWarnings").innerHTML = (d.warnings||[]).map(w=>`<div class="warn">⚠️ ${esc(w)}</div>`).join("");
  const cls = d.score>=70?"high":d.score>=45?"mid":"ok";
  const bandTxt = d.score>=70?"高疑似":d.score>=45?"中等疑似":"低疑似";
  const sigRows = d.signals.map(s=>`<tr><td>${esc(s.name)}</td><td>${esc(s.value)}</td><td>${s.score}</td></tr>`).join("");
  const framesHtml = d.frames.length ? d.frames.map(f=>`
    <div class="run"><div class="meta">第 ${f.para||"?"} 段 · ${esc(f.label)}</div>${esc(f.text)}</div>`).join("")
    : `<p class="empty">未命中模板短语</p>`;
  $("aigcResult").innerHTML = `
   <div class="card verdict">
     <div><div class="pct ${cls}">${d.score}</div><div style="color:var(--sub)">AIGC 疑似度(0-100)</div></div>
     <div><span class="badge ${cls}">${bandTxt}</span>
       <div class="meta2">论文 ${esc(d.paper.name)} · ${d.sentences_total} 句 · 离线启发式统计</div></div>
   </div>
   <div class="card"><h3>各信号明细</h3><table><tr><th>信号</th><th>取值</th><th>贡献(0-100)</th></tr>${sigRows}</table></div>
   <div class="card"><h3>模板短语命中(共 ${d.frames.length} 处)</h3>${framesHtml}</div>
   <div class="card"><h3>全文标注(橙 = 高疑似句,黄 = 中等疑似句)</h3><div class="fulltext">${d.paragraphs.join("")}</div></div>
   <div class="card dl"><button id="dlBtnAigc">⬇ 下载完整 HTML 报告</button>
     <div class="meta2" style="margin-top:8px">⚠️ 启发式统计,不构成 AI 写作的证明:模板化的人写论文也会得分,
     AI 写的朴素句子也可能低分,商用检测器同样有误判。仅作写作风格自查参考。</div></div>`;
  $("aigcResult").hidden = false;
  $("dlBtnAigc").onclick = ()=>{
    const blob = new Blob([d.report_html], {type:"text/html;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "AIGC疑似度报告_" + d.paper.name.replace(/\\.[^.]+$/, "") + ".html";
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 5000);
  };
  $("aigcResult").scrollIntoView({behavior:"smooth"});
}

/* ---------- 本地比对 ---------- */
const paperApi = makeZone("paperZone","paperInput","paperChips",{
  multiple:false, statusId:"statusLocal",
  onChange:()=>{ localPaper = paperApi.files; refreshLocal(); }
});
const refsApi = makeZone("refsZone","refsInput","refsChips",{
  multiple:true, statusId:"statusLocal",
  onChange:()=>{ localRefs = refsApi.files; refreshLocal(); }
});
function refreshLocal(){
  $("goBtn").disabled = !(localPaper && localRefs.length);
  if ($("goBtn").disabled) setStatus("请先选择论文与参考文献", false, "statusLocal");
}
$("goBtn").onclick = async ()=>{
  const fd = new FormData();
  fd.append("paper", localPaper, localPaper.name);
  localRefs.forEach(f=>fd.append("refs", f, f.name));
  const k = $("kInput").value.trim(), mr = $("minRunInput").value.trim();
  if (k) fd.append("k", k);
  if (mr) fd.append("min_run", mr);
  $("goBtn").disabled = true; setStatusLocal("正在比对,请稍候…");
  try {
    const r = await fetch("/api/check", {method:"POST", body:fd});
    const d = await r.json();
    if (!d.ok){ setStatusLocal(d.error||"检测失败", true); return; }
    setStatusLocal("比对完成"); renderLocal(d);
  } catch(e){ setStatusLocal("请求失败:"+e.message, true); }
  finally { refreshLocal(); }
};
function setStatusLocal(msg, err){ setStatus(msg, err, "statusLocal"); }
function renderLocal(d){
  $("warnings").innerHTML = (d.warnings||[]).map(w=>`<div class="warn">⚠️ ${esc(w)}</div>`).join("");
  const cls = d.overall>=0.3?"high":d.overall>=0.1?"mid":"ok";
  const bandTxt = d.overall>=0.3?"高":d.overall>=0.1?"中":"低";
  const refRows = d.refs.map((r,i)=>`<tr><td>${i+1}</td><td>${esc(r.name)}</td><td>${r.tokens.toLocaleString()}</td>
     <td><b>${(r.ratio*100).toFixed(1)}%</b></td><td>${r.runs} 处</td></tr>`).join("");
  const runsHtml = d.runs.length ? d.runs.map(r=>`
    <div class="run">
      <div class="meta">长度 ${r.tokens} 词元 · 论文第 ${r.paras.join("、")} 段 · 来源:${r.sources.map(esc).join("、")}</div>
      <div><span class="lbl">论文</span>${r.paper_html}</div>
      ${r.ref_html?`<div><span class="lbl">参考</span>${r.ref_html}</div>`:""}
    </div>`).join("") : `<p class="empty">未发现达到判定窗口的重复内容 🎉</p>`;
  $("result").innerHTML = `
   <div class="card verdict">
     <div><div class="pct ${cls}">${(d.overall*100).toFixed(1)}%</div><div style="color:var(--sub)">总重复率</div></div>
     <div><span class="badge ${cls}">${bandTxt}</span>
       <div class="meta2">判定窗口:连续 ${d.k} 词元 · 论文 ${d.paper.tokens.toLocaleString()} 词元 · 参考文献 ${d.refs.length} 篇</div></div>
   </div>
   <div class="card"><h3>各来源重合度</h3><table><tr><th>#</th><th>参考文献</th><th>词元数</th><th>与论文重合</th><th>匹配片段</th></tr>${refRows}</table></div>
   <div class="card"><h3>重复片段(共 ${d.runs.length} 处)</h3>${runsHtml}</div>
   <div class="card"><h3>论文全文(黄色 = 与参考文献重复)</h3><div class="fulltext">${d.paragraphs.join("")}</div></div>
   <div class="card dl"><button id="dlBtn">⬇ 下载完整 HTML 报告</button></div>`;
  $("result").hidden = false;
  $("dlBtn").onclick = ()=>{
    const blob = new Blob([d.report_html], {type:"text/html;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "查重报告_" + d.paper.name.replace(/\\.[^.]+$/, "") + ".html";
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 5000);
  };
  $("result").scrollIntoView({behavior:"smooth"});
}
</script>
</body>
</html>
"""


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="论文查重网页版(本地运行)")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(默认仅本机)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args(argv)

    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 52)
    print("📄 论文查重网页版已启动(本地运行)")
    print(f"   地址:{url}")
    print("   模式:在线自动检索(默认)/ 本地文献比对")
    print("   按 Ctrl+C 停止服务")
    print("=" * 52)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
