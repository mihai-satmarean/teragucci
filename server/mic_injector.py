"""
Microphone injection into PulseAudio for Teraguchi server.

Receives raw PCM s16le audio from the client and injects it into a virtual
PulseAudio null sink.  Apps running on the server can then select
"teraguchi_mic.monitor" as their microphone source.

Architecture
------------
  Client PCM  →  WebSocket binary (MIC frame)
              →  server/main.py handle_mic_frame()
              →  MicInjector.write(pcm)
              →  pacat stdin → PA null-sink "teraguchi_mic"
              →  Apps see "teraguchi_mic.monitor" as a mic source

The null-sink approach is chosen over module-pipe-source because pacat
handles all backpressure and format negotiation automatically.
"""

import logging
import os
import queue
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_SINK_NAME = "teraguchi_mic"
_SAMPLE_RATE = 48_000
_CHANNELS = 1
_FORMAT = "s16le"


class MicInjector:
    """
    Feeds client microphone PCM into a PulseAudio virtual null sink.

    Start/stop are idempotent.  write() is thread-safe and non-blocking
    (frames are dropped rather than blocked when the queue is full).
    """

    def __init__(self, uid: int, gid: int, pa_socket: Optional[str] = None):
        self._uid = uid
        self._gid = gid
        # PulseAudio socket path for the user's session
        self._pa_server = pa_socket or f"unix:/run/user/{uid}/pulse/native"
        self._module_idx: Optional[str] = None
        self._pacat: Optional[subprocess.Popen] = None
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        self._thread: Optional[threading.Thread] = None
        self._started = False

    # ── Lifecycle ────────────────────────────────

    def start(self) -> bool:
        """Load the PA null sink and start pacat.  Returns True on success."""
        if self._started:
            return True
        try:
            self._load_null_sink()
            self._start_pacat()
            self._thread = threading.Thread(
                target=self._writer_loop, name="mic-injector", daemon=True)
            self._thread.start()
            self._started = True
            logger.info("MicInjector: virtual mic '%s' ready (uid=%d)", _SINK_NAME, self._uid)
            return True
        except Exception as exc:
            logger.warning("MicInjector: startup failed: %s", exc)
            self._cleanup_resources()
            return False

    def stop(self):
        if not self._started:
            return
        self._started = False
        # Unblock writer thread
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._cleanup_resources()
        logger.info("MicInjector: stopped")

    # ── Data ─────────────────────────────────────

    def write(self, pcm_data: bytes):
        """Enqueue a PCM chunk for injection.  Non-blocking — drops if full."""
        if not self._started:
            return
        try:
            self._queue.put_nowait(pcm_data)
        except queue.Full:
            pass

    # ── Internal ─────────────────────────────────

    def _pactl(self, *args, timeout: int = 5) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "XDG_RUNTIME_DIR": f"/run/user/{self._uid}",
            "PULSE_RUNTIME_PATH": f"/run/user/{self._uid}/pulse",
            "HOME": f"/home/{self._get_username()}",
        }
        uid, gid = self._uid, self._gid

        def _drop():
            os.setgid(gid)
            os.setuid(uid)

        return subprocess.run(
            ["pactl", "--server", self._pa_server, *args],
            capture_output=True, text=True,
            timeout=timeout,
            env=env,
            preexec_fn=_drop,
        )

    def _get_username(self) -> str:
        try:
            import pwd
            return pwd.getpwuid(self._uid).pw_name
        except Exception:
            return "user"

    def _find_existing_module(self) -> Optional[str]:
        """Return module index if teraguchi_mic sink already exists."""
        result = self._pactl("list", "short", "modules")
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if "module-null-sink" in line and _SINK_NAME in line:
                return line.split()[0]
        return None

    def _load_null_sink(self):
        """Create the PulseAudio/PipeWire null sink (idempotent).

        Note: sink_properties with spaces breaks PipeWire-Pulse argument
        parsing when passed via subprocess list args, so we omit it.
        We unload only by stored index — never 'unload-module module-null-sink'
        which would nuke all null-sinks in the system.
        """
        existing = self._find_existing_module()
        if existing:
            logger.debug("MicInjector: reusing existing null-sink module idx=%s", existing)
            self._module_idx = existing
            return

        result = self._pactl(
            "load-module", "module-null-sink",
            f"sink_name={_SINK_NAME}",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pactl load-module module-null-sink failed: {result.stderr.strip()}"
            )
        self._module_idx = result.stdout.strip()
        logger.debug("MicInjector: null-sink module idx=%s", self._module_idx)

    def _start_pacat(self):
        """Start pacat to feed PCM from stdin into the null sink."""
        uid, gid = self._uid, self._gid
        env = {
            **os.environ,
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "PULSE_RUNTIME_PATH": f"/run/user/{uid}/pulse",
            "HOME": f"/home/{self._get_username()}",
        }

        def _drop():
            os.setgid(gid)
            os.setuid(uid)

        self._pacat = subprocess.Popen(
            [
                "pacat", "--playback",
                f"--server={self._pa_server}",
                f"--device={_SINK_NAME}",
                f"--format={_FORMAT}",
                f"--rate={_SAMPLE_RATE}",
                f"--channels={_CHANNELS}",
                "--raw",
                "--latency-msec=50",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            preexec_fn=_drop,
        )
        logger.debug("MicInjector: pacat pid=%d", self._pacat.pid)

    def _writer_loop(self):
        """Drain the queue and write chunks to pacat stdin."""
        while self._started:
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break
            if not self._pacat or self._pacat.poll() is not None:
                logger.warning("MicInjector: pacat exited — stopping injection")
                break
            try:
                self._pacat.stdin.write(chunk)
                self._pacat.stdin.flush()
            except (BrokenPipeError, OSError):
                logger.warning("MicInjector: pacat pipe broken")
                break

    def _cleanup_resources(self):
        if self._pacat:
            try:
                if self._pacat.stdin:
                    self._pacat.stdin.close()
                self._pacat.terminate()
                self._pacat.wait(timeout=2.0)
            except Exception:
                pass
            self._pacat = None

        if self._module_idx:
            try:
                self._pactl("unload-module", self._module_idx)
            except Exception:
                pass
            self._module_idx = None
