# instapurge

Bulk-unlikes posts and reels from Instagram's **Your activity → Likes**
page using a visible Chromium browser driven by Playwright.

Works in batches: select up to 27 items (the most Instagram loads per
selection session), press Unlike, confirm, refresh, repeat until done.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

## Usage

```bash
python main.py
```

Log into Instagram in the browser window on the first run (saved to
`ig-profile/`), then press ENTER in the terminal to start. Stop any
time with Ctrl+C.

A **Sort & filter** choice (e.g. Content type → Reels) only applies to
the first batch — the between-batch refresh resets it.

## Config

Constants at the top of `main.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `DRY_RUN` | `False` | `True` = select only, never unlike. Test with this first. |
| `MAX_BATCHES` | `None` | Batches to run. `None` = until done; `1` = single-batch test. |
| `BATCH_SIZE` | `27` | Items per batch (Instagram's per-session load limit). |
| `BATCH_PAUSE_MS` | `2500` | Pause between batches. Raise it if worried about rate limits. |

## Caveats

- Unliking is permanent. Test with `DRY_RUN` / `MAX_BATCHES = 1` first.
- Automation is against Instagram's ToS — use at your own risk.
- If the unlike flow fails, `debug_unlike.png` / `.html` are saved next
  to the script for inspection.
