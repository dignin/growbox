"""HomeCam agent - runs on the computer at home that can reach the webcam or RTSP camera.

Connects out to the VPS (no router port forwarding needed) and sends JPEG
frames only while someone is watching. Reconnects automatically.

    python camera_agent.py --server wss://camera.example.com/agent --keep-awake

The agent token is read from the HOMECAM_TOKEN environment variable or
from token.txt next to this script.
"""

import argparse
import datetime
import os
import sys
import time
from urllib.parse import urlsplit
from pathlib import Path

import cv2
from simple_websocket import Client, ConnectionClosed


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def read_token():
    token = os.environ.get("HOMECAM_TOKEN", "").strip()
    token_file = Path(__file__).resolve().with_name("token.txt")
    if not token and token_file.exists():
        token = token_file.read_text().strip()
    if not token:
        raise SystemExit("No token. Put it in token.txt next to this script or set HOMECAM_TOKEN.")
    return token


# Windows SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def set_keep_awake(enabled):
    """Stop Windows from sleeping on idle while the agent runs; the display may still turn off.

    Must be called from the main thread: Windows ties the request to the calling
    thread and drops it automatically when the process exits.
    """
    if os.name != "nt":
        if enabled:
            log("--keep-awake only applies to Windows; ignoring")
        return
    import ctypes
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    if ctypes.windll.kernel32.SetThreadExecutionState(flags) == 0:
        log("could not change the Windows sleep setting")
    elif enabled:
        log("keep-awake on: this PC will not sleep on idle while the agent runs")


class Webcam:
    def __init__(self, args):
        self.args = args
        self.cap = None
        self.retry_at = 0

    def read_jpeg(self):
        if self.cap is None:
            if time.monotonic() < self.retry_at:
                return None
            if self.args.rtsp_url:
                # TCP avoids dropped UDP packets; bounded reads allow reconnects.
                os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
                cap = cv2.VideoCapture(
                    self.args.rtsp_url, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                     cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
                )
            else:
                backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
                cap = cv2.VideoCapture(self.args.camera, backend)
            if not cap.isOpened():
                cap.release()
                log("could not open camera source, retrying in 5s")
                self.retry_at = time.monotonic() + 5
                return None
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep latency low
            self.cap = cap
            log("camera on")
        ok, img = self.cap.read()
        if not ok:
            log("camera read failed, reopening")
            self.close()
            self.retry_at = time.monotonic() + 5
            return None
        if self.args.rtsp_url:
            height, width = img.shape[:2]
            scale = min(self.args.width / width, self.args.height / height, 1.0)
            if scale < 1.0:
                img = cv2.resize(img, (max(1, round(width * scale)), max(1, round(height * scale))))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.args.quality])
        return jpg.tobytes() if ok else None

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
            log("camera off")


def stream(ws, cam, fps):
    streaming = False
    interval = 1.0 / fps
    while True:
        msg = ws.receive(timeout=0 if streaming else 1)  # raises ConnectionClosed on disconnect
        if msg == "start" and not streaming:
            log("viewer connected, streaming")
            streaming = True
        elif msg == "stop" and streaming:
            log("no viewers, pausing")
            streaming = False
            cam.close()
        if not streaming:
            continue
        started = time.monotonic()
        jpeg = cam.read_jpeg()
        if jpeg is None:
            time.sleep(0.5)
            continue
        ws.send(jpeg)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def main():
    p = argparse.ArgumentParser(description="HomeCam agent (runs at home, next to the webcam).")
    p.add_argument("--server", required=True, help="e.g. wss://cam.example.com/agent")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--camera", type=int, default=0, help="webcam index (default 0)")
    source.add_argument("--rtsp-url", help="local camera RTSP URL; alternatively use HOMECAM_RTSP_URL or rtsp-url.txt")
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=540)
    p.add_argument("--fps", type=float, default=10, help="max frames per second sent (default 10)")
    p.add_argument("--quality", type=int, default=70, help="JPEG quality 1-100 (default 70)")
    p.add_argument("--keep-awake", action="store_true",
                   help="Windows: prevent idle sleep while the agent runs (screen can still turn off)")
    args = p.parse_args()

    if not args.server.startswith("wss://") and "localhost" not in args.server and "127.0.0.1" not in args.server:
        raise SystemExit("Use a wss:// URL so the token and video are encrypted.")

    if args.fps <= 0 or args.width <= 0 or args.height <= 0 or not 1 <= args.quality <= 100:
        p.error("fps, width and height must be positive; quality must be 1-100")
    if args.rtsp_url is None:
        args.rtsp_url = os.environ.get("HOMECAM_RTSP_URL", "").strip()
        source_file = Path(__file__).resolve().with_name("rtsp-url.txt")
        if not args.rtsp_url and not os.environ.get("HOMECAM_IGNORE_RTSP_FILE") and source_file.exists():
            args.rtsp_url = source_file.read_text().strip()
        if args.rtsp_url and "--camera" in sys.argv:
            p.error("--camera cannot be combined with an RTSP source")
    if args.rtsp_url:
        parsed = urlsplit(args.rtsp_url)
        if parsed.scheme not in ("rtsp", "rtsps") or not parsed.hostname:
            p.error("RTSP source must be an rtsp:// or rtsps:// URL with a camera hostname")

    token = read_token()
    cam = Webcam(args)
    if args.keep_awake:
        set_keep_awake(True)
    backoff = 1
    while True:
        ws = None
        try:
            log(f"connecting to {args.server}")
            ws = Client.connect(args.server, headers={"Authorization": f"Bearer {token}"},
                                ping_interval=25)
            log("connected")
            backoff = 1
            stream(ws, cam, args.fps)
        except KeyboardInterrupt:
            break
        except (ConnectionClosed, ConnectionError, OSError) as e:
            log(f"connection lost ({e.__class__.__name__}: {e})")
        finally:
            cam.close()
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        log(f"reconnecting in {backoff}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            break
        backoff = min(backoff * 2, 60)

    if args.keep_awake:
        set_keep_awake(False)


if __name__ == "__main__":
    main()
