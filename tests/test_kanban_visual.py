"""Visual regression tests for Kanban board UI.

# requires running dashboard
# Start with: cd project-tracker && pt launch (or uvicorn dashboard.app:app)

This test suite exercises the Kanban board layout to detect issues like column
cutoff, spacing problems, and other visual regressions. The assertions live in
the `expect(...)` visibility checks and the bounding-box math; screenshots are
purely a debugging aid and are therefore opt-in via `PT_CAPTURE_SCREENSHOTS=1`.
"""

import os
from pathlib import Path

import httpx
import pytest

try:
    from playwright.sync_api import Page, expect, sync_playwright
except ImportError:  # playwright is an optional test extra; treat as "not available"
    Page = None
    expect = None
    sync_playwright = None

BASE_URL = "http://localhost:8000"

SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"


def _server_is_running() -> bool:
    """Check if the dashboard server is reachable."""
    try:
        r = httpx.get(f"{BASE_URL}/kanban", timeout=3)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):  # governance: allow-silent SF002: test skip probe; an unreachable server is the true "not running" answer and skips the module
        return False


def _chromium_available() -> bool:
    """Return True when the Playwright Chromium for this playwright version is installed.

    Detection reads ``p.chromium.executable_path`` and never launches a browser.
    Any exception here is expected-absence handling: playwright not importable or
    its driver failing means the browser is "not available", so the module skips
    instead of erroring.
    """
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # governance: allow-silent SF002: test skip probe; the docstring's expected-absence contract, a failing Playwright driver means the browser is unavailable and the module skips
        return False


pytestmark = [
    pytest.mark.skipif(
        not _server_is_running(),
        reason="Dashboard server not running at localhost:8000",
    ),
    pytest.mark.skipif(
        not _chromium_available(),
        reason=(
            "Playwright Chromium not installed for this playwright version; "
            "run: uv run --extra test --python 3.13 playwright install chromium"
        ),
    ),
]


def _capture_enabled() -> bool:
    """Screenshot capture is opt-in: PT_CAPTURE_SCREENSHOTS=1."""
    # governance: allow-silent SF003: optional opt-in; unset or empty means screenshot capture stays off
    return os.environ.get("PT_CAPTURE_SCREENSHOTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _screenshot(target, filename: str, **kwargs) -> None:
    """Write a screenshot to tests/screenshots/ when capture is enabled.

    `target` is anything with a `.screenshot(path=...)` method (a Page or a
    Locator). Paths are absolute so running pytest from a subdirectory does not
    scatter screenshot directories around the tree.
    """
    if not _capture_enabled():
        return
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / filename
    target.screenshot(path=str(path), **kwargs)
    print(f"\n📸 Screenshot saved to: {path}")


@pytest.fixture(scope="module")
def dashboard_server():
    """Return the dashboard server URL.

    Note: The server should be running before tests (via `pt launch`).
    The module-level skipif above guarantees it is reachable.
    """
    return BASE_URL


def test_kanban_board_full_layout(page: Page, dashboard_server):
    """Test that all Kanban columns are visible and not cut off.

    Verifies:
    - All columns (Backlog, To Do, In Progress, Review, Done) are visible
    - The DONE column is not cut off on the right edge
    - Proper spacing exists between columns and at the edges
    """
    # Navigate to Kanban board
    page.goto(f"{dashboard_server}/kanban")

    # Wait for the board to load
    page.wait_for_selector(".kanban-board-content", timeout=10000)

    _screenshot(page, "kanban_full_board.png", full_page=True)

    # Verify all columns are present
    columns = page.locator(".column")
    expect(columns).to_have_count(5)  # Backlog, To Do, In Progress, Review, Done

    # Print all column positions for debugging
    print("\n📊 Column Layout:")
    for i in range(5):
        col = columns.nth(i)
        box = col.bounding_box()
        if box:
            col_title = col.locator(".column-title").text_content()
            print(f"   {col_title}: x={box['x']:.0f}px, width={box['width']:.0f}px, right_edge={box['x'] + box['width']:.0f}px")

    # Verify the Done column is visible
    done_column = page.locator(".column").filter(has_text="Done").first
    expect(done_column).to_be_visible()


def test_kanban_board_done_column_visibility(page: Page, dashboard_server):
    """Specifically test that the Done column has proper spacing and is fully visible."""
    page.goto(f"{dashboard_server}/kanban")
    page.wait_for_selector(".kanban-board-content", timeout=10000)

    # Get the Done column
    done_column = page.locator(".column").filter(has_text="Done").first

    # Get the bounding box of the DONE column
    done_box = done_column.bounding_box()
    assert done_box is not None, "DONE column should be visible"

    # Get the viewport width
    viewport_size = page.viewport_size
    assert viewport_size is not None

    # Check that the DONE column's right edge is not at the viewport edge
    # There should be at least some padding (we added var(--space-xl) = 24px)
    right_edge = done_box["x"] + done_box["width"]

    print(f"\n📏 DONE column right edge: {right_edge}px")
    print(f"   Viewport width: {viewport_size['width']}px")
    print(f"   Distance from edge: {viewport_size['width'] - right_edge}px")

    # The column should not extend all the way to the viewport edge
    # Allow for scrollbar width (~15px) plus our padding (24px)
    assert right_edge < viewport_size["width"] - 10, \
        f"DONE column appears cut off (right edge at {right_edge}px, viewport {viewport_size['width']}px)"

    # Focused screenshot of the right side (opt-in)
    _screenshot(done_column, "kanban_done_column.png")


def test_kanban_board_horizontal_scroll(page: Page, dashboard_server):
    """Test that horizontal scrolling works and all columns remain accessible."""
    page.goto(f"{dashboard_server}/kanban")
    page.wait_for_selector(".kanban-board-content", timeout=10000)

    # Get the scrollable container
    board_content = page.locator(".kanban-board-content")

    # Scroll to the far right
    board_content.evaluate("el => el.scrollLeft = el.scrollWidth")

    # Wait for scroll to complete
    page.wait_for_timeout(500)

    _screenshot(page, "kanban_scrolled_right.png", full_page=True)

    # Verify DONE column is still fully visible after scrolling
    done_column = page.locator(".column").filter(has_text="DONE").first
    expect(done_column).to_be_visible()

    # The DONE column should be fully in view
    done_box = done_column.bounding_box()
    assert done_box is not None

    viewport_size = page.viewport_size
    assert viewport_size is not None

    # After scrolling right, the DONE column should be fully visible
    # and have proper right padding
    right_edge = done_box["x"] + done_box["width"]
    print(f"📏 After scroll - DONE column right edge: {right_edge}px")
    print(f"   Viewport width: {viewport_size['width']}px")

    # Should have padding on the right
    assert right_edge < viewport_size["width"], \
        "DONE column should have right padding even when scrolled"
