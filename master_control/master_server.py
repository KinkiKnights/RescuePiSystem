#!/usr/bin/env python3
"""Robot Master Server — manages robot programs via HTTP API on port 80"""

import json
import os
import signal
import subprocess
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import psutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRAMS_FILE = os.path.join(BASE_DIR, 'programs.json')
INDEX_FILE    = os.path.join(BASE_DIR, 'index.html')
PORT = 80


def _terminate_tree(proc):
    """Terminate a program and ALL its descendants.

    Programs are launched with start_new_session=True, so the child becomes the
    leader of a new process group. Signalling that whole group (killpg) ensures
    grandchildren such as `ros2 run`'s node or the camera publisher are killed
    too — a plain proc.terminate() would only kill the immediate shell and leave
    the real worker orphaned (camera/port stays busy and restart fails).
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class ProcessManager:
    def __init__(self):
        with open(PROGRAMS_FILE) as f:
            configs = json.load(f)
        self._lock = threading.Lock()
        self.programs = {c['id']: dict(c, process=None) for c in configs}

    def start(self, prog_id):
        with self._lock:
            prog = self.programs.get(prog_id)
            if not prog:
                return False, 'Program not found'
            if self._running(prog):
                return False, f"#{prog_id} is already running"
            try:
                kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
                if prog['type'] == 'ros2':
                    cmd = f'source /opt/ros/jazzy/setup.bash && {prog["cmd"]}'
                    proc = subprocess.Popen(cmd, shell=True, executable='/bin/bash', **kwargs)
                else:
                    proc = subprocess.Popen(prog['cmd'], shell=True, **kwargs)
                prog['process'] = proc
                return True, f"#{prog_id} started (PID {proc.pid})"
            except Exception as e:
                return False, str(e)

    def stop(self, prog_id):
        with self._lock:
            prog = self.programs.get(prog_id)
            if not prog:
                return False, 'Program not found'
            if not self._running(prog):
                return False, f"#{prog_id} is not running"
            try:
                _terminate_tree(prog['process'])
                prog['process'] = None
                return True, f"#{prog_id} stopped"
            except Exception as e:
                return False, str(e)

    def restart(self, prog_id):
        with self._lock:
            prog = self.programs.get(prog_id)
            if prog and self._running(prog):
                _terminate_tree(prog['process'])
                prog['process'] = None
        return self.start(prog_id)

    def get_status(self):
        with self._lock:
            return [
                {
                    'id':        prog['id'],
                    'name':      prog['name'],
                    'type':      prog['type'],
                    'status':    'running' if self._running(prog) else 'stopped',
                    'pid':       prog['process'].pid if self._running(prog) else None,
                    'autostart': bool(prog.get('autostart', False)),
                }
                for prog in sorted(self.programs.values(), key=lambda p: p['id'])
            ]

    def get_config(self):
        with self._lock:
            return [
                {'id': p['id'], 'name': p['name'], 'type': p['type'], 'cmd': p['cmd'],
                 'autostart': bool(p.get('autostart', False))}
                for p in sorted(self.programs.values(), key=lambda p: p['id'])
            ]

    def save_config(self, new_configs):
        with self._lock:
            old = self.programs
            new_ids = {c['id'] for c in new_configs}
            for pid, prog in list(old.items()):
                changed = any(c['id'] == pid and c['cmd'] != prog['cmd'] for c in new_configs)
                if (pid not in new_ids or changed) and self._running(prog):
                    _terminate_tree(prog['process'])
            # Normalise the autostart flag: honour an explicit value from the
            # payload, otherwise preserve whatever the program had before so the
            # basic config editor (which may omit it) never silently clears it.
            for c in new_configs:
                if 'autostart' in c:
                    c['autostart'] = bool(c['autostart'])
                else:
                    prev = old.get(c['id'])
                    c['autostart'] = bool(prev.get('autostart', False)) if prev else False
            with open(PROGRAMS_FILE, 'w') as f:
                json.dump(new_configs, f, ensure_ascii=False, indent=2)
            self.programs = {c['id']: dict(c, process=None) for c in new_configs}

    def set_autostart(self, prog_id, enabled):
        """Toggle a program's boot-autostart flag; persist to disk AND update the
        in-memory state so it takes effect without a service restart."""
        with self._lock:
            prog = self.programs.get(prog_id)
            if not prog:
                return False, 'Program not found'
            prog['autostart'] = bool(enabled)
            with open(PROGRAMS_FILE, 'w') as f:
                json.dump(self._snapshot(), f, ensure_ascii=False, indent=2)
            state = 'ON' if enabled else 'OFF'
            return True, f"#{prog_id} autostart {state}"

    def autostart_ids(self):
        """IDs of programs flagged for boot autostart, in ascending id order."""
        with self._lock:
            return [p['id'] for p in sorted(self.programs.values(), key=lambda p: p['id'])
                    if p.get('autostart')]

    def _snapshot(self):
        """On-disk representation of the current in-memory programs (no process)."""
        return [
            {k: v for k, v in p.items() if k != 'process'}
            for p in sorted(self.programs.values(), key=lambda p: p['id'])
        ]

    @staticmethod
    def _running(prog):
        return prog['process'] is not None and prog['process'].poll() is None


class CPUMonitor:
    def __init__(self, interval=5):
        self.cpu_percent = 0.0
        self._interval = interval
        self._timer = None
        psutil.cpu_percent()  # initialize measurement baseline
        self._schedule()

    def _schedule(self):
        self._timer = threading.Timer(self._interval, self._update)
        self._timer.daemon = True
        self._timer.start()

    def _update(self):
        self.cpu_percent = psutil.cpu_percent()
        self._schedule()

    def stop(self):
        if self._timer:
            self._timer.cancel()


class APIHandler(BaseHTTPRequestHandler):
    pm:  ProcessManager = None
    cpu: CPUMonitor     = None

    def log_message(self, *_):
        pass  # suppress default access log

    # ── GET ──────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            self._serve_html()
        elif path == '/status':
            self._json({
                'cpu_percent': self.cpu.cpu_percent,
                'timestamp':   datetime.now().isoformat(timespec='seconds'),
                'programs':    self.pm.get_status(),
            })
        elif path == '/programs/config':
            self._json(self.pm.get_config())
        else:
            self._not_found()

    # ── POST ─────────────────────────────────────────────
    def do_POST(self):
        parts = self.path.strip('/').split('/')

        if len(parts) == 2 and parts[0] in ('start', 'stop', 'restart'):
            try:
                prog_id = int(parts[1])
            except ValueError:
                self._not_found()
                return
            fn = {'start': self.pm.start, 'stop': self.pm.stop, 'restart': self.pm.restart}[parts[0]]
            ok, msg = fn(prog_id)
            self._json({'ok': ok, 'message': msg})

        elif len(parts) == 2 and parts[0] == 'autostart':
            try:
                prog_id = int(parts[1])
            except ValueError:
                self._not_found()
                return
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            try:
                enabled = bool(json.loads(body).get('enabled')) if body else False
            except Exception as e:
                self._json({'ok': False, 'message': str(e)})
                return
            ok, msg = self.pm.set_autostart(prog_id, enabled)
            self._json({'ok': ok, 'message': msg})

        elif parts == ['system', 'reboot']:
            self._json({'ok': True, 'message': 'Rebooting...'})
            threading.Timer(1.0, lambda: os.system('sudo reboot')).start()

        elif parts == ['system', 'shutdown']:
            self._json({'ok': True, 'message': 'Shutting down...'})
            threading.Timer(1.0, lambda: os.system('sudo shutdown -h now')).start()

        else:
            self._not_found()

    # ── PUT ──────────────────────────────────────────────
    def do_PUT(self):
        if self.path.strip('/') == 'programs/config':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                configs = json.loads(body)
                self.pm.save_config(configs)
                self._json({'ok': True, 'message': 'Config saved'})
            except Exception as e:
                self._json({'ok': False, 'message': str(e)})
        else:
            self._not_found()

    # ── helpers ──────────────────────────────────────────
    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        if not os.path.exists(INDEX_FILE):
            self.send_response(404)
            self.end_headers()
            return
        with open(INDEX_FILE, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self.send_response(404)
        self.end_headers()


def run_autostart(pm):
    """Launch every program flagged autostart when the service boots.

    Runs each start independently: logs the outcome and never lets a single
    failure abort server startup. Set MASTER_AUTOSTART_DRYRUN=1 to log the
    selection without actually spawning any process (used for safe testing).
    """
    dry_run = os.environ.get('MASTER_AUTOSTART_DRYRUN') == '1'
    ids = pm.autostart_ids()
    suffix = ' (DRY-RUN)' if dry_run else ''
    print(f'[Robot Master] Autostart targets: {ids}{suffix}')
    for pid in ids:
        try:
            if dry_run:
                ok, msg = True, '(dry-run: not launched)'
            else:
                ok, msg = pm.start(pid)
            print(f'[Robot Master] Autostart #{pid}: {"OK" if ok else "SKIP/FAIL"} — {msg}')
        except Exception as e:
            print(f'[Robot Master] Autostart #{pid}: ERROR — {e}')


def main():
    pm  = ProcessManager()
    cpu = CPUMonitor(interval=5)

    APIHandler.pm  = pm
    APIHandler.cpu = cpu

    # Auto-launch configured programs on boot before accepting requests.
    run_autostart(pm)

    # ThreadingHTTPServer: ブラウザの同時接続(ページ取得+/statusポーリング等)を
    # 並行処理する。単一スレッドのHTTPServerだと1接続の処理中に他がブロックされ、
    # UIがハングしたように見える。
    server = ThreadingHTTPServer(('0.0.0.0', PORT), APIHandler)
    server.daemon_threads = True
    print(f'[Robot Master] Listening on port {PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[Robot Master] Stopping...')
    finally:
        cpu.stop()
        server.server_close()


if __name__ == '__main__':
    main()
