#!/usr/bin/env python3
"""Morning Coffee — an agent test bench.

Claude Code (in CI, on a subscription) produces a "digest" JSON; this script
renders it to an interactive web page + PDF, archives it under docs/, rebuilds
the archive index, and emails it. The content is whatever the prompt asks for
(currently: a job-postings digest) — this script only renders and ships it.

A digest is generic:
    {
      "title": "Job Radar",
      "intro": "one or two sentences",
      "sections": [
        {"heading": "Source", "items": [
          {"title": "...", "subtitle": "...", "meta": "...", "summary": "...", "url": "..."}
        ]}
      ],
      "sources": ["label", ...]
    }
Only `title`/`heading`/`items` and each item's `title` are required.

Modes:
    python generate.py --sample                  # canned sample_digest.json, no email
    python generate.py --from-json data.json      # render a digest Claude wrote
    python generate.py --from-json data.json --no-email
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
NEWSLETTERS = DOCS / "newsletters"
DEFAULT_SITE = "https://egil10.github.io/morning-coffee"

esc = html.escape


def now_local() -> datetime:
    """Best-effort Oslo time; falls back to system local if tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Oslo"))
    except Exception:
        return datetime.now()


# --------------------------------------------------------------------------- #
# load + validate
# --------------------------------------------------------------------------- #
def parse_digest_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        blob = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("No JSON object found in input:\n" + text[:500])
        blob = text[start : end + 1]
    return json.loads(blob)


def validate_digest(d: dict) -> None:
    if not d.get("title"):
        d["title"] = "Morning Coffee"
    d.setdefault("intro", "")
    secs = d.get("sections")
    if not isinstance(secs, list) or not secs:
        raise ValueError("digest needs a non-empty 'sections' list")
    for s in secs:
        if not s.get("heading"):
            raise ValueError("each section needs a 'heading'")
        items = s.setdefault("items", [])
        if not isinstance(items, list):
            raise ValueError(f"section '{s['heading']}' needs an 'items' list")
        for it in items:
            if not it.get("title"):
                raise ValueError(f"an item in '{s['heading']}' is missing a 'title'")
    d.setdefault("sources", [])


def item_count(d: dict) -> int:
    return sum(len(s.get("items", [])) for s in d["sections"])


# --------------------------------------------------------------------------- #
# rendering — web page
# --------------------------------------------------------------------------- #
def _item_html(it: dict) -> str:
    if it.get("url"):
        title = '<a class="item-title" href="{u}" target="_blank" rel="noopener">{t}</a>'.format(
            u=esc(it["url"]), t=esc(it["title"])
        )
    else:
        title = '<span class="item-title">{t}</span>'.format(t=esc(it["title"]))
    meta = '<span class="item-meta tnum">{m}</span>'.format(m=esc(it["meta"])) if it.get("meta") else ""
    sub = '<div class="item-sub">{s}</div>'.format(s=esc(it["subtitle"])) if it.get("subtitle") else ""
    summ = '<p class="item-sum">{s}</p>'.format(s=esc(it["summary"])) if it.get("summary") else ""
    return (
        '<article class="card item"><div class="item-top">{title}{meta}</div>'
        "{sub}{summ}</article>"
    ).format(title=title, meta=meta, sub=sub, summ=summ)


def _sections_html(d: dict) -> str:
    blocks = []
    for s in d["sections"]:
        items = s.get("items", [])
        body = "".join(_item_html(it) for it in items) or '<p class="note">Nothing new found here today.</p>'
        blocks.append(
            '<div class="block"><h2 class="block-h">{h} '
            '<span class="count tnum">({n})</span></h2>'
            '<div class="stack">{body}</div></div>'.format(h=esc(s["heading"]), n=len(items), body=body)
        )
    return "".join(blocks)


def render_web(d: dict, date_str: str, generated_at: str) -> str:
    sources = " · ".join(esc(s) for s in d.get("sources", [])) or "various sources"
    hero = (
        '<header class="hero grain"><div class="wrap">'
        '<div class="topbar">'
        '<div class="crumb">&#9749; <span>morning coffee</span> '
        '<span class="dot">·</span> <span class="tnum">{date}</span></div>'
        '<button id="theme-toggle" class="ghost">theme</button></div>'
        '<h1 class="headline">{title}</h1>'
        '<p class="lede">{intro}</p>'
        '<p style="margin-top:16px"><a href="../../index.html">&larr; all editions</a></p>'
        "</div></header>"
    ).format(date=esc(date_str), title=esc(d["title"]), intro=esc(d["intro"]))

    body = (
        '<section class="body"><div class="wrap">{sections}'
        '<p class="foot" style="margin-top:30px">gathered from: {sources}</p>'
        "</div></section>"
        '<footer><div class="wrap"><p class="foot">'
        '{n} items · generated {ts} · <a href="digest.pdf">download pdf</a>'
        "</p></div></footer>"
    ).format(sections=_sections_html(d), sources=sources, n=item_count(d), ts=esc(generated_at))

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>{title} · {date}</title>"
        '<link rel="stylesheet" href="../../style.css"></head><body>'
        "{hero}{body}"
        '<script src="../../app.js"></script></body></html>'
    ).format(title=esc(d["title"]), date=esc(date_str), hero=hero, body=body)


# --------------------------------------------------------------------------- #
# rendering — archive index
# --------------------------------------------------------------------------- #
def build_index(site_base: str) -> str:
    editions = []
    if NEWSLETTERS.exists():
        for dir_ in sorted(NEWSLETTERS.iterdir(), reverse=True):
            meta = dir_ / "data.json"
            if not (dir_.is_dir() and meta.exists()):
                continue
            try:
                rec = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            intro = rec.get("intro", "")
            if len(intro) > 150:
                intro = intro[:147].rstrip() + "…"
            editions.append(
                '<a class="card edition" href="newsletters/{date}/index.html">'
                '<div class="date tnum">{pretty}</div>'
                "<h3>{title}</h3><p>{intro}</p>"
                '<span class="more">open &rarr;</span></a>'.format(
                    date=esc(dir_.name),
                    pretty=esc(rec.get("pretty", dir_.name)),
                    title=esc(rec.get("title", "Morning Coffee")),
                    intro=esc(intro),
                )
            )

    count = len(editions)
    grid = "".join(editions) if editions else '<p class="empty">No editions yet — check back soon.</p>'
    hero = (
        '<header class="hero grain"><div class="wrap">'
        '<div class="topbar"><div class="crumb">&#9749; <span>morning coffee</span></div>'
        '<button id="theme-toggle" class="ghost">theme</button></div>'
        '<h1 class="headline">small automated <span class="accent">briefings</span>, '
        "every morning.</h1>"
        '<p class="lede">Gathered by an agent, emailed at dawn, archived here. '
        '<span class="tnum">{count}</span> {ed} and counting.</p>'
        "</div></header>"
    ).format(count=count, ed="edition" if count == 1 else "editions")

    body = (
        '<section class="body"><div class="wrap">'
        '<div class="grid">{grid}</div></div></section>'
        '<footer><div class="wrap"><p class="foot">'
        "Morning Coffee · an automated agent test bench · "
        '<a href="{site}">{site}</a></p></div></footer>'
    ).format(grid=grid, site=esc(site_base))

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Morning Coffee — automated briefings</title>"
        '<link rel="stylesheet" href="style.css"></head><body>'
        "{hero}{body}"
        '<script src="app.js"></script></body></html>'
    ).format(hero=hero, body=body)


# --------------------------------------------------------------------------- #
# rendering — print / PDF (self-contained, light)
# --------------------------------------------------------------------------- #
PRINT_STYLE = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #171716; line-height: 1.5; margin: 0; }
.crumb { font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: #73736e; }
h1 { font-size: 26px; letter-spacing: -0.02em; margin: 6px 0 10px; }
.lede { color: #57574f; font-size: 13px; margin: 0 0 20px; max-width: 60ch; }
h2 { font-size: 12px; text-transform: lowercase; letter-spacing: .06em; color: #73736e;
  margin: 22px 0 8px; padding-top: 12px; border-top: 1px solid #e2e2e0; }
.item { padding: 7px 0; page-break-inside: avoid; }
.it-title { font-size: 14px; font-weight: 600; color: #171716; text-decoration: none; }
.it-sub { font-size: 12px; color: #0d6c63; }
.it-meta { font-size: 11px; color: #a8a8a2; }
.it-sum { font-size: 12px; color: #57574f; margin: 3px 0 0; }
.note { font-size: 12px; color: #a8a8a2; }
.foot { margin-top: 24px; padding-top: 12px; border-top: 1px solid #e2e2e0;
  font-size: 11px; color: #a8a8a2; }
"""


def render_print(d: dict, pretty: str) -> str:
    blocks = []
    for s in d["sections"]:
        rows = []
        for it in s.get("items", []):
            title = (
                '<a class="it-title" href="{u}">{t}</a>'.format(u=esc(it["url"]), t=esc(it["title"]))
                if it.get("url")
                else '<span class="it-title">{t}</span>'.format(t=esc(it["title"]))
            )
            sub = ' — <span class="it-sub">{s}</span>'.format(s=esc(it["subtitle"])) if it.get("subtitle") else ""
            meta = '<div class="it-meta">{m}</div>'.format(m=esc(it["meta"])) if it.get("meta") else ""
            summ = '<div class="it-sum">{s}</div>'.format(s=esc(it["summary"])) if it.get("summary") else ""
            rows.append('<div class="item">{title}{sub}{meta}{summ}</div>'.format(title=title, sub=sub, meta=meta, summ=summ))
        body = "".join(rows) or '<div class="note">Nothing new found here today.</div>'
        blocks.append("<h2>{h}</h2>{body}".format(h=esc(s["heading"]), body=body))
    sources = " · ".join(esc(s) for s in d.get("sources", [])) or "various sources"
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>{style}</style></head>'
        '<body><div class="crumb">&#9749; morning coffee</div>'
        "<h1>{title} — {pretty}</h1>"
        '<p class="lede">{intro}</p>{blocks}'
        '<p class="foot">gathered from: {sources}</p>'
        "</body></html>"
    ).format(
        style=PRINT_STYLE, title=esc(d["title"]), pretty=esc(pretty),
        intro=esc(d["intro"]), blocks="".join(blocks), sources=sources,
    )


def render_pdf(html_str: str, out_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover
        print(f"  ! playwright unavailable ({e}); skipping PDF", file=sys.stderr)
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_str, wait_until="load")
        page.pdf(path=str(out_path), format="A4", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
    return True


# --------------------------------------------------------------------------- #
# rendering — email (inline styles)
# --------------------------------------------------------------------------- #
def render_email_html(d: dict, pretty: str, site_url: str) -> str:
    BG, CARD, B, FG, MUTED, ACCENT = "#FCFCFA", "#FFFFFF", "#E2E2E0", "#171716", "#73736E", "#0D9488"
    sec_rows = []
    for s in d["sections"]:
        items = s.get("items", [])
        rows = []
        for it in items:
            title = it["title"]
            if it.get("url"):
                title = '<a href="{u}" style="color:{fg};text-decoration:none">{t}</a>'.format(
                    u=esc(it["url"]), fg=FG, t=esc(it["title"])
                )
            else:
                title = esc(it["title"])
            sub = ' <span style="color:{acc}">— {s}</span>'.format(acc=ACCENT, s=esc(it["subtitle"])) if it.get("subtitle") else ""
            meta = '<div style="font-size:12px;color:{m}">{x}</div>'.format(m=MUTED, x=esc(it["meta"])) if it.get("meta") else ""
            summ = '<div style="font-size:13px;color:{m};margin-top:2px">{x}</div>'.format(m=MUTED, x=esc(it["summary"])) if it.get("summary") else ""
            rows.append(
                '<div style="padding:10px 0;border-top:1px solid {b}">'
                '<div style="font-size:15px;font-weight:600;color:{fg}">{title}{sub}</div>'
                "{meta}{summ}</div>".format(b=B, fg=FG, title=title, sub=sub, meta=meta, summ=summ)
            )
        body = "".join(rows) or '<div style="font-size:13px;color:{m};padding:8px 0">Nothing new found here today.</div>'.format(m=MUTED)
        sec_rows.append(
            '<tr><td style="padding:14px 28px 0">'
            '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:{acc}">{h}</div>'
            "{body}</td></tr>".format(acc=ACCENT, h=esc(s["heading"]), body=body)
        )

    return (
        '<body style="margin:0;background:{bg};font-family:-apple-system,Segoe UI,Roboto,'
        'Helvetica,Arial,sans-serif"><table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="background:{bg}"><tr><td align="center" style="padding:28px 16px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:620px;background:{card};border:1px solid {b};border-radius:14px">'
        '<tr><td style="padding:28px 28px 6px">'
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:{muted}">'
        "&#9749; morning coffee &middot; {pretty}</div>"
        '<div style="font-size:24px;font-weight:600;color:{fg};letter-spacing:-.02em;margin:8px 0 6px">{title}</div>'
        '<div style="font-size:14px;color:{muted}">{intro}</div></td></tr>'
        "{sec_rows}"
        '<tr><td style="padding:18px 28px 26px;border-top:1px solid {b}">'
        '<div style="font-size:11px;color:{muted}">Archived at '
        '<a href="{site}" style="color:{acc}">{site}</a> · full list attached as PDF.</div>'
        "</td></tr></table></td></tr></table></body>"
    ).format(
        bg=BG, card=CARD, b=B, fg=FG, muted=MUTED, acc=ACCENT,
        pretty=esc(pretty), title=esc(d["title"]), intro=esc(d["intro"]),
        sec_rows="".join(sec_rows), site=esc(site_url),
    )


def render_email_text(d: dict, pretty: str, site_url: str) -> str:
    lines = [f"{d['title']} — {pretty}", "", d["intro"], ""]
    for s in d["sections"]:
        lines.append(f"== {s['heading']} ==")
        items = s.get("items", [])
        if not items:
            lines.append("  (nothing new found)")
        for it in items:
            bits = [it["title"]]
            if it.get("subtitle"):
                bits.append(f"— {it['subtitle']}")
            if it.get("meta"):
                bits.append(f"({it['meta']})")
            lines.append("  • " + " ".join(bits))
            if it.get("summary"):
                lines.append(f"    {it['summary']}")
            if it.get("url"):
                lines.append(f"    {it['url']}")
        lines.append("")
    lines.append(f"Archive: {site_url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# email
# --------------------------------------------------------------------------- #
def send_email(subject, html_body, text_body, pdf_path):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")  # Gmail shows it in groups of 4
    recipient = os.environ.get("RECIPIENT") or sender

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Morning Coffee <{sender}>"
    msg["To"] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if pdf_path and pdf_path.exists():
        msg.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf",
                           filename=pdf_path.name)

    import smtplib

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(sender, password)
        s.send_message(msg)
    print(f"  → emailed to {recipient}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Render + ship a Morning Coffee digest.")
    ap.add_argument("--sample", action="store_true", help="use sample_digest.json; no email")
    ap.add_argument("--from-json", default=None, metavar="PATH", help="render a digest JSON (e.g. from Claude Code)")
    ap.add_argument("--no-email", action="store_true", help="generate and save, but do not send email")
    ap.add_argument("--base-url", default=None, help="override the public site base URL")
    args = ap.parse_args()

    site = args.base_url or os.environ.get("SITE_BASE_URL") or DEFAULT_SITE

    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    pretty = now.strftime("%A, %d %B %Y")
    generated_at = now.strftime("%Y-%m-%d %H:%M %Z").strip()

    if args.sample:
        print("• sample mode — loading sample_digest.json")
        d = json.loads((ROOT / "sample_digest.json").read_text(encoding="utf-8"))
    elif args.from_json:
        print(f"• loading digest from {args.from_json}")
        d = parse_digest_json(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        raise SystemExit("nothing to render: pass --sample or --from-json PATH")
    validate_digest(d)
    print(f"  '{d['title']}' — {item_count(d)} items across {len(d['sections'])} sections")

    d["date"] = date_str
    d["pretty"] = pretty
    d["generated_at"] = generated_at

    nl_dir = NEWSLETTERS / date_str
    nl_dir.mkdir(parents=True, exist_ok=True)
    (nl_dir / "data.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    (nl_dir / "index.html").write_text(render_web(d, date_str, generated_at), encoding="utf-8")
    print(f"  wrote {nl_dir.relative_to(ROOT)}/index.html")

    pdf_path = nl_dir / "digest.pdf"
    ok = render_pdf(render_print(d, pretty), pdf_path)
    if ok:
        print(f"  wrote {pdf_path.relative_to(ROOT)}")
    elif not args.sample:
        raise SystemExit("PDF generation failed (Playwright/Chromium required)")

    (DOCS / "index.html").write_text(build_index(site), encoding="utf-8")
    print("  rebuilt docs/index.html")

    if args.sample or args.no_email:
        print("• skipping email")
    elif os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"):
        send_email(
            f"☕ {d['title']} — {date_str}",
            render_email_html(d, pretty, site),
            render_email_text(d, pretty, site),
            pdf_path if ok else None,
        )
    else:
        print("• GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — skipping email (site + PDF still built)")

    print(f"✓ done — {site}/newsletters/{date_str}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
