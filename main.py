"""
Bulk-unlikes posts/reels from Instagram's "Your activity"
likes page.

Flow per batch: enter select mode, click tiles until
Instagram's own "N selected" counter reaches BATCH_SIZE,
press Unlike in the bar, confirm in the dialog, wait,
refresh, repeat.

Everything is driven by what is actually on screen
(coordinates of thumbnails and text), not by Instagram's
DOM selectors, which change constantly. The "N selected"
counter is used as ground truth throughout.
"""

import os
import time

from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIG
# ============================================================

LIKES_URL = "https://www.instagram.com/your_activity/interactions/likes/"

PROFILE_DIR = Path(__file__).parent / "ig-profile"

# Instagram only loads ~27 items per selection session,
# so work in batches of what it actually gives us.
BATCH_SIZE = 27

# When True, only selects items and stops WITHOUT
# unliking — the Unlike buttons are never pressed.
DRY_RUN = False

# How many batches to run. None = keep going until no
# items are left.
MAX_BATCHES = None

# Small pauses so Instagram has time to update its UI.
CLICK_DELAY_MS = 100
SCROLL_WAIT_MS = 1000
AFTER_UNLIKE_MS = 5000

# Pause after each unliked batch, before refreshing the
# page for the next one.
BATCH_PAUSE_MS = 2500

# Give up looking for more items after this many scrolls
# that produce nothing new.
MAX_EMPTY_SCROLLS = 10


# JS snippet shared by several helpers. Finds the grid
# tiles (large images), which exist no matter the selection
# state of the page.
FIND_TILES_JS = """
    const findTiles = () => {
        const tiles = [];

        for (const el of document.querySelectorAll('img, div')) {
            const box = el.getBoundingClientRect();

            if (box.width < 100 || box.height < 100) {
                continue;
            }

            if (el.tagName === 'IMG') {
                tiles.push(el);
                continue;
            }

            const bg = getComputedStyle(el).backgroundImage;

            if (bg && bg !== 'none') {
                tiles.push(el);
            }
        }

        return tiles;
    };
"""


# ============================================================
# PAGE HELPERS
# ============================================================

def find_text_points(page, text):
    """
    Viewport center points of on-screen elements whose exact
    visible text is `text`. Works on text nodes, so a label
    split across styled spans is still found — unlike
    Playwright's exact text matching.
    """

    try:
        return page.evaluate(
            """(text) => {
                const points = [];
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT
                );

                while (walker.nextNode()) {
                    const node = walker.currentNode;

                    if (node.textContent.trim() !== text) {
                        continue;
                    }

                    const el = node.parentElement;

                    if (!el) {
                        continue;
                    }

                    const b = el.getBoundingClientRect();

                    if (
                        b.width < 1
                        || b.height < 1
                        || b.bottom < 0
                        || b.right < 0
                        || b.top > window.innerHeight
                        || b.left > window.innerWidth
                    ) {
                        continue;
                    }

                    const style = getComputedStyle(el);

                    if (
                        style.visibility === 'hidden'
                        || style.display === 'none'
                        || style.opacity === '0'
                    ) {
                        continue;
                    }

                    points.push({
                        x: b.x + b.width / 2,
                        y: b.y + b.height / 2,
                    });
                }

                return points;
            }""",
            text,
        ) or []
    except Exception:
        return []


def click_text(page, text):
    """
    Click the first on-screen element with this exact text.
    """

    points = find_text_points(page, text)

    if not points:
        return False

    page.mouse.click(points[0]["x"], points[0]["y"])

    return True


def get_selected_count(page):
    """
    Reads Instagram's "N selected" counter from the bar
    below the grid — the ground truth for whether our
    clicks are actually registering.
    """

    try:
        return page.evaluate(
            """() => {
                const m = document.body.innerText.match(
                    /(\\d+)\\s+selected/i
                );

                return m ? parseInt(m[1], 10) : null;
            }"""
        )
    except Exception:
        return None


# ============================================================
# TILES
# ============================================================

def list_tiles(page):
    """
    The tile thumbnails currently in the DOM, identified by
    their image URL.
    """

    try:
        tiles = page.evaluate(
            "() => {" + FIND_TILES_JS + """
                return findTiles()
                    .filter((el) =>
                        el.tagName === 'IMG' && el.src)
                    .map((el) => ({ id: el.src }));
            }""",
        )

        return tiles or []

    except Exception:
        return []


def center_tile(page, tile_id):
    """
    Bring one tile to the middle of the view and return its
    center point. In select mode the whole tile is the
    toggle — clicking its center selects it.
    """

    try:
        return page.evaluate(
            "(id) => {" + FIND_TILES_JS + """
                for (const el of findTiles()) {
                    if (el.tagName !== 'IMG' || el.src !== id) {
                        continue;
                    }

                    el.scrollIntoView({ block: 'center' });

                    const b = el.getBoundingClientRect();

                    return {
                        x: b.x + b.width / 2,
                        y: b.y + b.height / 2,
                    };
                }

                return null;
            }""",
            tile_id,
        )
    except Exception:
        return None


# ============================================================
# SCROLLING
#
# The grid scrolls inside its own section, not with the
# window, and Instagram's structure keeps changing — so
# several strategies are tried, each verified against the
# grid's actual content position.
# ============================================================

def grid_scroll_state(page):
    """
    Snapshot of the grid used to tell whether a scroll
    attempt actually moved the content. Positions are
    measured relative to the document, so scrolling just
    the window does not change these numbers.
    """

    try:
        return page.evaluate(
            "() => {" + FIND_TILES_JS + """
                const tiles = findTiles();
                const y = window.scrollY;

                const tops = tiles.map((el) => Math.round(
                    el.getBoundingClientRect().top + y
                ));

                return {
                    scrollY: Math.round(y),
                    count: tiles.length,
                    firstTop: tops.length ? tops[0] : null,
                    lastTop: tops.length
                        ? tops[tops.length - 1]
                        : null,
                };
            }""",
        )
    except Exception:
        return None


def grid_moved(before, after):
    if before is None or after is None:
        return True

    return (
        before["count"] != after["count"]
        or before["firstTop"] != after["firstTop"]
        or before["lastTop"] != after["lastTop"]
    )


def restore_window_scroll(page, before):
    """
    Undo any window scrolling a strategy caused, so the
    outer page stays where it was.
    """

    if before is None:
        return

    try:
        page.evaluate(
            "(y) => window.scrollTo(0, y)",
            before["scrollY"],
        )
    except Exception:
        pass


def hover_grid_tile(page):
    """
    Park the mouse over a tile that is on screen, so wheel
    events land inside the grid's section. Keeps a margin
    from the top/bottom to avoid sticky bars.
    """

    try:
        point = page.evaluate(
            "() => {" + FIND_TILES_JS + """
                for (const el of findTiles()) {
                    const b = el.getBoundingClientRect();
                    const x = b.x + b.width / 2;
                    const y = b.y + b.height / 2;

                    if (
                        x > 0
                        && x < window.innerWidth
                        && y > 100
                        && y < window.innerHeight - 100
                    ) {
                        return { x: x, y: y };
                    }
                }

                return null;
            }""",
        )
    except Exception:
        point = None

    if not point:
        return False

    page.mouse.move(point["x"], point["y"])

    return True


def scroll_items_section(page, delta):
    """
    Advance the grid so Instagram loads more items. Tries
    each strategy in turn until one verifiably moves the
    grid content; window-only scrolling is undone.
    """

    before = grid_scroll_state(page)

    def moved():
        page.wait_for_timeout(300)

        if grid_moved(before, grid_scroll_state(page)):
            restore_window_scroll(page, before)
            return True

        return False

    # 1. Real wheel with the mouse parked over a tile.

    if hover_grid_tile(page):
        page.mouse.wheel(0, delta)

        if moved():
            return

    # 2. Bring the last loaded tile into view — reaching
    #    the end of the list is what triggers Instagram's
    #    infinite scroll.

    try:
        page.evaluate(
            "() => {" + FIND_TILES_JS + """
                const tiles = findTiles();

                if (tiles.length) {
                    tiles[tiles.length - 1]
                        .scrollIntoView({ block: 'end' });
                }
            }""",
        )

        if moved():
            return

    except Exception:
        pass

    # 3. Directly scroll a tile's scrollable ancestor.

    page.evaluate(
        "(delta) => {" + FIND_TILES_JS + """
            for (const tile of findTiles()) {
                let el = tile.parentElement;

                while (
                    el
                    && el !== document.body
                    && el !== document.documentElement
                ) {
                    const prev = el.scrollTop;

                    el.scrollTop = prev + delta;

                    if (el.scrollTop > prev) {
                        return true;
                    }

                    el = el.parentElement;
                }
            }

            return false;
        }""",
        delta,
    )

    if moved():
        return

    # 4. Synthetic wheel event, in case the section is a
    #    JS-driven scroller rather than a native one.

    page.evaluate(
        "(delta) => {" + FIND_TILES_JS + """
            const tiles = findTiles();

            if (!tiles.length) {
                return false;
            }

            tiles[Math.floor(tiles.length / 2)]
                .dispatchEvent(new WheelEvent('wheel', {
                    deltaY: delta,
                    bubbles: true,
                    cancelable: true,
                }));

            return true;
        }""",
        delta,
    )

    if moved():
        return

    # Nothing moved the grid — Instagram is probably still
    # loading. Don't scroll the page; the empty-scroll
    # counter in select_batch decides when to give up.

    if not getattr(scroll_items_section, "warned", False):
        scroll_items_section.warned = True

        print(
            "\n(The grid did not move; waiting for "
            "Instagram to load more items...)"
        )


# ============================================================
# SELECTING
# ============================================================

def enter_select_mode(page):
    print("Looking for Select...")

    for _ in range(30):
        if click_text(page, "Select"):
            print("Entered selection mode.")
            page.wait_for_timeout(1000)
            return

        page.wait_for_timeout(250)

    raise RuntimeError('Could not find the "Select" button.')


def select_batch(page):
    """
    Click each tile (the whole tile is the toggle in select
    mode) and trust only Instagram's "N selected" counter
    to know whether the click registered.
    """

    selected = get_selected_count(page) or 0
    clicked = set()
    empty_scrolls = 0

    print(f"\nSelecting up to {BATCH_SIZE} items...\n")

    while selected < BATCH_SIZE:

        progressed = False

        for tile in list_tiles(page):

            if selected >= BATCH_SIZE:
                break

            if tile["id"] in clicked:
                continue

            # Never click the same tile twice — a second
            # click would DESELECT it.
            clicked.add(tile["id"])

            point = center_tile(page, tile["id"])

            if point is None:
                continue

            url_before = page.url

            page.mouse.click(point["x"], point["y"])

            page.wait_for_timeout(CLICK_DELAY_MS)

            # Safety: if the click opened the reel viewer
            # instead of toggling, back out immediately.
            if page.url != url_before:
                page.go_back()
                page.wait_for_timeout(1000)
                continue

            new_count = get_selected_count(page)

            if new_count is not None and new_count > selected:
                selected = new_count
                progressed = True

                print(
                    f"\rSelected: {selected}/{BATCH_SIZE}",
                    end="",
                    flush=True,
                )

        if selected >= BATCH_SIZE:
            break

        # Every known tile is handled -> scroll for more.

        if progressed:
            empty_scrolls = 0
            continue

        empty_scrolls += 1

        if empty_scrolls > MAX_EMPTY_SCROLLS:
            break

        scroll_items_section(page, 1400)

        page.wait_for_timeout(SCROLL_WAIT_MS)

    print()

    return selected


# ============================================================
# UNLIKING
# ============================================================

def js_click_unlike(page):
    """
    Fallback: click "Unlike" from inside the page, walking
    up from the text to the nearest clickable ancestor.
    Reaches handlers that a coordinate click might miss.
    """

    try:
        return page.evaluate(
            """() => {
                const matches = [];
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT
                );

                while (walker.nextNode()) {
                    const node = walker.currentNode;

                    if (node.textContent.trim() !== 'Unlike') {
                        continue;
                    }

                    if (node.parentElement) {
                        matches.push(node.parentElement);
                    }
                }

                const cx = window.innerWidth / 2;
                const cy = window.innerHeight / 2;

                let best = null;
                let bestDist = Infinity;

                for (const el of matches) {
                    const b = el.getBoundingClientRect();

                    if (b.width < 1 || b.height < 1) {
                        continue;
                    }

                    const d =
                        (b.x + b.width / 2 - cx) ** 2
                        + (b.y + b.height / 2 - cy) ** 2;

                    if (d < bestDist) {
                        bestDist = d;
                        best = el;
                    }
                }

                if (!best) {
                    return false;
                }

                let el = best;

                while (el && el !== document.body) {
                    if (
                        el.tagName === 'BUTTON'
                        || el.getAttribute('role') === 'button'
                        || el.onclick
                        || el.hasAttribute('tabindex')
                    ) {
                        el.click();
                        return true;
                    }

                    el = el.parentElement;
                }

                best.click();

                return true;
            }"""
        )
    except Exception:
        return False


def dump_debug(page, label):
    """
    Save a screenshot and the page HTML next to the script
    so a failure can be inspected instead of guessed at.
    """

    base = Path(__file__).parent

    png = base / f"debug_{label}.png"
    html = base / f"debug_{label}.html"

    try:
        page.screenshot(path=str(png))
        html.write_text(page.content())

        print(f"Saved {png.name} and {html.name}.")

    except Exception as error:
        print(f"Could not save debug files: {error}")


def bulk_unlike(page):
    """
    Press the bar's red Unlike, then the dialog's Unlike.
    State-driven: whatever "Unlike" is on screen gets
    clicked (the dialog's is the one nearest the screen
    center), until Instagram's "N selected" bar disappears
    — the only real proof the unlike went through.
    """

    print("Unliking the selected items...")

    viewport = page.viewport_size or {
        "width": 1280,
        "height": 720,
    }

    cx = viewport["width"] / 2
    cy = viewport["height"] / 2

    for attempt in range(1, 21):

        remaining = get_selected_count(page)

        if remaining is None or remaining == 0:
            print("Unlike confirmed.")
            page.wait_for_timeout(AFTER_UNLIKE_MS)
            return

        points = find_text_points(page, "Unlike")

        print(
            f"  attempt {attempt}: {remaining} selected, "
            f"{len(points)} Unlike button(s) on screen"
        )

        if not points:
            page.wait_for_timeout(1000)
            continue

        # Every third attempt, click from inside the page
        # instead of with the mouse.
        if attempt % 3 == 0:
            js_click_unlike(page)
        else:
            if len(points) == 1:
                # The bar's button (or the dialog's, if the
                # bar is gone) — either advances the flow.
                target = points[0]
            else:
                # The dialog's button is the one nearest
                # the middle of the screen.
                target = min(
                    points,
                    key=lambda p:
                        (p["x"] - cx) ** 2
                        + (p["y"] - cy) ** 2,
                )

            page.mouse.click(target["x"], target["y"])

        page.wait_for_timeout(1500)

    dump_debug(page, "unlike")

    raise RuntimeError(
        "Could not complete the unlike flow. Debug files "
        "were saved next to the script."
    )


# ============================================================
# REFRESH
# ============================================================

def refresh_likes_page(page):
    """
    Reload the likes page and wait for the grid to come
    back. NOTE: reloading clears any manual Sort & filter
    choice (e.g. Content type -> Reels).
    """

    print("Refreshing the likes page...")

    page.goto(LIKES_URL, wait_until="domcontentloaded")

    for _ in range(40):
        if list_tiles(page):
            page.wait_for_timeout(1000)
            return True

        page.wait_for_timeout(500)

    return False


# ============================================================
# MAIN
# ============================================================

def format_duration(seconds):
    seconds = int(seconds)

    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def main():

    with sync_playwright() as p:

        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )

        page = (
            context.pages[0]
            if context.pages
            else context.new_page()
        )

        page.goto(LIKES_URL, wait_until="domcontentloaded")

        print()
        print("=" * 60)
        print("INSTAGRAM BULK UNLIKER")
        print("=" * 60)

        print("\nIf this is your first run, log into Instagram.")

        print(
            "\nOptionally use Sort & filter (e.g. Content "
            "type -> Reels)."
        )

        print(
            "NOTE: the filter resets when the script "
            "refreshes between batches, so it only applies "
            "to the FIRST batch."
        )

        input(
            "\nWhen the Likes page is ready, "
            "press ENTER here..."
        )

        total_unliked = 0
        batch_number = 0
        interrupted = False
        start_time = time.monotonic()

        try:

            while True:

                if (
                    MAX_BATCHES is not None
                    and batch_number >= MAX_BATCHES
                ):
                    break

                batch_number += 1

                print()
                print("=" * 60)
                print(f"BATCH {batch_number}")
                print("=" * 60)

                enter_select_mode(page)

                selected = select_batch(page)

                if selected == 0:
                    print("\nNo selectable items found.")
                    print("Possibly finished.")
                    break

                print(f"\nSelected {selected} item(s).")

                if DRY_RUN:
                    print()
                    print("=" * 60)
                    print("DRY RUN COMPLETE")
                    print("=" * 60)

                    print(f"\nSelected: {selected}")
                    print("Nothing was unliked.")

                    print(
                        "\nLook at Instagram and confirm "
                        "the correct items are selected."
                    )

                    print(
                        "Refresh the Instagram page "
                        "to clear the selection."
                    )

                    input("\nPress ENTER to close...")

                    break

                bulk_unlike(page)

                total_unliked += selected

                print("\nBatch complete.")
                print(f"Total unliked this run: {total_unliked}")

                if (
                    MAX_BATCHES is None
                    or batch_number < MAX_BATCHES
                ):

                    seconds = BATCH_PAUSE_MS / 1000

                    print(
                        f"\nWaiting {seconds:.0f} seconds "
                        "before the next batch..."
                    )

                    page.wait_for_timeout(BATCH_PAUSE_MS)

                    if not refresh_likes_page(page):
                        print(
                            "\nNo items after refresh. "
                            "Possibly finished."
                        )
                        break

        except KeyboardInterrupt:
            interrupted = True
            print("\n\nStopped with Ctrl+C.")

        except PlaywrightTimeoutError as error:
            print("\nPlaywright timed out:")
            print(error)

        except Exception as error:
            print("\nBot stopped because of an error:")
            print(error)

        finally:
            elapsed = time.monotonic() - start_time

            print(f"\nTotal unliked this run: {total_unliked}")
            print(f"Time elapsed: {format_duration(elapsed)}")

            try:
                input("\nPress ENTER to close the browser...")
            except (KeyboardInterrupt, EOFError):
                pass

            if interrupted:
                # After Ctrl+C the Playwright connection is
                # left in a broken state — calling into it
                # (context.close) hangs forever. Exit the
                # process outright; the browser is killed
                # along with it.
                os._exit(0)

            context.close()
            


if __name__ == "__main__":
    main()
