"""
Teraguchi AI MCP Server

Exposes the Teraguchi remote desktop as MCP tools so AI agents (Cursor, Claude)
can take screenshots, move the mouse, click, type, and press key combinations.

Usage:
    python -m ai.mcp_server

Configure in ~/.cursor/mcp.json:
    "teraguchi": {
        "command": "/path/to/.venv-ai/bin/python",
        "args": ["-m", "ai.mcp_server"],
        "cwd": "/path/to/teragucci"
    }

Environment variables (optional, for pre-configured connections):
    TERAGUCHI_HOST      default host (e.g. "192.168.178.94")
    TERAGUCHI_PORT      default port (default: 4443)
    TERAGUCHI_USER      default username
    TERAGUCHI_PASS      default password
    TERAGUCHI_TLS       "1" to use TLS (default), "0" for plain ws://
"""

import json
import logging
import os
import sys
import time

# Add the project root to sys.path so `common/` and `ai/` are importable
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from fastmcp import FastMCP
from ai.headless_client import HeadlessClient

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

mcp = FastMCP("teraguchi")
_client: HeadlessClient = HeadlessClient()


# -- Connection tools ----------------------------------------------

@mcp.tool()
def connect(
    host: str = "",
    port: int = 4443,
    username: str = "",
    password: str = "",
    use_tls: bool = True,
) -> str:
    """
    Connect to a Teraguchi remote desktop server.

    Args:
        host:     Server IP or hostname. Falls back to TERAGUCHI_HOST env var.
        port:     Server port (default 4443).
        username: Login username. Falls back to TERAGUCHI_USER env var.
        password: Login password. Falls back to TERAGUCHI_PASS env var.
        use_tls:  Use wss:// (True, default) or ws:// (False).

    Returns a status message with the detected screen resolution.
    """
    h = host or os.environ.get("TERAGUCHI_HOST", "")
    p = port or int(os.environ.get("TERAGUCHI_PORT", "4443"))
    u = username or os.environ.get("TERAGUCHI_USER", "")
    pw = password or os.environ.get("TERAGUCHI_PASS", "")
    tls_env = os.environ.get("TERAGUCHI_TLS", "1")
    tls = use_tls if (host or username or password) else (tls_env != "0")

    if not h:
        return "Error: host is required (pass as argument or set TERAGUCHI_HOST)"
    if not u:
        return "Error: username is required (pass as argument or set TERAGUCHI_USER)"

    if _client.connected:
        _client.disconnect()

    try:
        _client.connect(h, p, u, pw, use_tls=tls, timeout=20.0)
    except ConnectionError as e:
        return f"Connection failed: {e}"

    w, h_res = _client.screen_size
    return f"Connected to {h}:{p} - screen {w}x{h_res}"


@mcp.tool()
def disconnect() -> str:
    """Disconnect from the Teraguchi server."""
    if not _client.connected:
        return "Not connected"
    _client.disconnect()
    return "Disconnected"


@mcp.tool()
def connection_status() -> str:
    """Return current connection status and screen resolution."""
    if not _client.connected:
        return "Not connected"
    w, h = _client.screen_size
    return f"Connected - screen {w}x{h}"


# -- Vision tools --------------------------------------------------

@mcp.tool()
def screenshot() -> str:
    """
    Capture the current remote desktop screen.

    Returns a base64-encoded PNG image of the full screen, or an error string
    if not connected or no frame has been received yet.

    After connecting, call this tool a couple of seconds later to ensure
    the first frame has been decoded.
    """
    if not _client.connected:
        return "Error: not connected - call connect() first"

    # Wait up to 5 seconds for the first frame
    for _ in range(50):
        data = _client.screenshot()
        if data:
            return data
        time.sleep(0.1)

    return "Error: no frame received yet - server may still be starting the session"


@mcp.tool()
def get_screen_size() -> dict:
    """Return the remote screen resolution as {width, height}."""
    if not _client.connected:
        return {"error": "not connected"}
    w, h = _client.screen_size
    return {"width": w, "height": h}


# -- Mouse tools ---------------------------------------------------

@mcp.tool()
def click(
    x: float,
    y: float,
    button: str = "left",
) -> str:
    """
    Click at a position on the remote screen.

    Args:
        x:      Horizontal position, normalized 0.0 (left) to 1.0 (right).
        y:      Vertical position, normalized 0.0 (top) to 1.0 (bottom).
        button: Which mouse button: "left" (default), "right", "middle".

    Returns "ok" or an error string.
    """
    if not _client.connected:
        return "Error: not connected"
    btn = {"left": 1, "middle": 2, "right": 3}.get(button.lower(), 1)
    _client.click(x, y, button=btn)
    return "ok"


@mcp.tool()
def double_click(x: float, y: float) -> str:
    """
    Double-click at a normalized position on the remote screen.

    Args:
        x: Horizontal position 0.0-1.0.
        y: Vertical position 0.0-1.0.
    """
    if not _client.connected:
        return "Error: not connected"
    _client.click(x, y, button=1, double=True)
    return "ok"


@mcp.tool()
def move_mouse(x: float, y: float) -> str:
    """
    Move the mouse cursor to a normalized position without clicking.

    Args:
        x: Horizontal position 0.0-1.0.
        y: Vertical position 0.0-1.0.
    """
    if not _client.connected:
        return "Error: not connected"
    _client.move_mouse(x, y)
    return "ok"


@mcp.tool()
def scroll(
    x: float,
    y: float,
    direction: str = "down",
    amount: int = 3,
) -> str:
    """
    Scroll the mouse wheel at a given position.

    Args:
        x:         Horizontal position 0.0-1.0.
        y:         Vertical position 0.0-1.0.
        direction: "up", "down", "left", or "right".
        amount:    Number of scroll steps (default 3).
    """
    if not _client.connected:
        return "Error: not connected"
    dx, dy = 0, 0
    if direction == "up":
        dy = -amount
    elif direction == "down":
        dy = amount
    elif direction == "left":
        dx = -amount
    elif direction == "right":
        dx = amount
    _client.scroll(x, y, dx=dx, dy=dy)
    return "ok"


# -- Keyboard tools ------------------------------------------------

@mcp.tool()
def type_text(text: str) -> str:
    """
    Type a string of text on the remote desktop, character by character.

    Supports printable ASCII, newline (\\n), and tab (\\t).
    For special keys or combinations use the key() tool instead.

    Args:
        text: The text to type.
    """
    if not _client.connected:
        return "Error: not connected"
    _client.type_text(text)
    return f"ok - typed {len(text)} characters"


@mcp.tool()
def key(combo: str) -> str:
    """
    Press a key or key combination on the remote desktop.

    Modifier keys are joined with '+'. Examples:
        "enter", "escape", "tab", "backspace", "delete"
        "ctrl+c", "ctrl+v", "ctrl+z", "ctrl+a"
        "ctrl+shift+t", "alt+F4"
        "F1" through "F12"
        "left", "right", "up", "down"
        "home", "end", "pageup", "pagedown"

    Args:
        combo: Key combination string (case-insensitive).
    """
    if not _client.connected:
        return "Error: not connected"
    try:
        _client.press_combo(combo)
        return f"ok - pressed {combo!r}"
    except ValueError as e:
        return f"Error: {e}"


# -- Stream watching tools -----------------------------------------

@mcp.tool()
def watch(
    duration_ms: int = 2000,
    sample_every_ms: int = 500,
) -> str:
    """
    Observe the remote screen for a period of time and return a sequence of frames.

    The result is a JSON array of objects:
        [{"ts": <unix_ms>, "frame": "<base64_png>"}, ...]

    Use this to understand what is happening on screen over time - for example
    to watch a video, track an animation, or verify that a UI transition completed.

    Args:
        duration_ms:     How long to observe, in milliseconds (default 2000 = 2s).
        sample_every_ms: Interval between captured frames (default 500ms = 2 fps).
                         Set to 100 for ~10 fps, or 33 for ~30 fps.
                         Warning: many frames at high fps = large response.

    Returns a JSON string (array).
    """
    if not _client.connected:
        return "Error: not connected"

    frames = _client.watch_frames(duration_ms=duration_ms, sample_every_ms=sample_every_ms)
    result = [{"ts": ts, "frame": b64} for ts, b64 in frames]
    return json.dumps(result)


@mcp.tool()
def wait_for_change(
    timeout_ms: int = 10_000,
    sensitivity: float = 0.02,
) -> str:
    """
    Block until something on the remote screen changes, then return the new frame.

    This is more efficient than polling screenshot() in a loop.
    The server watches the decoded frame stream internally and returns as soon
    as the pixel difference exceeds the sensitivity threshold.

    Args:
        timeout_ms:  Maximum time to wait in milliseconds (default 10 000 = 10s).
        sensitivity: Fraction of screen pixels that must change to trigger
                     (0.0-1.0, default 0.02 = 2%).
                     0.005 - cursor blink, clock update, subtle animations
                     0.02  - UI element appears, button click feedback
                     0.05  - window open/close, significant layout change
                     0.20  - full scene change, new application launched

    Returns a JSON object: {"ts": <unix_ms>, "diff": <0.0-1.0>, "frame": "<base64_png>"}
    or a timeout error string.
    """
    if not _client.connected:
        return "Error: not connected"

    result = _client.wait_for_change(timeout_ms=timeout_ms, sensitivity=sensitivity)
    if result is None:
        return f"Timeout: no change detected within {timeout_ms}ms (sensitivity={sensitivity})"

    ts, b64, diff = result
    return json.dumps({"ts": ts, "diff": round(diff, 4), "frame": b64})


@mcp.tool()
def frame_history(count: int = 5) -> str:
    """
    Return the last N frames from the live frame ring buffer.

    The ring buffer holds up to 300 frames. This lets you inspect what
    happened recently without needing to capture in real time.

    Args:
        count: Number of most recent frames to return (default 5, max 300).

    Returns a JSON array: [{"ts": <unix_ms>, "frame": "<base64_png>"}, ...]
    """
    if not _client.connected:
        return "Error: not connected"

    frames = _client.latest_frame_history(count=min(count, 300))
    result = [{"ts": ts, "frame": b64} for ts, b64 in frames]
    return json.dumps(result)


# -- Entry point ---------------------------------------------------

def main():
    # Pre-connect if all env vars are set
    host = os.environ.get("TERAGUCHI_HOST", "")
    user = os.environ.get("TERAGUCHI_USER", "")
    pw   = os.environ.get("TERAGUCHI_PASS", "")
    port = int(os.environ.get("TERAGUCHI_PORT", "4443"))
    tls  = os.environ.get("TERAGUCHI_TLS", "1") != "0"

    if host and user and pw:
        try:
            _client.connect(host, port, user, pw, use_tls=tls, timeout=15.0)
            w, h = _client.screen_size
            logger.warning("Pre-connected to %s:%d - %dx%d", host, port, w, h)
        except Exception as e:
            logger.warning("Pre-connect failed: %s - use connect() tool", e)

    mcp.run()


if __name__ == "__main__":
    main()
