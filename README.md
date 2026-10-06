# Growbox camera

Version **0.1-alpha**.

Forward a camera from your home network to an Ubuntu server, then watch it in a
browser from anywhere with a login.

```text
Private camera network                         Public Ubuntu server
RTSP camera → Windows workstation ── WSS ──→ HomeCam + Caddy ← HTTPS ← Viewers
               camera agent
```

The workstation reads the local camera and makes an outbound encrypted connection
to the server. No home router port forwarding is needed. Keep the workstation
running and awake. The camera source is opened only while someone is watching.

The current version supports one camera connection and one shared website account.
Internet viewing requires a login; anonymous viewing is not enabled.

## If your Ubuntu server is already running

Skip to **Windows workstation setup** below. Have these ready:

- The working HTTPS website address.
- The camera connection token printed during server setup.
- The website username and password for viewers.

These are separate credentials: the workstation uses the token; viewers use the
website login. If the token is lost, follow **Replace the camera token** below.

## Admin: deploy the Ubuntu server

### 1. Domain and network

Point your domain's DNS A record to the Ubuntu server's public IP. Any AAAA record
must also point to this server over working IPv6. Allow incoming TCP ports 80 and
443, plus your SSH port, in the server and hosting provider firewalls.

Use the same domain in DNS, Caddy and the Windows setup window. Examples use
`camera.example.com`; replace it with your own domain.

### 2. Copy and install

From the workstation, copy the project's `server` folder (replace user and IP):

```bash
scp -r server YOUR_USER@SERVER_IP:~/homecam-server
```

On Ubuntu, run the following for a fresh installation:

```bash
sudo apt update
sudo apt install -y python3-venv caddy
sudo useradd --system --home /opt/homecam --shell /usr/sbin/nologin homecam
sudo mkdir -p /opt/homecam
sudo cp -r ~/homecam-server /opt/homecam/server
sudo python3 -m venv /opt/homecam/venv
sudo /opt/homecam/venv/bin/pip install -r /opt/homecam/server/requirements.txt
sudo chown -R homecam:homecam /opt/homecam
```

### 3. Create the login and camera token

```bash
cd /opt/homecam/server
sudo -u homecam /opt/homecam/venv/bin/python app.py --setup
```

Choose the website username and a password of at least 12 characters. On initial
setup, the server also creates and prints a camera token. Save it and give
it to the workstation user along with the HTTPS website address. Only the token's
hash is stored on the server, so the original token cannot be retrieved later.


### 4. Start the server service

```bash
sudo cp /opt/homecam/server/deploy/homecam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homecam
sudo systemctl status homecam --no-pager
```

The application listens on `127.0.0.1:8000` on the Ubuntu server. Caddy provides
public HTTPS access; do not use the local workstation address here.

### 5. Configure Caddy

Edit `/etc/caddy/Caddyfile`. If Growbox is the only site, replace the default
configuration with the block below. If the server hosts other sites, preserve
their complete blocks and add this as a separate top-level block.

```caddy
camera.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
```

Replace the domain if yours differs. Do not put this block inside `:80`, and keep
`flush_interval` inside `reverse_proxy`.

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
```

If validation succeeds:

```bash
sudo systemctl reload caddy
```

Caddy manages the HTTPS certificate. Open your HTTPS website and check that the
login page appears. Camera offline is expected until the workstation connects.

## Windows workstation setup

Run the agent on a workstation in the same private network as the RTSP
camera. The Ubuntu server does not need direct access to your local camera.

1. Install Python 3 with the Python launcher enabled.
2. Copy the **`agent` folder**, including `Start-Camera.cmd`,
   `setup_camera.py`, `camera_agent.py` and `requirements.txt`, to a permanent
   folder on the workstation.
3. Double-click **Start-Camera.cmd**. It creates a local environment and installs
   camera components on the first run; internet access is needed for this step.
4. In the setup window, enter:
   - **Server website address:** the admin's complete `https://…` address.
   - **Connection token:** the camera token from the admin.
   - **Camera RTSP address:** the camera's full URL, including any credentials.
     Leave this field blank to use the attached webcam instead.
5. Click **Test camera**. If it works, click **Save and connect**.
6. Keep the window open. Open the public website, sign in, and confirm video.

Example local camera address (replace every camera-specific part):

```text
rtsp://USERNAME:PASSWORD@192.168.1.100:554/CAMERA_STREAM_PATH
```

`192.168.1.100` is an example private address. Get the actual RTSP
path from the camera settings or its documentation. URL-encode special characters
in credentials.

### Saved settings and sleep

Settings are saved in `agent/camera-settings.json`, including the connection token
and camera credentials. Restrict access to this folder and do not share or commit
that file. On subsequent runs, reopen **Start-Camera.cmd** and click **Save and
connect**. Disconnect before changing settings. Closing the window stops the agent.
The launcher does not automatically connect at startup.

While connected, the agent requests that Windows stay awake on idle. The display
may turn off. Manual sleep, shutdown or closing a laptop lid can still stop video.
For a webcam, enable **Settings → Privacy & security → Camera → Let desktop apps
access your camera**.

## Viewers

Open the public HTTPS website and sign in using the website account created by the
admin. No camera software is needed on viewing devices. Use Pause/Resume, Snapshot
or Fullscreen in the viewer.

## Admin maintenance and troubleshooting

### Replace the camera token

If the token is lost or needs changing:

```bash
cd /opt/homecam/server
sudo -u homecam /opt/homecam/venv/bin/python app.py --new-agent-token
sudo systemctl restart homecam
```

The old token stops working. On the workstation, disconnect, replace it with the
new printed token and click **Save and connect**.

### Change the website login

```bash
cd /opt/homecam/server
sudo -u homecam /opt/homecam/venv/bin/python app.py --setup
sudo systemctl restart homecam
```

This signs out existing browser sessions. The server preserves an existing
camera token, so the workstation's token does not need changing.

### Diagnose problems

| Symptom | What to check |
|---|---|
| Launcher cannot start | Python launcher is installed; first-run dependency installation has internet access. |
| Camera test fails | RTSP address, credentials and path; camera reachable from the workstation. A 403 response means the source rejected the request. |
| Camera works but agent cannot connect | Exact HTTPS domain, certificate, connection token and Ubuntu service. |
| Website shows camera offline | Workstation window is open and connected; computer is awake. |
| Website returns 502 | `homecam` is running and Caddy targets `127.0.0.1:8000`. |
| Caddy validation fails | Top-level domain block, balanced braces and `flush_interval` inside `reverse_proxy`. |
| Login stops working after setup | Use the newly configured website account. Five failed attempts can cause a 15-minute lockout. |

On Ubuntu:

```bash
sudo systemctl status homecam caddy --no-pager
sudo journalctl -u homecam -n 50 --no-pager
sudo journalctl -u caddy -n 50 --no-pager
```

## Advanced: command-line agent

The desktop window is the recommended Windows setup. For a command-line agent,
install `agent/requirements.txt`, put the token in `agent/token.txt`, and put the
RTSP URL on one line in `agent/rtsp-url.txt`:

```powershell
cd C:\path\to\agent
py -m pip install -r requirements.txt
py camera_agent.py --server wss://YOUR_DOMAIN/agent --keep-awake
```

Alternatively use `HOMECAM_TOKEN` and `HOMECAM_RTSP_URL`. The RTSP source priority
is `--rtsp-url`, then the environment variable, then `rtsp-url.txt`. Without an RTSP
source the agent uses webcam 0; `--camera 1` selects another webcam.

The desktop window uses `camera-settings.json` and overrides the legacy token and
RTSP files. The command-line agent does not read `camera-settings.json`.

Options: `--width` / `--height` (default 960 × 540), `--fps` (default 10),
`--quality` (default 70), and `--keep-awake` (Windows). RTSP defaults to TCP and
retries failures. RTSP frames are scaled down to fit the chosen size while retaining
aspect ratio. Lower FPS or JPEG quality to reduce workstation upload bandwidth.

For Linux agents, use `agent/homecam-agent.service`, updating the domain in its
`ExecStart` line. Set the token and RTSP source files and install dependencies in
the service's Python environment before enabling it.
