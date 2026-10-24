"""
USB camera detection and image capture for the overhead game-board view.

On macOS the built-in FaceTime / Continuity camera is at index 0.
We scan system_profiler to identify USB cameras and map them to
OpenCV indices so we never accidentally grab the built-in camera.
"""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import cv2


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

BUILTIN_KEYWORDS = ("facetime", "apple", "isight", "continuity")

# USB Vendor IDs known to be external cameras (not Apple built-in)
# EMEET SmartCam C960: VID=0x328f
KNOWN_USB_CAMERA_VIDS = {0x328F}


def _get_system_cameras() -> list[dict]:
    """Return camera dicts from system_profiler (macOS only)."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        return data.get("SPCameraDataType", [])
    except Exception:
        return []


def _is_builtin(cam: dict) -> bool:
    name = (cam.get("_name", "") + cam.get("spcamera_manufacturer", "")).lower()
    return any(kw in name for kw in BUILTIN_KEYWORDS)


def _get_usb_camera_uids() -> set[str]:
    """
    Return a set of UID strings for cameras that are definitely USB
    (i.e. vendor ID appears in KNOWN_USB_CAMERA_VIDS or name doesn't match
    built-in keywords).
    """
    cams = _get_system_cameras()
    usb_uids: set[str] = set()
    for cam in cams:
        if _is_builtin(cam):
            continue
        uid = cam.get("spcamera_uid", "")
        if uid:
            usb_uids.add(uid)
        else:
            # No UID means it may still be the external cam — mark by name
            usb_uids.add(cam.get("_name", ""))
    return usb_uids


def find_usb_camera_index(preferred: int | None = None) -> int | None:
    """
    Return the OpenCV video capture index for the USB game-board camera
    (EMEET SmartCam C960 or any other non-Apple camera).

    Strategy on macOS:
      - Built-in FaceTime/Continuity camera is always at index 0.
      - USB cameras appear at index 1+ in the order the OS enumerates them.
      - We start scanning from index 1 to avoid the built-in camera.
    """
    if preferred is not None:
        cap = cv2.VideoCapture(preferred)
        if cap.isOpened():
            cap.release()
            return preferred
        cap.release()

    # Start from 1 (macOS puts built-in at 0)
    for idx in range(1, 10):
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            cap.release()
            return idx
        cap.release()

    # Last resort: index 0 — but never the built-in FaceTime camera, which would
    # silently film the operator instead of the field.
    cams = _get_system_cameras()
    if cams and all(_is_builtin(c) for c in cams):
        return None
    cap = cv2.VideoCapture(0)
    if cap.isOpened():
        cap.release()
        return 0

    return None


def shrink_jpeg_b64(b64: str, max_side: int = 1280, quality: int = 80) -> str:
    """Downscale a base64 JPEG so its longest side is <= max_side.

    Raw 1080p frames are ~280 KB each; ~90 of them in one prompt would exceed the
    model APIs' request-size limits."""
    import numpy as np
    img = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return b64
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode() if ok else b64


# ---------------------------------------------------------------------------
# Capture class
# ---------------------------------------------------------------------------

class CameraCapture:
    """
    Captures JPEG frames from the USB camera at a given FPS.

    Frames are written to a temporary directory during a run.
    After the run, call `collect_and_encode()` to get sorted base64 frames,
    then `cleanup()` to delete the temp directory.
    """

    def __init__(
        self,
        cfg: dict,
        log: Callable[[str], None] | None = None,
    ):
        cam_cfg = cfg["camera"]
        self._preferred_idx: int | None = cam_cfg.get("index")
        # The overhead camera is identified by resolution, not index: macOS camera
        # numbering changes when devices are re-plugged (the EMEET has been both 0 and
        # 1), and the built-in FaceTime camera can't deliver 1920×1080.
        self._req_w: int = int(cam_cfg.get("width", 1920))
        self._req_h: int = int(cam_cfg.get("height", 1080))
        self._capture_fps: float = cam_cfg.get("capture_fps", 1)
        self._preview_fps: float = cam_cfg.get("preview_fps", 5)
        self._log = log or print

        self._idx: int | None = None
        self._cap: cv2.VideoCapture | None = None
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._tmp_path: Path | None = None
        self._running = False
        self._capture_task: asyncio.Task | None = None
        self._grab_future: asyncio.Future | None = None
        self._frame_count = 0
        self._latest_frame_path: Path | None = None
        self._last_reopen_time: float = 0.0
        # Called (from a worker thread) once when the camera stops delivering frames
        # and can't be reopened — e.g. after Mac sleep, which this process can't recover from.
        self.on_failure: Callable[[], None] | None = None
        self._failure_reported = False
        self._lock = __import__('threading').Lock()

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def _open_verified(self, idx: int) -> tuple[cv2.VideoCapture | None, str]:
        """Open camera `idx` at the required resolution. Returns (cap, description);
        cap is None (and released) unless frames really come at >= width×height."""
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            cap.release()
            return None, "not available"
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._req_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._req_h)
        ok, frame = False, None
        for _ in range(5):   # macOS AVFoundation needs a few frames to warm up
            ok, frame = cap.read()
            time.sleep(0.05)
        if not ok or frame is None:
            cap.release()
            return None, "no frames"
        h, w = frame.shape[:2]
        if w < self._req_w or h < self._req_h:
            cap.release()
            return None, f"{w}×{h} (not the overhead camera)"
        return cap, f"{w}×{h}"

    def open(self, index: int | None = None) -> bool:
        """Open the overhead camera. With `index`, only that camera is tried (an
        explicit choice in the UI); otherwise the configured index, then 0-5.
        Any camera below the required resolution is rejected, and the current camera
        stays open until a replacement has been verified. Returns True on success."""
        if index is not None and index == self._idx and self._cap is not None:
            return True   # already the (verified) camera in use
        candidates = [index] if index is not None else list(dict.fromkeys(
            ([self._preferred_idx] if self._preferred_idx is not None else []) + list(range(6))))
        tried = []
        for idx in candidates:
            if idx == self._idx and self._cap is not None:
                return True
            cap, desc = self._open_verified(idx)
            if cap is None:
                if desc != "not available":
                    tried.append(f"camera {idx}: {desc}")
                continue
            with self._lock:   # the preview stream reads _cap from other threads
                old, self._cap, self._idx = self._cap, cap, idx
                self._last_reopen_time = time.monotonic()
            if old is not None:
                old.release()
            self._failure_reported = False
            self._log(f"[camera] Opened overhead camera at index {idx} ({desc}).")
            return True
        found = "; ".join(tried) or "no cameras found"
        if self._cap is not None:
            self._log(f"[camera] Camera {index} not used ({found}); keeping camera {self._idx}.")
        else:
            self._log(f"[camera] ERROR: no camera delivers {self._req_w}×{self._req_h} ({found}). "
                      f"Plug in the overhead camera — if it was plugged in after the server "
                      f"started, restart the server.")
        return False

    def close(self) -> None:
        with self._lock:
            if self._cap:
                self._cap.release()
                self._cap = None
            self._idx = None

    # ------------------------------------------------------------------ #
    #  Run-time capture                                                    #
    # ------------------------------------------------------------------ #

    async def start_capture(self, on_frame=None) -> None:
        """Start writing frames to a new temp directory.
        on_frame(b64: str, frame_num: int) is called after each captured frame."""
        self._tmpdir = tempfile.TemporaryDirectory(prefix="vex_aim_run_")
        self._tmp_path = Path(self._tmpdir.name)
        self._frame_count = 0
        self._frame_times: list[float] = []   # seconds from capture start, per saved frame
        self._capture_t0 = time.monotonic()
        self._running = True
        self._on_frame = on_frame
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._log(f"[camera] Capture started (temp: {self._tmp_path})")

    async def stop_capture(self) -> None:
        """Stop the capture loop."""
        self._running = False
        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        # Cancelling the task doesn't stop a grab already running in its worker thread;
        # wait for it so its frame is included and nothing is written after we return.
        if self._grab_future is not None:
            try:
                await self._grab_future
            except Exception:
                pass
            self._grab_future = None
        self._log(f"[camera] Capture stopped. {self._frame_count} frames captured.")

    async def _capture_loop(self) -> None:
        import base64
        interval = 1.0 / self._capture_fps
        loop = asyncio.get_event_loop()
        while self._running:
            t0 = time.monotonic()
            self._grab_future = loop.run_in_executor(None, self._grab_frame)
            frame_path = await asyncio.shield(self._grab_future)
            if frame_path:
                self._latest_frame_path = frame_path
                if self._on_frame:
                    b64 = base64.b64encode(frame_path.read_bytes()).decode()
                    try:
                        await self._on_frame(b64, self._frame_count)
                    except Exception:
                        pass
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    def _grab_frame(self) -> Path | None:
        ret, frame = self._read_with_reopen(flush=2)
        if not ret:
            return None
        t = time.monotonic() - self._capture_t0
        fname = self._tmp_path / f"frame_{self._frame_count + 1:04d}.jpg"
        if not cv2.imwrite(str(fname), frame, [cv2.IMWRITE_JPEG_QUALITY, 80]):
            return None
        self._frame_count += 1
        self._frame_times.append(round(t, 2))
        return fname

    # ------------------------------------------------------------------ #
    #  Single snapshot (for starting image, preview, etc.)                #
    # ------------------------------------------------------------------ #

    def _read_with_reopen(self, flush: int = 0) -> tuple[bool, any]:
        """Thread-safe frame read with automatic reopen on failure.

        flush: frames to discard first. When the camera is read infrequently
        (0.5 fps during a run) the driver's queue can hand back a frame that is
        seconds old; grabbing a couple first returns the current scene."""
        with self._lock:
            if self._cap and self._cap.isOpened():
                for _ in range(flush):
                    self._cap.grab()
                ret, frame = self._cap.read()
                if ret:
                    return True, frame

            # Don't reopen more than once every 2 seconds to avoid hammering
            now = time.monotonic()
            if now - self._last_reopen_time < 2.0:
                return False, None

            if self._idx is None:
                return False, None

            self._log(f"[camera] read() failed, reopening at index {self._idx}")
            if self._cap:
                self._cap.release()
                self._cap = None

            # Re-plugging can renumber devices — verify it's still the overhead camera
            cap, desc = self._open_verified(self._idx)
            self._last_reopen_time = time.monotonic()
            if cap is None:
                self._log(f"[camera] Reopen failed: camera {self._idx} is {desc}.")
                if not self._failure_reported and self.on_failure:
                    self._failure_reported = True
                    try:
                        self.on_failure()
                    except Exception:
                        pass
                return False, None
            self._cap = cap
            self._failure_reported = False
            ret, frame = self._cap.read()
            if ret:
                self._log(f"[camera] Reopened overhead camera at index {self._idx} ({desc})")
            return ret, frame if ret else None

    def snapshot(self, save_path: Path) -> bool:
        """Capture one frame and save to save_path. Returns True on success."""
        ret, frame = self._read_with_reopen()
        if not ret:
            return False
        cv2.imwrite(str(save_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return True

    def snapshot_base64(self) -> tuple[str, str] | None:
        """Return (base64_data, media_type) for the current frame, or None."""
        ret, frame = self._read_with_reopen(flush=4)
        if not ret:
            return None
        success, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            return None
        return base64.b64encode(buf.tobytes()).decode(), "image/jpeg"

    # ------------------------------------------------------------------ #
    #  Post-run: collect and encode frames                                 #
    # ------------------------------------------------------------------ #

    def collect_and_encode(self) -> list[tuple[str, str]]:
        """
        Return all captured frames as sorted list of (base64_data, media_type).
        Call this after stop_capture() and before cleanup().
        """
        if not self._tmp_path:
            return []
        frames = sorted(self._tmp_path.glob("frame_*.jpg"))
        encoded = []
        for fp in frames:
            data = base64.b64encode(fp.read_bytes()).decode()
            encoded.append((data, "image/jpeg"))
        return encoded

    def frame_times(self) -> list[float]:
        """Capture time (s from start) of each frame returned by collect_and_encode()."""
        return list(self._frame_times)

    def cleanup(self) -> None:
        """Delete all temporary frame files."""
        if self._tmpdir:
            self._tmpdir.cleanup()
            self._tmpdir = None
            self._tmp_path = None
            self._latest_frame_path = None
            self._log("[camera] Temporary frames cleaned up.")

    # ------------------------------------------------------------------ #
    #  MJPEG / live preview                                                #
    # ------------------------------------------------------------------ #

    def get_latest_jpeg_bytes(self) -> bytes | None:
        """
        Return the latest frame as raw JPEG bytes.
        Used by the web server's MJPEG stream and single-frame endpoints.
        Falls back to grabbing a fresh frame from the camera if no file exists.
        """
        # Try the most recently saved file first (non-blocking, no extra capture)
        if self._latest_frame_path and self._latest_frame_path.exists():
            return self._latest_frame_path.read_bytes()

        # Fallback: grab a live frame directly
        ret, frame = self._read_with_reopen()
        if ret:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                return buf.tobytes()
        return None

    # ------------------------------------------------------------------ #
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def latest_frame_path(self) -> Path | None:
        return self._latest_frame_path
