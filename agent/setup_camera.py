"""Desktop setup and launcher for the HomeCam agent."""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
SETTINGS = BASE / 'camera-settings.json'


class Setup:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.events = queue.Queue()
        root.title('Growbox camera setup')
        root.minsize(480, 420)
        panel = ttk.Frame(root, padding=20)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Connect your camera', font=('', 16, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='Run this on the computer in the same network as your camera.').pack(anchor='w', pady=(4, 16))
        self.fields = {}
        for key, title, hint in [
            ('server', 'Server website address', 'Example: https://camera.example.com'),
            ('token', 'Connection token', 'Paste the token supplied by your server admin.'),
            ('rtsp_url', 'Camera RTSP address', 'Leave blank to use the attached webcam.')]:
            ttk.Label(panel, text=title).pack(anchor='w')
            value = tk.StringVar()
            ttk.Entry(panel, textvariable=value, show='*' if key == 'token' else '').pack(fill='x', pady=(4, 2))
            ttk.Label(panel, text=hint, wraplength=440).pack(anchor='w', pady=(0, 12))
            self.fields[key] = value
        try:
            saved = json.loads(SETTINGS.read_text())
            for key, value in self.fields.items():
                value.set(saved.get(key, ''))
        except (OSError, ValueError):
            pass
        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=6)
        self.test_button = ttk.Button(actions, text='Test camera', command=self.test)
        self.test_button.pack(side='left')
        self.connect_button = ttk.Button(actions, text='Save and connect', command=self.connect)
        self.connect_button.pack(side='left', padx=8)
        self.stop_button = ttk.Button(actions, text='Disconnect', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        self.status = tk.StringVar(value='Ready. Settings are saved on this computer; protect access to this folder.')
        ttk.Label(panel, textvariable=self.status, wraplength=440).pack(anchor='w', pady=8)
        self.logs = tk.Text(panel, height=8, width=55, state='disabled', wrap='word')
        self.logs.pack(fill='both', expand=True)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(150, self.poll)

    def values(self):
        return {key: value.get().strip() for key, value in self.fields.items()}

    def test(self):
        url = self.fields['rtsp_url'].get().strip()
        if url and (urlsplit(url).scheme not in ('rtsp', 'rtsps') or not urlsplit(url).hostname):
            messagebox.showerror('Camera address', 'Enter a complete rtsp:// camera address.')
            return
        self.test_button.configure(state='disabled')
        self.status.set('Testing camera connection…')
        def worker():
            try:
                from camera_agent import Webcam
                from types import SimpleNamespace
                camera = Webcam(SimpleNamespace(rtsp_url=url, camera=0, width=960, height=540, quality=70))
                try:
                    ok = camera.read_jpeg() is not None
                finally:
                    camera.close()
                self.events.put(('test', 'Camera works.' if ok else 'Camera unavailable. Check its address, credentials and network, then test again.'))
            except Exception:
                self.events.put(('test', 'Camera test failed. Check that camera dependencies are installed.'))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        values = self.values()
        try:
            address = urlsplit(values['server'])
            if address.scheme not in ('https', 'wss') or not address.hostname or address.username or address.password:
                raise ValueError('Enter the secure server address supplied by your admin (https://…).')
            if not values['token']:
                raise ValueError('Enter the connection token supplied by your admin.')
            camera = urlsplit(values['rtsp_url'])
            if values['rtsp_url'] and (camera.scheme not in ('rtsp', 'rtsps') or not camera.hostname):
                raise ValueError('Enter a complete rtsp:// camera address, or leave it blank for a webcam.')
            endpoint = 'wss://' + address.netloc + '/agent'
            temp = SETTINGS.with_suffix('.tmp')
            with open(temp, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
                json.dump(values, file, indent=2)
            temp.replace(SETTINGS)
            env = os.environ.copy()
            env['HOMECAM_TOKEN'] = values['token']
            env['HOMECAM_RTSP_URL'] = values['rtsp_url']
            env['HOMECAM_IGNORE_RTSP_FILE'] = '1'
            self.stop()
            self.process = subprocess.Popen(
                [sys.executable, '-u', str(BASE / 'camera_agent.py'), '--server', endpoint, '--keep-awake'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            process = self.process
            self.connect_button.configure(state='disabled')
            self.stop_button.configure(state='normal')
            self.status.set('Connecting to server…')
            def reader():
                for line in process.stdout:
                    # RTSP libraries can include credentials in diagnostic output.
                    if 'rtsp://' in line or 'rtsps://' in line:
                        line = 'Camera connection diagnostic (address hidden).\n'
                    self.events.put(('log', line.rstrip()))
                self.events.put(('exit', process))
            threading.Thread(target=reader, daemon=True).start()
        except (ValueError, OSError) as error:
            messagebox.showerror('Setup needs attention', str(error))

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'test':
                self.test_button.configure(state='normal')
                self.status.set(value)
            elif kind == 'log':
                self.logs.configure(state='normal')
                self.logs.insert('end', value + '\n')
                if int(self.logs.index('end-1c').split('.')[0]) > 150:
                    self.logs.delete('1.0', '2.0')
                self.logs.see('end')
                self.logs.configure(state='disabled')
                if value.endswith(' connected'):
                    self.status.set('Connected. Open the server website to watch your camera.')
                elif 'reconnecting' in value or 'connection lost' in value:
                    self.status.set('Server connection lost. Retrying automatically…')
                elif 'could not open' in value or 'camera read failed' in value:
                    self.status.set('Camera unavailable. Check its address and network.')
            elif kind == 'exit' and value is self.process:
                self.process = None
                self.connect_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                self.status.set('Agent stopped. Check the messages, then connect again.')
        self.root.after(150, self.poll)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.connect_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self.status.set('Disconnected.')

    def close(self):
        self.stop()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    Setup(root)
    root.mainloop()
