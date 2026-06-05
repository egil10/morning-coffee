#!/usr/bin/env python3
"""Morning Coffee — generate a daily news quiz, render it (web + PDF), archive
it, and email it.

Usage:
    python generate.py              # live: web-sourced quiz, save, email
    python generate.py --sample     # use sample_quiz.json, no API, no email
    python generate.py --no-email   # generate + save, but don't send email

Environment (live mode):
    ANTHROPIC_API_KEY    required — the Claude API key
    GMAIL_ADDRESS        required to email — Gmail account that sends
    GMAIL_APP_PASSWORD   required to email — a Google App Password
    RECIPIENT            optional — defaults to GMAIL_ADDRESS
    MODEL                optional — defaults to claude-opus-4-8
    SITE_BASE_URL        optional — defaults to the GitHub Pages URL
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
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_SITE = "https://egil10.github.io/morning-coffee"

esc = html.escape


# --------------------------------------------------------------------------- #
# date
# --------------------------------------------------------------------------- #
def now_local() -> datetime:
    """Best-effort Oslo time; falls back to system local if tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Oslo"))
    except Exception:
        return datetime.now()


# --------------------------------------------------------------------------- #
# quiz generation (Claude + web search)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are the editor of 'Morning Coffee', a daily current-affairs quiz that "
    "lands in someone's inbox with their morning coffee. You write smart, fair, "
    "globally minded multiple-choice questions about the most significant news of "
    "the last 24-48 hours. Use the web_search tool to find what actually happened "
    "recently across world news, politics, business and markets, science and "
    "technology, and culture and sport. Every question must be answerable from "
    "widely reported facts, unambiguous, and have exactly one correct option. "
    "Avoid speculation about unresolved events. Keep a neutral, non-partisan tone."
)

USER_PROMPT = """Today is {pretty}. Search the web for the most important and \
interesting news from roughly the last 48 hours, then write a 10-question \
multiple-choice quiz.

Return a single JSON object (and nothing else) with exactly this shape:
{{
  "intro": "one or two warm sentences setting up today's quiz and its themes",
  "questions": [
    {{
      "question": "the question text",
      "options": ["option one", "option two", "option three", "option four"],
      "answer_index": 0,
      "explanation": "one or two sentences explaining the answer with the key fact",
      "category": "World | Politics | Business | Science & Tech | Culture | Sport"
    }}
  ],
  "sources": ["short label of a key story or outlet you used"]
}}

Rules: exactly 10 questions; exactly 4 options each; answer_index is the 0-based \
index of the correct option; vary the categories; make the wrong options \
plausible; no duplicate questions. Output only the JSON object, optionally inside \
a ```json code fence."""


def generate_quiz(model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": USER_PROMPT.format(pretty=now_local().strftime("%A, %d %B %Y"))}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]

    resp = None
    for _ in range(6):  # allow a few pause_turn continuations of the server tool loop
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    if resp is None:
        raise RuntimeError("no response from the API")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = parse_quiz_json(text)
    validate_quiz(data)
    return data


def parse_quiz_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        blob = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("No JSON object found in model output:\n" + text[:500])
        blob = text[start : end + 1]
    return json.loads(blob)


def validate_quiz(q: dict) -> None:
    qs = q.get("questions")
    if not isinstance(qs, list) or not (8 <= len(qs) <= 12):
        raise ValueError(f"expected 8-12 questions, got {len(qs) if isinstance(qs, list) else 'none'}")
    for i, item in enumerate(qs, 1):
        opts = item.get("options")
        if not isinstance(opts, list) or len(opts) != 4:
            raise ValueError(f"question {i}: needs exactly 4 options")
        ai = item.get("answer_index")
        if not isinstance(ai, int) or not (0 <= ai < 4):
            raise ValueError(f"question {i}: answer_index must be 0-3")
        if not item.get("question") or not item.get("explanation"):
            raise ValueError(f"question {i}: missing question or explanation")
        item.setdefault("category", "news")
    q.setdefault("intro", "")
    q.setdefault("sources", [])


# --------------------------------------------------------------------------- #
# rendering — web quiz page
# --------------------------------------------------------------------------- #
LETTERS = ["A", "B", "C", "D"]


def render_web_quiz(quiz: dict, date_str: str, generated_at: str) -> str:
    cards = []
    for i, q in enumerate(quiz["questions"], 1):
        opts = "".join(
            '<button class="opt" data-correct="{c}">{t}</button>'.format(
                c="true" if j == q["answer_index"] else "false", t=esc(opt)
            )
            for j, opt in enumerate(q["options"])
        )
        cards.append(
            '<article class="card q" data-answered="false">'
            '<div class="qhead"><span class="pill">{cat}</span>'
            '<span class="qnum tnum">{n:02d}</span></div>'
            '<h3 class="qtext">{question}</h3>'
            '<div class="opts">{opts}</div>'
            '<p class="explain">{exp}</p></article>'.format(
                cat=esc(q["category"]),
                n=i,
                question=esc(q["question"]),
                opts=opts,
                exp=esc(q["explanation"]),
            )
        )

    sources = " · ".join(esc(s) for s in quiz.get("sources", [])) or "the morning papers"
    hero = (
        '<header class="hero grain"><div class="wrap">'
        '<div class="topbar">'
        '<div class="crumb">&#9749; <span>morning coffee</span> '
        '<span class="dot">·</span> <span class="tnum">{date}</span></div>'
        '<button id="theme-toggle" class="ghost">theme</button></div>'
        '<h1 class="headline">today&#39;s <span class="accent">ten</span>.</h1>'
        '<p class="lede">{intro}</p>'
        '<p class="scoreline" id="score" style="margin-top:16px"></p>'
        '<p style="margin-top:16px"><a href="../../index.html">&larr; all editions</a></p>'
        "</div></header>"
    ).format(date=esc(date_str), intro=esc(quiz["intro"]))

    body = (
        '<section class="body"><div class="wrap"><div class="stack">{cards}</div>'
        '<p class="foot" style="margin-top:28px">brewed from: {sources}</p>'
        "</div></section>"
        '<footer><div class="wrap"><p class="foot">'
        '{n} questions · generated {ts} · <a href="quiz.pdf">download pdf</a>'
        "</p></div></footer>"
    ).format(cards="".join(cards), sources=sources, n=len(quiz["questions"]), ts=esc(generated_at))

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Morning Coffee · {date}</title>"
        '<link rel="stylesheet" href="../../style.css"></head><body>'
        "{hero}{body}"
        '<script src="../../app.js"></script></body></html>'
    ).format(date=esc(date_str), hero=hero, body=body)


# --------------------------------------------------------------------------- #
# rendering — archive index
# --------------------------------------------------------------------------- #
def build_index(site_base: str) -> str:
    editions = []
    if NEWSLETTERS.exists():
        for d in sorted(NEWSLETTERS.iterdir(), reverse=True):
            meta = d / "quiz.json"
            if not (d.is_dir() and meta.exists()):
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
                '<span class="more">take the quiz &rarr;</span></a>'.format(
                    date=esc(d.name),
                    pretty=esc(rec.get("pretty", d.name)),
                    title=esc(rec.get("title", "Morning Coffee")),
                    intro=esc(intro),
                )
            )

    count = len(editions)
    grid = "".join(editions) if editions else '<p class="empty">No editions yet — check back tomorrow morning.</p>'

    hero = (
        '<header class="hero grain"><div class="wrap">'
        '<div class="topbar"><div class="crumb">&#9749; <span>morning coffee</span></div>'
        '<button id="theme-toggle" class="ghost">theme</button></div>'
        '<h1 class="headline">a quiz with your <span class="accent">coffee</span>, '
        "every morning.</h1>"
        '<p class="lede">Ten questions on the day&#39;s news — emailed at dawn and '
        'archived here. <span class="tnum">{count}</span> '
        "{ed} and counting.</p>"
        "</div></header>"
    ).format(count=count, ed="edition" if count == 1 else "editions")

    body = (
        '<section class="body"><div class="wrap">'
        '<div class="grid">{grid}</div></div></section>'
        '<footer><div class="wrap"><p class="foot">'
        "Morning Coffee · an automated daily news quiz · "
        '<a href="{site}">{site}</a></p></div></footer>'
    ).format(grid=grid, site=esc(site_base))

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Morning Coffee — daily news quiz</title>"
        '<link rel="stylesheet" href="style.css"></head><body>'
        "{hero}{body}"
        '<script src="app.js"></script></body></html>'
    ).format(hero=hero, body=body)


# --------------------------------------------------------------------------- #
# rendering — print / PDF (self-contained, answers shown)
# --------------------------------------------------------------------------- #
PRINT_STYLE = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #171716; line-height: 1.5; margin: 0; }
.crumb { font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: #73736e; }
h1 { font-size: 26px; letter-spacing: -0.02em; margin: 6px 0 10px; }
.lede { color: #57574f; font-size: 13px; margin: 0 0 22px; max-width: 60ch; }
.q { padding: 14px 0; border-top: 1px solid #e2e2e0; page-break-inside: avoid; }
.qhead { font-size: 10px; text-transform: lowercase; letter-spacing: 0.04em; color: #0d9488; }
.qnum { color: #a8a8a2; }
.qtext { font-size: 15px; font-weight: 600; margin: 4px 0 10px; }
.opt { font-size: 13px; padding: 4px 0 4px 22px; position: relative; color: #44443f; }
.opt .ltr { position: absolute; left: 0; color: #a8a8a2; }
.opt.correct { color: #0d6c63; font-weight: 600; }
.opt.correct .ltr { color: #0d9488; }
.exp { font-size: 12px; color: #73736e; margin: 8px 0 0; }
.foot { margin-top: 26px; padding-top: 12px; border-top: 1px solid #e2e2e0;
  font-size: 11px; color: #a8a8a2; }
"""


def render_print_html(quiz: dict, pretty: str) -> str:
    blocks = []
    for i, q in enumerate(quiz["questions"], 1):
        opts = "".join(
            '<div class="opt {cls}"><span class="ltr">{ltr}</span>{t}</div>'.format(
                cls="correct" if j == q["answer_index"] else "",
                ltr=LETTERS[j],
                t=esc(opt),
            )
            for j, opt in enumerate(q["options"])
        )
        blocks.append(
            '<div class="q"><div class="qhead">{cat} '
            '<span class="qnum">· {n:02d}</span></div>'
            '<div class="qtext">{question}</div>{opts}'
            '<div class="exp"><strong>Answer:</strong> {ltr}. {exp}</div></div>'.format(
                cat=esc(q["category"]),
                n=i,
                question=esc(q["question"]),
                opts=opts,
                ltr=LETTERS[q["answer_index"]],
                exp=esc(q["explanation"]),
            )
        )
    sources = " · ".join(esc(s) for s in quiz.get("sources", [])) or "the morning papers"
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>{style}</style></head>'
        '<body><div class="crumb">&#9749; morning coffee</div>'
        "<h1>Today&#39;s ten — {pretty}</h1>"
        '<p class="lede">{intro}</p>{blocks}'
        '<p class="foot">brewed from: {sources} · morning-coffee</p>'
        "</body></html>"
    ).format(
        style=PRINT_STYLE,
        pretty=esc(pretty),
        intro=esc(quiz["intro"]),
        blocks="".join(blocks),
        sources=sources,
    )


def render_pdf(html_str: str, out_path: Path) -> bool:
    """Render HTML to PDF with headless Chromium. Returns True on success."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover - only hit when playwright missing
        print(f"  ! playwright unavailable ({e}); skipping PDF", file=sys.stderr)
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_str, wait_until="load")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()
    return True


# --------------------------------------------------------------------------- #
# rendering — email (inline styles for client compatibility)
# --------------------------------------------------------------------------- #
def render_email_html(quiz: dict, date_str: str, pretty: str, site_url: str) -> str:
    BG, CARD, BORDER, FG, MUTED, ACCENT = "#FCFCFA", "#FFFFFF", "#E2E2E0", "#171716", "#73736E", "#0D9488"
    qrows, answers = [], []
    for i, q in enumerate(quiz["questions"], 1):
        opts = "".join(
            '<div style="font-size:14px;color:{fg};padding:2px 0">'
            '<span style="color:{muted}">{ltr}.</span> {t}</div>'.format(
                fg=FG, muted=MUTED, ltr=LETTERS[j], t=esc(opt)
            )
            for j, opt in enumerate(q["options"])
        )
        qrows.append(
            '<tr><td style="padding:16px 0;border-top:1px solid {b}">'
            '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.1em;'
            'color:{acc}">{cat} &middot; {n:02d}</div>'
            '<div style="font-size:16px;font-weight:600;color:{fg};margin:4px 0 8px">{question}</div>'
            "{opts}</td></tr>".format(
                b=BORDER, acc=ACCENT, cat=esc(q["category"]), n=i, fg=FG,
                question=esc(q["question"]), opts=opts,
            )
        )
        answers.append(
            '<div style="font-size:13px;color:{muted};padding:4px 0">'
            "<span style=\"color:{acc};font-weight:600\">{n:02d} &rarr; {ltr}.</span> "
            "{exp}</div>".format(
                muted=MUTED, acc=ACCENT, n=i, ltr=LETTERS[q["answer_index"]],
                exp=esc(q["explanation"]),
            )
        )

    edition_url = f"{site_url.rstrip('/')}/newsletters/{date_str}/index.html"
    return (
        '<body style="margin:0;background:{bg};font-family:-apple-system,Segoe UI,'
        'Roboto,Helvetica,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:{bg}"><tr><td align="center" style="padding:28px 16px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;background:{card};border:1px solid {b};border-radius:14px">'
        '<tr><td style="padding:28px 28px 8px">'
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.14em;'
        'color:{muted}">&#9749; morning coffee &middot; {hdate}</div>'
        '<div style="font-size:26px;font-weight:600;color:{fg};letter-spacing:-.02em;'
        'margin:8px 0 6px">Today&#39;s ten.</div>'
        '<div style="font-size:14px;color:{muted}">{intro}</div></td></tr>'
        '<tr><td style="padding:8px 28px"><table role="presentation" width="100%" '
        'cellpadding="0" cellspacing="0">{qrows}</table></td></tr>'
        '<tr><td style="padding:18px 28px">'
        '<a href="{edition}" style="display:inline-block;background:{acc};color:#fff;'
        'font-size:14px;font-weight:600;text-decoration:none;padding:10px 18px;'
        'border-radius:999px">Take it interactively &rarr;</a></td></tr>'
        '<tr><td style="padding:8px 28px 26px;border-top:1px solid {b}">'
        '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.1em;'
        'color:{muted};margin:14px 0 8px">answer key</div>{answers}'
        '<div style="font-size:11px;color:{muted};margin-top:18px">'
        "The full quiz (with answers) is attached as a PDF. Archive: "
        '<a href="{site}" style="color:{acc}">{site}</a></div>'
        "</td></tr></table></td></tr></table></body>"
    ).format(
        bg=BG, card=CARD, b=BORDER, fg=FG, muted=MUTED, acc=ACCENT,
        hdate=esc(pretty), intro=esc(quiz["intro"]), qrows="".join(qrows),
        edition=esc(edition_url), answers="".join(answers), site=esc(site_url),
    )


def render_email_text(quiz: dict, pretty: str, site_url: str) -> str:
    lines = [f"Morning Coffee — Today's ten ({pretty})", "", quiz["intro"], ""]
    for i, q in enumerate(quiz["questions"], 1):
        lines.append(f"{i:02d}. [{q['category']}] {q['question']}")
        for j, opt in enumerate(q["options"]):
            lines.append(f"    {LETTERS[j]}. {opt}")
        lines.append("")
    lines.append("— Answer key —")
    for i, q in enumerate(quiz["questions"], 1):
        lines.append(f"{i:02d} -> {LETTERS[q['answer_index']]}. {q['explanation']}")
    lines += ["", f"Archive: {site_url}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# email
# --------------------------------------------------------------------------- #
def send_email(date_str, html_body, text_body, pdf_path):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")  # Gmail shows it in groups of 4
    recipient = os.environ.get("RECIPIENT") or sender

    msg = EmailMessage()
    msg["Subject"] = f"☕ Morning Coffee Quiz — {date_str}"
    msg["From"] = f"Morning Coffee <{sender}>"
    msg["To"] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if pdf_path and pdf_path.exists():
        msg.add_attachment(
            pdf_path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=f"morning-coffee-{date_str}.pdf",
        )

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
    try:  # keep emoji/arrows from crashing legacy Windows consoles
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Generate the Morning Coffee quiz.")
    ap.add_argument("--sample", action="store_true", help="use sample_quiz.json; no API call, no email")
    ap.add_argument("--from-json", default=None, metavar="PATH",
                    help="render a quiz JSON produced elsewhere (e.g. by Claude Code); no API call")
    ap.add_argument("--no-email", action="store_true", help="generate and save, but do not send email")
    ap.add_argument("--model", default=None, help="override the Claude model id")
    ap.add_argument("--base-url", default=None, help="override the public site base URL")
    args = ap.parse_args()

    # `or` (not get's default) so an empty env var — common in CI when a
    # repository variable is undefined — still falls back to the default.
    model = args.model or os.environ.get("MODEL") or DEFAULT_MODEL
    site = args.base_url or os.environ.get("SITE_BASE_URL") or DEFAULT_SITE

    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    pretty = now.strftime("%A, %d %B %Y")
    generated_at = now.strftime("%Y-%m-%d %H:%M %Z").strip()

    if args.sample:
        print("• sample mode — loading sample_quiz.json")
        quiz = json.loads((ROOT / "sample_quiz.json").read_text(encoding="utf-8"))
        validate_quiz(quiz)
    elif args.from_json:
        print(f"• loading quiz from {args.from_json}")
        quiz = parse_quiz_json(Path(args.from_json).read_text(encoding="utf-8"))
        validate_quiz(quiz)
        print(f"  got {len(quiz['questions'])} questions")
    else:
        print(f"• generating quiz with {model} (web search)…")
        quiz = generate_quiz(model)
        print(f"  got {len(quiz['questions'])} questions")

    quiz["title"] = "Morning Coffee" + (" (sample)" if args.sample else "")
    quiz["date"] = date_str
    quiz["pretty"] = pretty
    quiz["generated_at"] = generated_at

    nl_dir = NEWSLETTERS / date_str
    nl_dir.mkdir(parents=True, exist_ok=True)

    (nl_dir / "quiz.json").write_text(json.dumps(quiz, indent=2, ensure_ascii=False), encoding="utf-8")
    (nl_dir / "index.html").write_text(render_web_quiz(quiz, date_str, generated_at), encoding="utf-8")
    print(f"  wrote {nl_dir.relative_to(ROOT)}/index.html")

    pdf_path = nl_dir / "quiz.pdf"
    ok = render_pdf(render_print_html(quiz, pretty), pdf_path)
    if ok:
        print(f"  wrote {pdf_path.relative_to(ROOT)}")
    elif not args.sample:
        raise SystemExit("PDF generation failed in live mode (Playwright/Chromium required)")

    (DOCS / "index.html").write_text(build_index(site), encoding="utf-8")
    print("  rebuilt docs/index.html")

    if args.sample or args.no_email:
        print("• skipping email")
    elif os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"):
        send_email(
            date_str,
            render_email_html(quiz, date_str, pretty, site),
            render_email_text(quiz, pretty, site),
            pdf_path if ok else None,
        )
    else:
        print("• GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — skipping email (site + PDF still built)")

    print(f"✓ done — {site}/newsletters/{date_str}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
