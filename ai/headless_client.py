"""
Teraguchi headless client — connects to a Teraguchi server via WebSocket,
decodes H.264 video frames into PIL Images, and sends mouse/keyboard input.

No Qt dependency. Designed for use by the AI MCP server.
"""

import asyncio
import base64
import collections
import io
import json
import logging
import ssl
import threading
import time
from typing import List, Optional, Tuple

import websockets

from common.messages import (
    MsgType, ClientHelloMsg,
    FrameType, VideoCodec,
    decode_video_header, decode_jpeg_header,
    VIDEO_HEADER_SIZE, JPEG_HEADER_SIZE,
    AuthResponse, parse_message,
)

logger = logging.getLogger(__name__)


class HeadlessClient:
    """
    Headless WebSocket client for the Teraguchi protocol.

    Frame history size: last FRAME_HISTORY_SIZE decoded frames are kept in
    memory (ring buffer) so stream-watching tools can sample them without
    needing to decode on the fly.
    """

    FRAME_HISTORY_SIZE = 300   # ~5 min at 1 fps, ~10 s at 30 fps

    def __init__(self):
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._closing = False

        # Frame buffer — latest frame + ring buffer for stream watching
        self._frame_lock = threading.Lock()
        self._latest_frame = None                          # PIL.Image or None
        # (timestamp_ms: int, PIL.Image) — last FRAME_HISTORY_SIZE frames
        self._frame_history: collections.deque = collections.deque(
            maxlen=self.FRAME_HISTORY_SIZE
        )
        self._screen_width = 0
        self._screen_height = 0

        # Video decoder — lazy import so PIL/av failures are clear
        self._decoder_ctx = None
        self._decoder_codec = None

        # Connection params
        self._host = ""
        self._port = 0
        self._username = ""
        self._password = ""
        self._use_tls = True

        # Event: set once server_hello received (first usable state)
        self._ready = threading.Event()
        self._error: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────

    def connect(self, host: str, port: int, username: str, password: str,
                use_tls: bool = True, timeout: float = 15.0,
                auto_unlock: bool = True,
                ssh_user: str = "", ssh_key: str = "") -> bool:
        """
        Connect to a Teraguchi server.  Blocks until server_hello is received
        or *timeout* seconds elapse.  Returns True on success.

        auto_unlock: if True (default), automatically runs `loginctl
        unlock-session` via SSH when a lock screen is detected after connect.
        ssh_user: SSH username for unlock (defaults to the Teraguchi username).
        ssh_key: path to SSH private key (default: ~/.ssh/id_ed25519).
        """
        if self._thread and self._thread.is_alive():
            self.disconnect()

        self._closing = False
        self._ready.clear()
        self._error = None
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="teraguchi-ai-io",
        )
        self._thread.start()

        self._ready.wait(timeout=timeout)
        if self._error:
            raise ConnectionError(self._error)
        if not self._connected:
            return False

        # PAM mode starts a new Xorg session; capture pipeline needs ~5-8s
        # to start sending frames. Wait here so callers get a ready client.
        frame_deadline = time.time() + 15.0
        while time.time() < frame_deadline:
            with self._frame_lock:
                if self._latest_frame is not None:
                    break
            time.sleep(0.1)

        if auto_unlock and self._connected:
            self._auto_unlock_if_needed(
                ssh_user=ssh_user or username,
                ssh_key=ssh_key or "",
            )

        return self._connected

    def _auto_unlock_if_needed(self, ssh_user: str, ssh_key: str = "") -> None:
        """
        Detect a GNOME lock screen by checking average frame brightness.
        A lock screen is mostly dark background (~10-30 average brightness).
        If detected, SSH in and run `loginctl unlock-session` for all sessions.
        """
        import subprocess
        with self._frame_lock:
            frame = self._latest_frame

        if frame is None:
            return

        # Quick brightness check: lock screen = dark purple gradient, avg ~30-60
        # Desktop with content = much brighter. Threshold: <80 = likely locked.
        import struct
        small = frame.resize((64, 40))
        pixels = list(small.getdata())
        # pixels can be RGB or RGBA tuples, or ints for grayscale
        if pixels and isinstance(pixels[0], (tuple, list)):
            avg = sum(sum(p[:3]) / 3 for p in pixels) / len(pixels)
        else:
            avg = sum(pixels) / len(pixels)

        logger.info("Auto-unlock brightness check: avg=%.1f", avg)
        if avg >= 80:
            # Bright enough - not a lock screen
            return

        logger.info("Lock screen detected (avg brightness %.1f) - unlocking via SSH", avg)
        cmd = ["ssh",
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5"]
        if ssh_key:
            cmd += ["-i", ssh_key]
        cmd += [f"{ssh_user}@{self._host}",
                "for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
                "sudo loginctl unlock-session $s 2>/dev/null; done"]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            logger.info("loginctl unlock-session: rc=%d", result.returncode)
            # Give GNOME a moment to dismiss the lock screen
            time.sleep(2)
        except Exception as e:
            logger.warning("Auto-unlock SSH failed: %s", e)

    def disconnect(self):
        """Disconnect and stop the background thread."""
        self._closing = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def screen_size(self) -> tuple:
        """Returns (width, height) of the remote display."""
        return (self._screen_width, self._screen_height)

    def screenshot(self) -> Optional[str]:
        """
        Return the latest decoded frame as a base64-encoded PNG string,
        or None if no frame has been received yet.
        """
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._encode_frame(self._latest_frame)

    # ── Streaming / change-detection API ─────────────────────────

    def watch_frames(
        self,
        duration_ms: int = 2000,
        sample_every_ms: int = 500,
    ) -> List[Tuple[int, str]]:
        """
        Collect frames over *duration_ms* milliseconds, sampling one frame
        every *sample_every_ms* ms.  Returns a list of (timestamp_ms, base64_png).

        Frames come from the live ring buffer — no extra decoding needed.
        If the buffer has fewer frames than requested, waits up to duration_ms
        for new ones to arrive.
        """
        results: List[Tuple[int, str]] = []
        deadline = time.time() + duration_ms / 1000.0
        next_sample = time.time()

        while time.time() < deadline:
            now = time.time()
            if now >= next_sample:
                with self._frame_lock:
                    if self._frame_history:
                        ts, img = self._frame_history[-1]
                        results.append((ts, self._encode_frame(img)))
                next_sample = now + sample_every_ms / 1000.0
            time.sleep(0.01)

        return results

    def wait_for_change(
        self,
        timeout_ms: int = 10_000,
        sensitivity: float = 0.02,
    ) -> Optional[Tuple[int, str, float]]:
        """
        Block until a frame differs from the last captured frame by more than
        *sensitivity* (fraction of pixels that changed, 0.0–1.0).

        Returns (timestamp_ms, base64_png, diff_score) or None on timeout.

        sensitivity=0.01 triggers on tiny UI changes (cursor, clock tick).
        sensitivity=0.05 triggers only on significant scene changes.
        sensitivity=0.20 triggers only on major changes (new window, new scene).
        """
        # Grab reference frame
        with self._frame_lock:
            reference = self._latest_frame

        if reference is None:
            # Wait for first frame
            deadline = time.time() + timeout_ms / 1000.0
            while time.time() < deadline:
                time.sleep(0.05)
                with self._frame_lock:
                    if self._latest_frame is not None:
                        reference = self._latest_frame
                        break
            if reference is None:
                return None

        deadline = time.time() + timeout_ms / 1000.0
        ref_arr = self._to_array(reference)

        while time.time() < deadline:
            time.sleep(0.05)
            with self._frame_lock:
                if not self._frame_history:
                    continue
                ts, candidate = self._frame_history[-1]

            if candidate is reference:
                continue

            score = self._frame_diff(ref_arr, candidate)
            if score >= sensitivity:
                return ts, self._encode_frame(candidate), score

            # Update reference to avoid re-triggering on the same small change
            ref_arr = self._to_array(candidate)
            reference = candidate

        return None

    def latest_frame_history(self, count: int = 10) -> List[Tuple[int, str]]:
        """Return the last *count* frames from the ring buffer as (ts_ms, base64_png)."""
        with self._frame_lock:
            frames = list(self._frame_history)[-count:]
        return [(ts, self._encode_frame(img)) for ts, img in frames]

    # ── Frame helpers ─────────────────────────────────────────────

    @staticmethod
    def _encode_frame(img) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _to_array(img):
        """Convert PIL Image to uint8 numpy array, resized to a small diff resolution."""
        try:
            import numpy as np
            # Downscale for fast diff — 160x90 is plenty for change detection
            small = img.resize((160, 90)).convert("RGB")
            return np.asarray(small, dtype=np.float32)
        except Exception:
            return None

    @staticmethod
    def _frame_diff(ref_arr, candidate) -> float:
        """Return fraction of pixels that changed (0.0–1.0). 0 if numpy unavailable."""
        if ref_arr is None:
            return 0.0
        try:
            import numpy as np
            small = candidate.resize((160, 90)).convert("RGB")
            arr = np.asarray(small, dtype=np.float32)
            diff = np.abs(arr - ref_arr).mean(axis=2)  # per-pixel mean channel diff
            changed = (diff > 10).sum()                 # pixels changed by >10/255
            return float(changed) / diff.size
        except Exception:
            return 0.0

    def send_input(self, msg: dict):
        """Send a JSON input event (mouse/keyboard). Thread-safe."""
        if not self._connected or not self._ws:
            return
        data = json.dumps(msg)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.send(data), self._loop)

    def click(self, x: float, y: float, button: int = 1, double: bool = False):
        """Click at normalized coordinates (0.0–1.0). button: 1=left 2=mid 3=right."""
        self.send_input({"type": MsgType.MOUSE_MOVE, "x": x, "y": y})
        clicks = 2 if double else 1
        for _ in range(clicks):
            self.send_input({
                "type": MsgType.MOUSE_BUTTON,
                "button": button, "pressed": True, "x": x, "y": y,
            })
            time.sleep(0.03)
            self.send_input({
                "type": MsgType.MOUSE_BUTTON,
                "button": button, "pressed": False, "x": x, "y": y,
            })
            if double:
                time.sleep(0.05)

    def move_mouse(self, x: float, y: float):
        self.send_input({"type": MsgType.MOUSE_MOVE, "x": x, "y": y})

    def scroll(self, x: float, y: float, dx: int = 0, dy: int = -3):
        """Scroll at position. dy<0 = scroll up, dy>0 = scroll down."""
        self.send_input({
            "type": MsgType.MOUSE_SCROLL,
            "dx": dx, "dy": dy, "x": x, "y": y,
        })

    def send_key(self, qt_key: int, pressed: bool, modifiers: int = 0):
        """Send a raw Qt key event."""
        self.send_input({
            "type": MsgType.KEY_EVENT,
            "key": "", "scan_code": qt_key,
            "pressed": pressed, "modifiers": modifiers,
        })

    def type_text(self, text: str):
        """Type a string character by character."""
        from ai.keymap import char_to_qt_key, QT_MOD_SHIFT
        for ch in text:
            qt_key, needs_shift = char_to_qt_key(ch)
            if qt_key is None:
                continue
            mods = QT_MOD_SHIFT if needs_shift else 0
            self.send_key(qt_key, True, mods)
            time.sleep(0.02)
            self.send_key(qt_key, False, mods)
            time.sleep(0.01)

    def press_combo(self, combo: str):
        """
        Press a key combination such as "ctrl+c", "alt+F4", "enter", "escape".
        Modifier keys are pressed before the main key and released after.
        """
        from ai.keymap import parse_combo
        modifiers, main_key = parse_combo(combo)

        for mod_key in modifiers:
            self.send_key(mod_key, True)
            time.sleep(0.02)

        self.send_key(main_key, True)
        time.sleep(0.03)
        self.send_key(main_key, False)
        time.sleep(0.02)

        for mod_key in reversed(modifiers):
            self.send_key(mod_key, False)
            time.sleep(0.02)

    # ── Internal: event loop ───────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_receive())
        except Exception as e:
            if not self._closing:
                logger.error("Connection error: %s", e)
                self._error = str(e)
                self._ready.set()
        finally:
            self._connected = False
            # Cancel all pending tasks before closing to suppress
            # "Task was destroyed" and SSL teardown noise from websockets 16.x
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()
            self._loop = None

    async def _connect_and_receive(self):
        scheme = "wss" if self._use_tls else "ws"
        uri = f"{scheme}://{self._host}:{self._port}"
        logger.info("AI client connecting to %s", uri)

        ssl_context = None
        if self._use_tls:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(
            uri,
            max_size=50 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=30,
            ssl=ssl_context,
        ) as ws:
            self._ws = ws
            logger.info("AI client WebSocket connected")

            first_msg = await ws.recv()
            if isinstance(first_msg, str):
                msg = parse_message(first_msg)
                if msg.get("type") == MsgType.AUTH_REQUEST:
                    await self._handle_auth(ws, msg)
                elif msg.get("type") == MsgType.SERVER_HELLO:
                    self._handle_server_hello(msg)

            # Send client_hello BEFORE signalling ready, so the server
            # starts the capture pipeline and frames begin flowing immediately.
            hello = ClientHelloMsg(
                client_name="teraguchi-ai",
                screen_width=1920,
                screen_height=1080,
            )
            await ws.send(hello.to_json())
            await ws.send(json.dumps({"type": MsgType.REQUEST_FULL_FRAME}))

            self._connected = True
            self._ready.set()   # unblock connect() only after hello is sent

            async for message in ws:
                if self._closing:
                    break
                if isinstance(message, str):
                    self._handle_json(message)
                elif isinstance(message, bytes):
                    self._handle_binary(message)

    async def _handle_auth(self, ws, auth_msg: dict):
        auth_mode = auth_msg.get("auth_mode", "local")
        logger.info("AI client authenticating via %s as '%s'", auth_mode, self._username)

        resp = AuthResponse(
            method="pam" if auth_mode == "pam" else "password",
            username=self._username,
            credential=self._password,
            screen_width=1920,
            screen_height=1080,
        )
        await ws.send(resp.to_json())

        result_raw = await ws.recv()
        if isinstance(result_raw, str):
            result = parse_message(result_raw)
            if result.get("type") == MsgType.AUTH_RESULT:
                if not result.get("success", False):
                    raise ConnectionError(
                        f"Auth failed: {result.get('message', 'Invalid credentials')}"
                    )
                logger.info("AI client authenticated")

            # After auth_result, server immediately sends server_hello
            hello_raw = await ws.recv()
            if isinstance(hello_raw, str):
                hello = parse_message(hello_raw)
                if hello.get("type") == MsgType.SERVER_HELLO:
                    self._handle_server_hello(hello)

    def _handle_server_hello(self, msg: dict):
        self._screen_width = msg.get("screen_width", 1920)
        self._screen_height = msg.get("screen_height", 1080)
        logger.info("AI client server_hello: %dx%d", self._screen_width, self._screen_height)

    def _handle_json(self, data: str):
        try:
            msg = parse_message(data)
            mtype = msg.get("type")
            if mtype == MsgType.SERVER_HELLO:
                self._handle_server_hello(msg)
            elif mtype == MsgType.HEALTH_PING:
                pong = {"type": MsgType.HEALTH_PONG,
                        "timestamp": msg.get("timestamp", 0)}
                self.send_input(pong)
        except Exception as e:
            logger.debug("JSON parse error: %s", e)

    def _handle_binary(self, data: bytes):
        if not data:
            return
        frame_type = data[0]

        if frame_type in (FrameType.VIDEO_H264, FrameType.VIDEO_H265, FrameType.VIDEO_AV1):
            if len(data) < VIDEO_HEADER_SIZE:
                return
            ft, codec, chroma, flags, ts, mon, payload = decode_video_header(data)
            self._decode_video_frame(codec, payload)

        elif frame_type in (FrameType.VIDEO_FULL, FrameType.VIDEO_PARTIAL):
            if len(data) < JPEG_HEADER_SIZE:
                return
            ft, x, y, w, h, payload = decode_jpeg_header(data)
            self._decode_jpeg_frame(payload)

    def _decode_video_frame(self, codec: VideoCodec, payload: bytes):
        """Decode H.264/H.265/AV1 payload into a PIL Image using PyAV."""
        try:
            import av
            from PIL import Image

            codec_name = {
                VideoCodec.H264: "h264",
                VideoCodec.H265: "hevc",
                VideoCodec.AV1:  "av1",
            }.get(codec, "h264")

            if self._decoder_codec != codec_name:
                self._decoder_ctx = av.CodecContext.create(codec_name, "r")
                self._decoder_codec = codec_name

            packet = av.Packet(payload)
            for frame in self._decoder_ctx.decode(packet):
                img = Image.fromarray(frame.to_ndarray(format="rgb24"))
                self._store_frame(img)
                break

        except Exception as e:
            logger.debug("Video decode error: %s", e)

    def _decode_jpeg_frame(self, payload: bytes):
        """Decode a JPEG payload into a PIL Image."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(payload))
            img.load()
            self._store_frame(img.convert("RGB"))
        except Exception as e:
            logger.debug("JPEG decode error: %s", e)

    def _store_frame(self, img):
        """Push a decoded frame into the latest slot and the ring buffer."""
        ts = int(time.time() * 1000)
        with self._frame_lock:
            self._latest_frame = img
            self._frame_history.append((ts, img))
