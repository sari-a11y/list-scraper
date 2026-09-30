#!/usr/bin/env python3
"""
Event attendee-list scraper.

Pulls a list of people (attendees, registrants, RSVPs, speakers, members...)
from a page you have legitimate access to, and writes it to CSV.

It supports three source shapes, because event platforms differ:

  * html    - the list is present in the page's HTML source. Parsed with
              CSS selectors (requests + BeautifulSoup). Fast, no browser.
  * json    - the page loads attendees from a backend JSON endpoint. Point
              the scraper at that endpoint and map JSON keys to columns.
              Most robust when available. Supports pagination.
  * render  - the list is drawn by JavaScript (Luma, Eventbrite, Meetup,
              infinite scroll...). A headless browser (Playwright) loads the
              page, optionally scrolls, and the resulting HTML is parsed with
              the same CSS selectors as `html` mode. Requires the optional
              Playwright dependency.

You describe the source in a small JSON config (see configs/*.example.json).
Run `--inspect URL` first if you don't know the structure yet; it fetches the
page and prints hints (tables, likely JSON endpoints, repeated card markup).

Scope / etiquette:
  * Only use this on lists you're allowed to access. It does NOT bypass logins,
    paywalls, captchas, or access controls.
  * It honours robots.txt by default (override with --ignore-robots only when
    you know you're permitted).
  * It rate-limits requests so it stays polite.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'requests'. Run: pip install -r requirements.txt")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'beautifulsoup4'. Run: pip install -r requirements.txt")


DEFAULT_UA = (
    "Mozilla/5.0 (compatible; attendee-list-scraper/1.0; "
    "+https://github.com/sari-a11y/list-scraper)"
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class FieldSpec:
    """How to extract one output column from one record."""
    name: str
    # For html/render mode: a CSS selector applied within the item element.
    selector: Optional[str] = None
    # Which part of the matched element to read: "text" (default) or an attribute
    # name like "href", "src", "title".
    attr: str = "text"
    # For json mode: a dotted path within the record, e.g. "user.name" or
    # "profile.social.twitter". Supports [i] list indexing, e.g. "emails[0]".
    path: Optional[str] = None
    # Optional regex; if set, the first capture group (or whole match) is kept.
    regex: Optional[str] = None

    @classmethod
    def from_dict(cls, name: str, d: Any) -> "FieldSpec":
        # Shorthand: "column": "css selector"  or  "column": "json.path"
        if isinstance(d, str):
            return cls(name=name, selector=d, path=d)
        return cls(
            name=name,
            selector=d.get("selector"),
            attr=d.get("attr", "text"),
            path=d.get("path"),
            regex=d.get("regex"),
        )


@dataclass
class Config:
    mode: str                      # "html" | "json" | "render"
    url: str
    fields: list[FieldSpec]
    # html/render: CSS selector matching each attendee item (row/card).
    item_selector: Optional[str] = None
    # json: dotted path to the list of records inside the response, e.g.
    # "data.attendees" or "" for a top-level array.
    records_path: str = ""
    # json pagination.
    page_param: Optional[str] = None       # query param name, e.g. "page" or "offset"
    page_start: int = 1
    page_step: int = 1
    max_pages: int = 50
    # json: dotted path to a "next page" cursor/url in the response (alt to page_param).
    next_path: Optional[str] = None
    # render: extra behaviour.
    scroll: bool = False
    scroll_pause: float = 1.0
    max_scrolls: int = 30
    wait_selector: Optional[str] = None    # wait until this selector appears
    # networking
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    delay: float = 1.0                     # seconds between requests

    @classmethod
    def load(cls, path: str) -> "Config":
        raw = json.loads(Path(path).read_text())
        fields = [FieldSpec.from_dict(k, v) for k, v in raw.get("fields", {}).items()]
        if not fields:
            sys.exit(f"Config {path}: no 'fields' defined.")
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in raw.items() if k in known and k != "fields"}
        return cls(fields=fields, **kwargs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def dig(obj: Any, path: str) -> Any:
    """Navigate a dotted path with optional [i] indexing. Returns None if absent."""
    if path == "" or path is None:
        return obj
    cur = obj
    for part in path.split("."):
        m = re.match(r"^([^\[]*)((?:\[\d+\])*)$", part)
        if not m:
            return None
        key, idx = m.group(1), m.group(2)
        if key:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        for i in re.findall(r"\[(\d+)\]", idx):
            if isinstance(cur, list) and int(i) < len(cur):
                cur = cur[int(i)]
            else:
                return None
        if cur is None:
            return None
    return cur


def apply_regex(value: Optional[str], pattern: Optional[str]) -> Optional[str]:
    if value is None or not pattern:
        return value
    m = re.search(pattern, value)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


def clean(text: Optional[str]) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def check_robots(url: str, user_agent: str) -> bool:
    """Return True if fetching `url` is allowed by robots.txt (fail-open on error)."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception:
        return True  # no robots / unreachable -> don't block
    return rp.can_fetch(user_agent, url)


# --------------------------------------------------------------------------- #
# Extractors
# --------------------------------------------------------------------------- #

def extract_from_element(el, fields: list[FieldSpec], base_url: str) -> dict[str, str]:
    row: dict[str, str] = {}
    for f in fields:
        value: Optional[str] = None
        target = el
        if f.selector:
            found = el.select_one(f.selector)
            target = found
        if target is not None:
            if f.attr == "text":
                value = target.get_text(" ", strip=True)
            else:
                value = target.get(f.attr)
                if value and f.attr in ("href", "src") and base_url:
                    value = urljoin(base_url, value)
        value = apply_regex(value, f.regex)
        row[f.name] = clean(value)
    return row


def extract_from_record(rec: Any, fields: list[FieldSpec]) -> dict[str, str]:
    row: dict[str, str] = {}
    for f in fields:
        value = dig(rec, f.path if f.path else f.name)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = apply_regex(None if value is None else str(value), f.regex)
        row[f.name] = clean(value)
    return row


# --------------------------------------------------------------------------- #
# Scrapers
# --------------------------------------------------------------------------- #

def scrape_html(cfg: Config, session: requests.Session, html: Optional[str] = None) -> list[dict]:
    if html is None:
        resp = session.get(cfg.url, params=cfg.params, timeout=30)
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "lxml")
    if not cfg.item_selector:
        sys.exit("html/render mode requires 'item_selector' in the config.")
    items = soup.select(cfg.item_selector)
    print(f"  matched {len(items)} item(s) with selector {cfg.item_selector!r}", file=sys.stderr)
    return [extract_from_element(el, cfg.fields, cfg.url) for el in items]


def scrape_json(cfg: Config, session: requests.Session) -> list[dict]:
    rows: list[dict] = []
    url = cfg.url
    params = dict(cfg.params)
    page = cfg.page_start
    for n in range(cfg.max_pages):
        if cfg.page_param:
            params[cfg.page_param] = str(page)
        resp = session.get(url, params=params if cfg.page_param or not cfg.next_path else None,
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = dig(data, cfg.records_path)
        if records is None:
            print(f"  records_path {cfg.records_path!r} found nothing on page {n+1}",
                  file=sys.stderr)
            break
        if isinstance(records, dict):
            records = list(records.values())
        batch = [extract_from_record(r, cfg.fields) for r in records]
        rows.extend(batch)
        print(f"  page {n+1}: +{len(batch)} record(s) (total {len(rows)})", file=sys.stderr)
        if not batch:
            break
        # advance
        if cfg.next_path:
            nxt = dig(data, cfg.next_path)
            if not nxt:
                break
            url = urljoin(url, str(nxt))
            params = {}
        elif cfg.page_param:
            page += cfg.page_step
        else:
            break  # single page
        time.sleep(cfg.delay)
    return rows


def scrape_render(cfg: Config, session: requests.Session) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "render mode needs Playwright:\n"
            "  pip install playwright && playwright install chromium"
        )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=cfg.headers.get("User-Agent", DEFAULT_UA))
        pg = ctx.new_page()
        pg.goto(cfg.url, wait_until="networkidle", timeout=60000)
        if cfg.wait_selector:
            try:
                pg.wait_for_selector(cfg.wait_selector, timeout=15000)
            except Exception:
                print(f"  wait_selector {cfg.wait_selector!r} never appeared", file=sys.stderr)
        if cfg.scroll:
            last = 0
            for _ in range(cfg.max_scrolls):
                pg.mouse.wheel(0, 20000)
                time.sleep(cfg.scroll_pause)
                height = pg.evaluate("document.body.scrollHeight")
                if height == last:
                    break
                last = height
        html = pg.content()
        browser.close()
    return scrape_html(cfg, session, html=html)


# --------------------------------------------------------------------------- #
# Inspect mode
# --------------------------------------------------------------------------- #

def inspect(url: str, session: requests.Session) -> None:
    print(f"Fetching {url} ...", file=sys.stderr)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    print("\n== Tables ==")
    for i, t in enumerate(soup.find_all("table")):
        rows = t.find_all("tr")
        print(f"  table[{i}]: {len(rows)} rows; headers: "
              f"{[clean(th.get_text()) for th in t.find_all('th')][:8]}")

    print("\n== Repeated card markup (candidate item selectors) ==")
    counts: dict[str, int] = {}
    for el in soup.find_all(True, class_=True):
        for cls in el.get("class", []):
            key = f"{el.name}.{cls}"
            counts[key] = counts.get(key, 0) + 1
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        if n >= 3:
            print(f"  {n:>4}x  {key}")

    print("\n== Likely data endpoints in page source ==")
    endpoint_re = re.compile(
        r"""["'](/[^"']*?(?:api|attendee|guest|rsvp|member|registrant|people|json)[^"']*)["']""",
        re.I,
    )
    seen: set[str] = set()
    for m in endpoint_re.finditer(html):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            print(f"  {urljoin(url, u)}")

    print("\n== Inline JSON blobs (Next.js / state) ==")
    for sid in ("__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__"):
        if sid in html:
            print(f"  found: {sid}  (attendees likely embedded; use json mode on the page's state)")

    print("\nTip: open the page in your browser's devtools > Network, filter XHR,"
          "\nand look for the request that returns the attendee list as JSON.")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def dedupe(rows: list[dict], keys: Optional[list[str]]) -> list[dict]:
    if not keys:
        return rows
    seen = set()
    out = []
    for r in rows:
        sig = tuple(r.get(k, "") for k in keys)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def write_csv(rows: list[dict], fieldnames: list[str], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_session(cfg_headers: dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_UA, "Accept": "*/*"})
    s.headers.update(cfg_headers)
    return s


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", help="Path to a JSON config describing the source.")
    ap.add_argument("--inspect", metavar="URL",
                    help="Fetch a URL and print structure hints, then exit.")
    ap.add_argument("-o", "--output", default="attendees.csv", help="CSV output path.")
    ap.add_argument("--dedupe", metavar="COLS",
                    help="Comma-separated columns to de-duplicate on (e.g. name,email).")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="Skip the robots.txt check (only if you're permitted).")
    ap.add_argument("--limit", type=int, help="Stop after N records.")
    args = ap.parse_args(argv)

    if args.inspect:
        session = build_session({})
        inspect(args.inspect, session)
        return 0

    if not args.config:
        ap.error("provide a config file, or use --inspect URL")

    cfg = Config.load(args.config)
    session = build_session(cfg.headers)

    ua = session.headers.get("User-Agent", DEFAULT_UA)
    if not args.ignore_robots and not check_robots(cfg.url, ua):
        sys.exit(
            f"robots.txt disallows fetching {cfg.url} for this user agent.\n"
            f"If you have permission to access this list, re-run with --ignore-robots."
        )

    print(f"Scraping ({cfg.mode}) {cfg.url}", file=sys.stderr)
    if cfg.mode == "html":
        rows = scrape_html(cfg, session)
    elif cfg.mode == "json":
        rows = scrape_json(cfg, session)
    elif cfg.mode == "render":
        rows = scrape_render(cfg, session)
    else:
        sys.exit(f"Unknown mode {cfg.mode!r} (expected html/json/render).")

    # drop fully-empty rows
    rows = [r for r in rows if any(v for v in r.values())]
    rows = dedupe(rows, args.dedupe.split(",") if args.dedupe else None)
    if args.limit:
        rows = rows[: args.limit]

    fieldnames = [f.name for f in cfg.fields]
    write_csv(rows, fieldnames, args.output)
    print(f"\nWrote {len(rows)} attendee(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
