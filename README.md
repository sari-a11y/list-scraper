# Event attendee-list scraper

A small, config-driven tool for pulling an event's attendee/registrant/RSVP list
into a CSV. Event platforms expose their lists in three different shapes, so the
scraper supports all three from one config format.

| Mode | When to use | How it works |
|------|-------------|--------------|
| `html` | The list is in the page's HTML source (a table or repeated cards). | `requests` + CSS selectors. Fast, no browser. |
| `json` | The page loads attendees from a backend JSON endpoint. | Hits the endpoint directly, maps JSON keys to columns, follows pagination. Most reliable when available. |
| `render` | The list is drawn by JavaScript / infinite scroll (Luma, Eventbrite, Meetup…). | Headless browser (Playwright) loads + scrolls the page, then the HTML is parsed with the same CSS selectors as `html`. |

## Before you start — scope

- Use this only on lists you're **allowed** to access. It does **not** bypass
  logins, paywalls, captchas, or access controls.
- It honours `robots.txt` by default. Override with `--ignore-robots` only when
  you know you're permitted to fetch the page.
- Attendee lists are personal data. Handle the CSV accordingly (don't
  redistribute, mind GDPR/CCPA, delete when done).

## Install

```bash
pip install -r requirements.txt
# Only if you need `render` mode for JS-heavy pages:
pip install playwright && playwright install chromium
```

## Step 1 — find the structure

If you don't yet know how the list is built, inspect the page:

```bash
python3 scrape.py --inspect "https://example.com/event/attendees"
```

This prints: HTML tables found, the most-repeated CSS classes (good candidate
`item_selector`s for cards/rows), any likely `/api/...attendees...` endpoints
referenced in the source, and whether the page embeds framework state
(`__NEXT_DATA__` etc.).

For `json` mode the surest route is your browser's **DevTools → Network → XHR**:
reload the attendee page and look for the request that returns the list as JSON.
Copy its URL (and, if it needs your session, the `Cookie`/`Authorization` header)
into the config.

## Step 2 — write a config

Configs are JSON. See the three worked examples in [`configs/`](configs/):

- `html-table.example.json`
- `json-api.example.json`
- `render-cards.example.json`

### Field mapping

Each entry under `"fields"` becomes one CSV column.

- **html / render** — value is a CSS selector applied *within* each item.
  Use the object form to grab an attribute instead of text:
  ```json
  "profile": { "selector": "a.profile", "attr": "href" }
  ```
  `href`/`src` values are auto-resolved to absolute URLs.
- **json** — value is a dotted `path` into each record. Supports nested keys and
  list indexing: `"company.name"`, `"socials[0].handle"`.
- Any field may add a `"regex"` to keep just part of the value (first capture
  group, else the whole match). Useful for pulling an email out of a blob.

### Key config fields

| Key | Modes | Meaning |
|-----|-------|---------|
| `mode` | all | `html` / `json` / `render` |
| `url` | all | Page or endpoint to fetch |
| `item_selector` | html, render | CSS selector matching each attendee row/card |
| `records_path` | json | Dotted path to the list inside the response (`""` = top-level array) |
| `page_param` | json | Query param to increment for pagination (e.g. `page`, `offset`) |
| `page_step`, `page_start`, `max_pages` | json | Pagination controls |
| `next_path` | json | Dotted path to a "next page" URL/cursor (alternative to `page_param`) |
| `scroll`, `max_scrolls`, `scroll_pause` | render | Auto-scroll to trigger lazy-loading |
| `wait_selector` | render | Wait until this selector appears before reading |
| `headers` | all | Extra request headers (e.g. `Cookie`, `Authorization`, `Accept`) |
| `delay` | all | Seconds between requests (be polite) |

## Step 3 — run

```bash
# basic
python3 scrape.py configs/my-event.json -o attendees.csv

# de-duplicate on one or more columns
python3 scrape.py configs/my-event.json -o attendees.csv --dedupe name,company

# cap the number of records (handy while tuning selectors)
python3 scrape.py configs/my-event.json --limit 20
```

Output is UTF-8 CSV with one row per attendee and a header row of your field names.

## Troubleshooting

- **0 items matched** — your `item_selector` is off. Re-run `--inspect` and try
  one of the repeated classes it lists, or test the selector in DevTools
  (`document.querySelectorAll("...")`).
- **`records_path found nothing`** — open the JSON response and check the exact
  key path to the array; remember arrays need `[0]`-style indexing.
- **Empty values for some columns** — the per-field selector/path is wrong for
  that column only; the others working means the item/record match is fine.
- **Page needs login** — grab the `Cookie` header from a logged-in browser
  session (DevTools → Network → request → Headers) and put it under `headers`.
  Don't commit real cookies to git.
- **render mode: nothing loads** — increase `max_scrolls`, set a `wait_selector`,
  or confirm Playwright's Chromium is installed.
