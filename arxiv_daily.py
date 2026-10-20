#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
arXiv 每日论文摘要邮件
======================

每天抓取 arXiv 指定分类（默认 cs.CL）最新提交的 N 篇论文（默认 50 篇），
把标题、作者、分类和完整摘要排版成 HTML 邮件发送到指定邮箱。

用法：
    python arxiv_daily.py              # 抓取并发送
    python arxiv_daily.py --dry-run    # 只抓取，生成 out/preview.html 预览，不发邮件
    python arxiv_daily.py --test-email # 发一封测试邮件，检查 SMTP 配置
    python arxiv_daily.py --limit 20   # 临时改为 20 篇
    python arxiv_daily.py --category cs.AI cs.LG --limit 50
    python arxiv_daily.py --force      # 即使今天没有新论文也发送（重发最新 N 篇）

配置文件：同目录下的 config.ini
已发送记录：state/seen.json（用于跨天去重，避免重复推送）
"""

from __future__ import annotations

import argparse
import configparser
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
OUT_DIR = BASE_DIR / "out"
STATE_PATH = STATE_DIR / "seen.json"
LOG_PATH = LOG_DIR / "arxiv_daily.log"

API_URL = "https://export.arxiv.org/api/query"
API_URL_FALLBACK = "http://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

log = logging.getLogger("arxiv_daily")


# --------------------------------------------------------------------------- #
# 配置与日志
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_PATH, encoding="utf-8")
    ]
    # pythonw.exe 下 sys.stdout 为 None
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)  # 授权码里可能有 %
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    elif not (os.environ.get("ARXIV_SMTP_USER") and os.environ.get("ARXIV_SMTP_AUTH_CODE")):
        raise SystemExit(
            f"找不到配置文件：{CONFIG_PATH}\n"
            "请复制 config.example.ini 为 config.ini 并填写邮箱信息，"
            "或通过环境变量 ARXIV_SMTP_USER / ARXIV_SMTP_AUTH_CODE / ARXIV_MAIL_TO 提供。"
        )
    return cfg


def cfg_get(cfg: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    return (cfg.get(section, key, fallback=default) or "").strip()


def get_setting(
    cfg: configparser.ConfigParser,
    section: str,
    key: str,
    default: str = "",
    env: str = "",
) -> str:
    """环境变量优先于 config.ini，便于在 GitHub Actions 中用 Secrets 注入。"""
    if env:
        value = os.environ.get(env)
        if value is not None and value.strip():
            return value.strip()
    return cfg_get(cfg, section, key, default)


# --------------------------------------------------------------------------- #
# arXiv 抓取
# --------------------------------------------------------------------------- #
@dataclass
class Paper:
    id: str
    version: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    primary: str = ""
    published: str = ""
    updated: str = ""
    abs_url: str = ""
    pdf_url: str = ""


def _text(node, path: str) -> str:
    return " ".join((node.findtext(path, default="", namespaces=NS) or "").split())


def parse_entry(entry) -> Paper | None:
    raw_id = _text(entry, "atom:id")
    m = re.search(r"/abs/(.+)$", raw_id)
    if not m:
        return None
    ident = m.group(1)
    vm = re.match(r"^(.*?)(v\d+)?$", ident)
    base_id = vm.group(1) if vm else ident
    version = (vm.group(2) or "") if vm else ""

    pdf_url = ""
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = link.get("href", "")
            break

    primary_node = entry.find("arxiv:primary_category", NS)
    primary = primary_node.get("term", "") if primary_node is not None else ""

    return Paper(
        id=base_id,
        version=version,
        title=_text(entry, "atom:title"),
        abstract=_text(entry, "atom:summary"),
        authors=[
            " ".join((a.findtext("atom:name", default="", namespaces=NS) or "").split())
            for a in entry.findall("atom:author", NS)
        ],
        categories=[c.get("term", "") for c in entry.findall("atom:category", NS) if c.get("term")],
        primary=primary,
        published=_text(entry, "atom:published"),
        updated=_text(entry, "atom:updated"),
        abs_url=f"https://arxiv.org/abs/{base_id}",
        pdf_url=pdf_url or f"https://arxiv.org/pdf/{base_id}",
    )


def _fetch_page(
    query: str,
    start: int,
    count: int,
    timeout: int,
    retries: int,
    headers: dict[str, str],
) -> list[Paper]:
    """抓取单页。arXiv 对云 IP 的频率限制较严，429 时做长退避。"""
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": str(start),
        "max_results": str(count),
    }
    delays = [20, 45, 90, 180, 240]
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        url = API_URL if attempt % 2 else API_URL_FALLBACK
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                retry_after = (resp.headers.get("Retry-After") or "").strip()
                wait = int(retry_after) if retry_after.isdigit() else delays[min(attempt - 1, len(delays) - 1)]
                last_err = RuntimeError("HTTP 429 Too Many Requests")
                if attempt < retries:
                    log.warning("arXiv 限流（第 %d/%d 次，start=%d），%d 秒后重试",
                                attempt, retries, start, wait)
                    time.sleep(wait)
                    continue
                break

            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            entries = root.findall("atom:entry", NS)
            return [p for p in (parse_entry(e) for e in entries) if p and p.title]
        except Exception as exc:  # noqa: BLE001 - 网络错误统一重试
            last_err = exc
            log.warning("第 %d/%d 次抓取失败（start=%d）：%s", attempt, retries, start, exc)
            if attempt < retries:
                time.sleep(delays[min(attempt - 1, len(delays) - 1)])

    raise RuntimeError(f"抓取 arXiv 失败（start={start}）：{last_err}")


def fetch_papers_api(
    categories: list[str],
    fetch_count: int,
    timeout: int = 60,
    page_size: int = 100,
    page_delay: int = 20,
    retries: int = 5,
) -> list[Paper]:
    """分页拉取最新论文（含摘要）。

    实测 arXiv 单次 100 条约返回 200，但连续快速请求会 429，
    因此每页之间等待 page_delay 秒。arXiv 建议 UA 中带联系方式。
    """
    query = " OR ".join(f"cat:{c}" for c in categories)
    contact = os.environ.get("ARXIV_CONTACT_EMAIL", "").strip()
    if contact:
        headers = {"User-Agent": f"arxiv-daily/1.0 (mailto:{contact})"}
    else:
        headers = {"User-Agent": "arxiv-daily/1.0 (personal daily digest)"}

    collected: list[Paper] = []
    seen_ids: set[str] = set()
    start = 0

    while len(collected) < fetch_count:
        want = min(page_size, fetch_count - len(collected))
        page = _fetch_page(query, start, want, timeout, retries=retries, headers=headers)
        if not page:
            break
        for paper in page:
            if paper.id not in seen_ids:
                seen_ids.add(paper.id)
                collected.append(paper)
        start += want
        if len(page) < want or len(collected) >= fetch_count:
            break
        time.sleep(page_delay)

    if collected:
        log.info("arXiv API 返回 %d 篇（查询：%s）", len(collected), query)
        return collected[:fetch_count]
    raise RuntimeError("抓取 arXiv 失败：API 返回 0 篇论文")


# --------------------------------------------------------------------------- #
# 网页抓取（API 被 429 时的备用通道）
# --------------------------------------------------------------------------- #
def _html_headers() -> dict[str, str]:
    return {"User-Agent": "Mozilla/5.0 (compatible; arxiv-daily/1.0)"}


def _strip_tags(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(text or "")).split())


def _meta(content: str, name: str) -> str:
    m = re.search(rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"', content, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _format_author(name: str) -> str:
    """arXiv meta 里的作者是 "Last, First"，转成更常见的 "First Last"。"""
    name = name.strip()
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name


def list_recent_ids(category: str, size: int = 100, timeout: int = 45) -> list[str]:
    url = f"https://arxiv.org/list/{category}/recent?skip=0&show={size}"
    resp = requests.get(url, headers=_html_headers(), timeout=timeout)
    resp.raise_for_status()
    ids: list[str] = []
    for pid in re.findall(r'href\s*=\s*["\']/abs/([0-9]{4}\.[0-9]{4,5})["\']', resp.text):
        if pid not in ids:
            ids.append(pid)
    return ids


def parse_abs_page(content: str, paper_id: str) -> Paper | None:
    title = _meta(content, "citation_title")
    if not title:
        return None
    abstract = _meta(content, "citation_abstract")
    if not abstract:
        m = re.search(r'<blockquote class="abstract mathjax">(.*?)</blockquote>', content, re.S)
        if m:
            abstract = _strip_tags(m.group(1)).removeprefix("Abstract:").strip()

    categories: list[str] = []
    m = re.search(r'<td class="tablecell subjects">(.*?)</td>', content, re.S)
    if m:
        categories = re.findall(r"\(([a-z\-]+(?:\.[A-Z]{2})?)\)", _strip_tags(m.group(1)))

    pdf_url = _meta(content, "citation_pdf_url") or f"https://arxiv.org/pdf/{paper_id}"
    date = _meta(content, "citation_date").replace("/", "-")
    return Paper(
        id=paper_id,
        version="",
        title=title,
        abstract=abstract,
        authors=[_format_author(a) for a in re.findall(
            r'<meta name="citation_author" content="([^"]*)"', content
        )],
        categories=categories,
        primary=categories[0] if categories else "",
        published=date,
        updated=date,
        abs_url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=pdf_url,
    )


def fetch_papers_html(
    categories: list[str],
    wanted: int,
    seen: set[str],
    timeout: int = 45,
    listing_size: int = 250,
    abs_delay: float = 1.2,
) -> list[Paper]:
    """从 arXiv 列表页取 ID，再逐篇抓取摘要页。

    arxiv.org 主站对 GitHub Actions 的出口 IP 没有限流，
    因此这是云端运行时的可靠通道。列表页单次最多返回 250 条
    （合法 show 值：25/50/100/250/500/1000/2000）。
    """
    ids: list[str] = []
    for cat in categories:
        try:
            for pid in list_recent_ids(cat, size=listing_size, timeout=timeout):
                if pid not in ids:
                    ids.append(pid)
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            log.warning("获取 %s 列表失败：%s", cat, exc)

    if not ids:
        raise RuntimeError("抓取 arXiv 失败：列表页没有解析到论文 ID")

    todo = [pid for pid in ids if pid not in seen]
    log.info("列表页共 %d 个 ID，未推送 %d 篇，准备抓取摘要", len(ids), len(todo))

    papers: list[Paper] = []
    headers = _html_headers()
    for pid in todo:
        if len(papers) >= wanted:
            break
        if papers:
            time.sleep(abs_delay)
        for attempt in (1, 2):
            try:
                resp = requests.get(f"https://arxiv.org/abs/{pid}", headers=headers, timeout=timeout)
                resp.raise_for_status()
                paper = parse_abs_page(resp.text, pid)
                if paper:
                    papers.append(paper)
                else:
                    log.warning("解析失败：%s", pid)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log.warning("获取 %s 失败：%s", pid, exc)
                else:
                    time.sleep(5)

    log.info("网页抓取完成：%d 篇", len(papers))
    return papers


def fetch_papers(
    categories: list[str],
    fetch_count: int,
    timeout: int = 60,
    seen: set[str] | None = None,
    wanted: int = 50,
    source: str = "auto",
) -> list[Paper]:
    """source: auto（API 优先，失败转网页）/ api / html"""
    seen = seen or set()
    source = (source or "auto").lower()

    if source == "html":
        return fetch_papers_html(categories, wanted, seen, timeout=timeout)
    try:
        return fetch_papers_api(categories, fetch_count, timeout=timeout,
                                retries=5 if source == "api" else 2)
    except Exception as exc:  # noqa: BLE001
        if source == "api":
            raise
        log.warning("API 抓取失败（%s），改用网页列表页", exc)
        return fetch_papers_html(categories, wanted, seen, timeout=timeout)


# --------------------------------------------------------------------------- #
# 去重状态
# --------------------------------------------------------------------------- #
def load_seen() -> dict[str, str]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_seen(seen: dict[str, str], new_ids: list[str], today: str, keep: int = 30000) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for pid in new_ids:
        seen.setdefault(pid, today)
    if len(seen) > keep:
        seen = dict(list(seen.items())[-keep:])
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _authors_line(authors: list[str], limit: int = 6) -> str:
    if not authors:
        return "—"
    if len(authors) > limit:
        return f"{esc(', '.join(authors[:limit]))} 等 {len(authors)} 人"
    return esc(", ".join(authors))


def render_card(idx: int, total: int, p: Paper) -> str:
    cats = " · ".join(esc(c) for c in p.categories[:6]) or esc(p.primary)
    return f"""
      <tr><td style="padding:0 0 16px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;">
          <tr><td style="padding:20px 22px 18px 22px;font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
            <div style="font-size:11px;letter-spacing:.12em;color:#9ca3af;margin-bottom:8px;">
              NO.{idx:02d} <span style="color:#d1d5db;">/ {total}</span>
            </div>
            <a href="{esc(p.abs_url)}"
               style="font-size:17px;line-height:1.45;color:#111827;font-weight:600;text-decoration:none;display:block;">
              {esc(p.title)}
            </a>
            <div style="font-size:13px;color:#6b7280;margin-top:9px;line-height:1.6;">
              {_authors_line(p.authors)}
            </div>
            <div style="font-size:12px;color:#9ca3af;margin-top:5px;">{cats}</div>
            <div style="font-size:14px;line-height:1.8;color:#374151;margin-top:13px;text-align:justify;">
              {esc(p.abstract)}
            </div>
            <div style="margin-top:15px;padding-top:12px;border-top:1px solid #f3f4f6;font-size:13px;">
              <a href="{esc(p.abs_url)}" style="color:#2563eb;text-decoration:none;margin-right:18px;">摘要页 →</a>
              <a href="{esc(p.pdf_url)}" style="color:#2563eb;text-decoration:none;">PDF 下载 →</a>
              <span style="color:#9ca3af;float:right;">arXiv:{esc(p.id)}</span>
            </div>
          </td></tr>
        </table>
      </td></tr>"""


def build_html(papers: list[Paper], categories: list[str], date_str: str) -> str:
    total = len(papers)
    cat_label = " / ".join(categories)
    cards = "".join(render_card(i, total, p) for i, p in enumerate(papers, 1))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>arXiv 每日论文精选 {esc(date_str)}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;">
<div style="background:#f3f4f6;padding:24px 12px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:720px;width:100%;">
        <!-- 头部 -->
        <tr><td style="background:#0f172a;border-radius:14px 14px 0 0;padding:30px 28px;">
          <div style="font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
            <div style="font-size:11px;letter-spacing:.22em;color:#64748b;text-transform:uppercase;margin-bottom:10px;">
              ARXIV DAILY DIGEST
            </div>
            <div style="font-size:24px;font-weight:700;color:#ffffff;letter-spacing:.5px;">
              每日前沿论文 · {esc(cat_label)}
            </div>
            <div style="font-size:14px;color:#94a3b8;margin-top:10px;">
              {esc(date_str)} &nbsp;·&nbsp; 共 <span style="color:#e2e8f0;font-weight:600;">{total}</span> 篇新论文
            </div>
          </div>
        </td></tr>
        <!-- 正文 -->
        <tr><td style="background:#f9fafb;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;padding:22px 20px 6px 20px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{cards}
          </table>
        </td></tr>
        <!-- 底部 -->
        <tr><td style="background:#ffffff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 14px 14px;padding:20px 24px;">
          <div style="font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;font-size:12px;color:#9ca3af;line-height:1.8;">
            本邮件由脚本 arxiv-daily 自动生成，数据来源
            <a href="https://arxiv.org/list/{esc(categories[0])}/recent" style="color:#6b7280;">arXiv.org</a>。<br>
            如需调整分类、数量或发送时间，请修改项目目录下的 config.ini。
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</div>
</body>
</html>"""


def build_text(papers: list[Paper], categories: list[str], date_str: str) -> str:
    lines = [
        f"arXiv 每日前沿论文 · {' / '.join(categories)} · {date_str}",
        f"共 {len(papers)} 篇",
        "",
    ]
    for i, p in enumerate(papers, 1):
        lines += [
            f"[{i:02d}] {p.title}",
            f"作者：{', '.join(p.authors) or '—'}",
            f"分类：{', '.join(p.categories) or p.primary}",
            f"摘要：{p.abstract}",
            f"摘要页：{p.abs_url}",
            f"PDF：{p.pdf_url}",
            "-" * 72,
        ]
    lines.append("本邮件由 arxiv-daily 自动生成。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 邮件发送
# --------------------------------------------------------------------------- #
def smtp_connection(host: str, port: int, user: str, auth_code: str, timeout: int = 60):
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        server.starttls()
        server.ehlo()
    server.login(user, auth_code)
    return server


def send_email(
    *,
    host: str,
    port: int,
    user: str,
    auth_code: str,
    from_name: str,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, user))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    log.info("正在发送邮件到 %s ...", ", ".join(recipients))
    with smtp_connection(host, port, user, auth_code) as server:
        server.send_message(msg)
    log.info("邮件发送成功")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="arXiv 每日论文摘要邮件")
    ap.add_argument("--dry-run", action="store_true", help="只抓取并生成预览，不发送、不记录已读")
    ap.add_argument("--test-email", action="store_true", help="发送一封测试邮件，验证 SMTP 配置")
    ap.add_argument("--limit", type=int, default=None, help="本次发送篇数（覆盖 config.ini）")
    ap.add_argument("--category", nargs="+", default=None, help="arXiv 分类，可多个，如 cs.CL cs.AI")
    ap.add_argument("--force", action="store_true", help="今天没有新论文时也发送最新 N 篇")
    return ap.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()
    cfg = load_config()

    host = get_setting(cfg, "smtp", "host", "smtp.126.com", "ARXIV_SMTP_HOST")
    port = int(get_setting(cfg, "smtp", "port", "465", "ARXIV_SMTP_PORT"))
    user = get_setting(cfg, "smtp", "user", "", "ARXIV_SMTP_USER")
    auth_code = get_setting(cfg, "smtp", "auth_code", "", "ARXIV_SMTP_AUTH_CODE")
    from_name = get_setting(cfg, "smtp", "from_name", "arXiv 论文日报", "ARXIV_FROM_NAME")

    to_raw = get_setting(cfg, "mail", "to", "", "ARXIV_MAIL_TO")
    recipients = [x.strip() for x in re.split(r"[,;，；]", to_raw) if x.strip()]

    categories = args.category or [
        c.strip()
        for c in re.split(
            r"[,，\s]+",
            get_setting(cfg, "arxiv", "categories", "cs.CL", "ARXIV_CATEGORIES"),
        )
        if c.strip()
    ]
    max_papers = args.limit or int(get_setting(cfg, "arxiv", "max_papers", "50", "ARXIV_MAX_PAPERS"))
    fetch_count = int(get_setting(cfg, "arxiv", "lookback_fetch", "200", "ARXIV_LOOKBACK_FETCH"))
    timeout = int(get_setting(cfg, "arxiv", "request_timeout", "60", "ARXIV_REQUEST_TIMEOUT"))
    on_no_new = get_setting(cfg, "app", "on_no_new", "skip", "ARXIV_ON_NO_NEW").lower()
    today = datetime.now().strftime("%Y-%m-%d")

    if args.test_email:
        if not (user and auth_code and recipients):
            raise SystemExit("请先在 config.ini 中填写 smtp.user、smtp.auth_code 和 mail.to")
        send_email(
            host=host, port=port, user=user, auth_code=auth_code, from_name=from_name,
            recipients=recipients,
            subject=f"【测试】arXiv 每日论文邮件配置成功 · {today}",
            html_body="<p style=\"font-family:sans-serif\">配置成功，明天早上 9:00 你会收到第一封论文摘要邮件。</p>",
            text_body="配置成功，明天早上 9:00 你会收到第一封论文摘要邮件。",
        )
        return 0

    # 1. 抓取
    seen = load_seen()

    # 幂等保护：同一天多次触发（例如多个 cron 兜底）只发送一次
    if not args.force and not args.dry_run and any(v == today for v in seen.values()):
        log.info("今天（%s）已经发送过日报，跳过本次运行。", today)
        return 0

    source = get_setting(cfg, "arxiv", "source", "auto", "ARXIV_SOURCE")
    fetch_n = max(fetch_count, max_papers * 3)
    papers = fetch_papers(
        categories, fetch_count=fetch_n, timeout=timeout,
        seen=seen, wanted=max_papers, source=source,
    )

    # 2. 去重（同一响应内部 + 历史记录）
    unique: dict[str, Paper] = {}
    for p in papers:
        unique.setdefault(p.id, p)
    papers = list(unique.values())

    fresh = [p for p in papers if p.id not in seen]

    if not fresh:
        if args.force or on_no_new == "resend":
            log.warning("没有新论文，按配置重发最新 %d 篇", max_papers)
            fresh = papers[:max_papers]
        else:
            log.info("今天没有新论文（%s），跳过发送。", ", ".join(categories))
            return 0

    selected = fresh[:max_papers]
    log.info("新论文 %d 篇，本次发送 %d 篇", len(fresh), len(selected))

    html_body = build_html(selected, categories, today)
    text_body = build_text(selected, categories, today)

    if args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        preview = OUT_DIR / f"preview_{datetime.now():%Y%m%d_%H%M%S}.html"
        preview.write_text(html_body, encoding="utf-8")
        log.info("预览已生成：%s（未发送邮件、未记录已读）", preview)
        for i, p in enumerate(selected, 1):
            log.info("  %02d. %s  [%s]", i, p.title[:80], p.id)
        return 0

    # 3. 校验收件配置
    if not (user and auth_code and recipients):
        raise SystemExit(
            "请先在 config.ini 中填写：\n"
            "  [smtp] user = 你的邮箱@126.com\n"
            "  [smtp] auth_code = SMTP 授权码（不是登录密码）\n"
            "  [mail] to = 收件邮箱"
        )

    subject = f"arXiv 每日论文 · {'/'.join(categories)} · {today} · {len(selected)} 篇"
    send_email(
        host=host, port=port, user=user, auth_code=auth_code, from_name=from_name,
        recipients=recipients,
        subject=subject, html_body=html_body, text_body=text_body,
    )

    # 4. 发送成功后才记录，失败可下次重试
    save_seen(seen, [p.id for p in selected], today)
    log.info("完成：%s 共发送 %d 篇", today, len(selected))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 记录任何未捕获错误
        logging.getLogger("arxiv_daily").exception("运行失败：%s", exc)
        sys.exit(1)
