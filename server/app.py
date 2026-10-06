"""HomeCam relay server - runs on the VPS.

The home camera agent connects *out* to this server over a WebSocket and
pushes JPEG frames; logged-in browsers watch them as an MJPEG stream.
No port forwarding is needed on the home router.

    python app.py --setup             # choose the website username + password
    python app.py --new-agent-token   # create the token the home agent uses
    python app.py                     # run (behind Caddy, see deploy/)
"""

import argparse
import datetime
import getpass
import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

MAX_FAILED_LOGINS = 5            # per IP address ...
LOCKOUT_WINDOW = 15 * 60         # ... within this many seconds
SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 12
MAX_FRAME_BYTES = 2 * 1024 * 1024
VIEWER_IDLE_TIMEOUT = 30         # end a viewer's stream after this long with no frames


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def setup_credentials():
    cfg = load_config()
    print("Set the website login for HomeCam.")
    username = input(f"Username [{cfg.get('username', 'admin')}]: ").strip() or cfg.get("username", "admin")
    while True:
        password = getpass.getpass(f"Password (min {MIN_PASSWORD_LENGTH} characters): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print("Too short, try again.")
            continue
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match, try again.")
            continue
        break
    cfg["username"] = username
    cfg["password_hash"] = generate_password_hash(password)
    cfg.setdefault("secret_key", secrets.token_hex(32))
    # Bumping the epoch signs out every existing session.
    cfg["session_epoch"] = cfg.get("session_epoch", 0) + 1
    save_config(cfg)
    print(f"Saved to {CONFIG_PATH.name}. All existing sessions have been signed out.")
    print("Restart the service for the change to take effect.")


def new_agent_token():
    cfg = load_config()
    token = secrets.token_urlsafe(32)
    cfg["agent_token_hash"] = hash_token(token)
    save_config(cfg)
    print("New agent token (shown only once - put it in agent/token.txt on the home machine):\n")
    print(f"    {token}\n")
    print("Any previously issued token no longer works. Restart the service for the change to take effect.")


# --------------------------------------------------------------------------
# Relay: holds the latest frame from the home agent and tells the agent to
# start/stop streaming depending on whether anyone is watching.
# --------------------------------------------------------------------------

class Relay:
    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.viewers = 0
        self.agent_generation = 0     # bumps on every agent connection
        self.agent_online = False
        self.agent_addr = None

    def run_agent(self, ws, addr):
        with self.cond:
            self.agent_generation += 1
            generation = self.agent_generation  # a newer connection replaces this one
            self.agent_online, self.agent_addr, self.frame = True, addr, None
            self.cond.notify_all()
        app_log(f"agent connected from {addr}")
        sent = None
        try:
            while True:
                with self.cond:
                    if self.agent_generation != generation:
                        return
                    wanted = "start" if self.viewers > 0 else "stop"
                if wanted != sent:
                    ws.send(wanted)
                    sent = wanted
                msg = ws.receive(timeout=0.5)
                if isinstance(msg, (bytes, bytearray)) and msg[:2] == b"\xff\xd8":  # JPEG magic
                    with self.cond:
                        self.frame = bytes(msg)
                        self.cond.notify_all()
        except ConnectionClosed:
            pass
        finally:
            with self.cond:
                if self.agent_generation == generation:
                    self.agent_online, self.agent_addr, self.frame = False, None, None
                    self.cond.notify_all()
            app_log(f"agent from {addr} disconnected")

    def _add_viewer(self, n):
        with self.cond:
            self.viewers += n

    def _wait_frame(self, previous, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.frame is not None and self.frame is not previous, timeout=timeout)
            return self.frame

    def mjpeg(self):
        self._add_viewer(1)
        try:
            last, idle_since = None, time.monotonic()
            while True:
                frame = self._wait_frame(last, timeout=5)
                if frame is None:
                    # Agent offline: end the response so the viewer slot is freed;
                    # the page reconnects when /status says the camera is back.
                    if time.monotonic() - idle_since > VIEWER_IDLE_TIMEOUT:
                        return
                    continue
                idle_since = time.monotonic()
                # Re-sending an unchanged frame on timeout doubles as a keepalive, so
                # closed browser tabs are noticed and the camera can be stopped.
                last = frame
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            self._add_viewer(-1)

    def snapshot(self):
        self._add_viewer(1)
        try:
            with self.cond:
                self.cond.wait_for(lambda: self.frame is not None, timeout=10)
                return self.frame
        finally:
            self._add_viewer(-1)


def app_log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------

def create_app(relay, cfg, behind_proxy=True):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg["secret_key"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=behind_proxy,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=SESSION_HOURS),
        SOCK_SERVER_OPTIONS={"ping_interval": 25, "max_message_size": MAX_FRAME_BYTES},
    )
    if behind_proxy:
        # Caddy sets X-Forwarded-For/Proto; needed for per-IP lockout and https URLs.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    sock = Sock(app)

    failed_attempts = defaultdict(deque)
    failed_lock = threading.Lock()

    def is_locked_out(ip):
        with failed_lock:
            attempts = failed_attempts[ip]
            while attempts and attempts[0] < time.time() - LOCKOUT_WINDOW:
                attempts.popleft()
            if not attempts:
                del failed_attempts[ip]
                return False
            return len(attempts) >= MAX_FAILED_LOGINS

    def record_failure(ip):
        with failed_lock:
            failed_attempts[ip].append(time.time())

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf():
        sent, expected = request.form.get("csrf", ""), session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400)

    app.jinja_env.globals["csrf_token"] = csrf_token

    def logged_in():
        return session.get("user") == cfg["username"] and session.get("epoch") == cfg["session_epoch"]

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not logged_in():
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapper

    @app.before_request
    def authenticate_agent():
        # Runs before flask-sock upgrades the connection, so a bad token gets a plain 401.
        if request.path != "/agent":
            return
        ip = request.remote_addr
        if is_locked_out(ip):
            abort(429)
        expected = cfg.get("agent_token_hash")
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not expected or not token or not secrets.compare_digest(hash_token(token), expected):
            record_failure(ip)
            app_log(f"rejected agent connection from {ip}")
            abort(401)

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'"
        resp.headers["Cache-Control"] = "no-store"
        if behind_proxy:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if logged_in():
            return redirect(url_for("index"))
        if request.method == "POST":
            check_csrf()
            ip = request.remote_addr
            if is_locked_out(ip):
                flash("Too many failed attempts. Try again in 15 minutes.")
                return render_template("login.html"), 429
            user_ok = secrets.compare_digest(request.form.get("username", ""), cfg["username"])
            pass_ok = check_password_hash(cfg["password_hash"], request.form.get("password", ""))
            if user_ok and pass_ok:
                session.clear()  # also rotates the CSRF token
                session.permanent = True
                session["user"] = cfg["username"]
                session["epoch"] = cfg["session_epoch"]
                app_log(f"login from {ip}")
                return redirect(url_for("index"))
            record_failure(ip)
            app_log(f"failed login from {ip}")
            time.sleep(1)  # slow down guessing
            flash("Wrong username or password.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        return render_template("index.html", user=cfg["username"])

    @app.get("/stream.mjpg")
    @login_required
    def stream():
        return Response(relay.mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"X-Accel-Buffering": "no"})

    @app.get("/snapshot.jpg")
    @login_required
    def snapshot():
        frame = relay.snapshot()
        if frame is None:
            return "Camera offline", 503
        name = datetime.datetime.now().strftime("homecam-%Y%m%d-%H%M%S.jpg")
        return Response(frame, mimetype="image/jpeg",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/status")
    @login_required
    def status():
        return {"camera_online": relay.agent_online, "viewers": relay.viewers}

    @sock.route("/agent")
    def agent(ws):
        relay.run_agent(ws, request.remote_addr)

    return app


def main():
    p = argparse.ArgumentParser(description="HomeCam relay server (runs on the VPS).")
    p.add_argument("--setup", action="store_true", help="set or change the website username and password")
    p.add_argument("--new-agent-token", action="store_true", help="create a new token for the home agent")
    p.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1, behind Caddy)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-proxy", action="store_true",
                   help="not behind a reverse proxy (local testing only; no HTTPS)")
    args = p.parse_args()

    if args.setup:
        setup_credentials()
        if not load_config().get("agent_token_hash"):
            new_agent_token()
        return
    if args.new_agent_token:
        return new_agent_token()

    cfg = load_config()
    if "password_hash" not in cfg:
        raise SystemExit("No login configured. Run: python app.py --setup")
    if "agent_token_hash" not in cfg:
        raise SystemExit("No agent token configured. Run: python app.py --new-agent-token")

    app = create_app(Relay(), cfg, behind_proxy=not args.no_proxy)
    server = make_server(args.host, args.port, app, threaded=True)
    app_log(f"HomeCam relay listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
