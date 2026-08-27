#!/usr/bin/env python3
"""Robot Master Server — manages robot programs via HTTP API on port 80"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import psutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# カメラ / マイクのデバイス設定 (robot/device_config.py)。プログラム起動時に
# 環境変数として注入し、Web UI からは候補を選んで保存できるようにする。
sys.path.insert(0, os.path.dirname(BASE_DIR))
import device_config  # noqa: E402
PROGRAMS_FILE = os.path.join(BASE_DIR, 'programs.json')
INDEX_FILE    = os.path.join(BASE_DIR, 'index.html')
# 既定は 80 (systemd ユニットが CAP_NET_BIND_SERVICE を付ける)。検証時は
# MASTER_CONTROL_PORT で非特権ポートに変えられる。
PORT = int(os.environ.get('MASTER_CONTROL_PORT') or 80)


def _terminate_tree(proc, grace=5.0):
    """Terminate a program and ALL its descendants, robustly.

    Programs are launched with start_new_session=True, so the child leads a new
    process group; signalling that whole group (killpg) reaches grandchildren
    such as `ros2 run`'s node or the camera publisher too.

    We must wait for the ENTIRE group to exit, not merely the tracked leader.
    The previous version did `killpg(SIGTERM)` then `proc.wait(timeout=5)` and
    escalated to SIGKILL only if that wait timed out. But `proc.wait()` returns
    as soon as the tracked *leader* (e.g. the `ros2 run` wrapper) exits — when a
    slower grandchild node had not yet honoured SIGTERM, the wait returned early,
    the SIGKILL escalation was skipped, and the node was reparented to init
    (ppid=1) as an orphan: a duplicate DDS publisher that corrupts discovery.

    Here we poll the whole process group for liveness and, if anything survives
    the grace period, SIGKILL the group (plus any escapee descendant by PID).
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    def _group_alive():
        # signal 0 probes the whole process group; ESRCH -> nobody left.
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    # 1. Polite shutdown of the whole group.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # 2. Wait for EVERY group member to exit (reaping the leader as it goes).
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and _group_alive():
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        else:
            time.sleep(0.1)  # leader reaped; keep polling for stragglers

    # 3. Escalate: SIGKILL the whole group, then chase any escapee descendants.
    if _group_alive():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            me = psutil.Process(proc.pid)
            for child in me.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
        kdeadline = time.monotonic() + 3.0
        while time.monotonic() < kdeadline and _group_alive():
            time.sleep(0.1)

    # 4. Reap the tracked leader so it never lingers as a zombie.
    try:
        proc.wait(timeout=1)
    except Exception:
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
                              start_new_session=True, env=self._child_env())
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
    def _child_env():
        """子プロセスへ渡す環境変数 (デバイス設定を注入する)。

        カメラ/マイクの指定を programs.json の cmd 文字列に埋め込む代わりに、
        devices.json から解決した値をここで環境変数として渡す。publisher 側は
        引数 > 環境変数 > devices.json の順で解決するので、cmd で明示した値が
        あればそちらが優先される。
        """
        env = dict(os.environ)
        try:
            env.update(device_config.program_env())
        except Exception as exc:
            print(f"[devices] 設定を解決できませんでした({exc})。既定で起動します。",
                  flush=True)
        return env

    def find_by_name(self, name):
        """名前でプログラムを引く (デバイス設定の即反映で camera / mic を特定する)。"""
        with self._lock:
            for prog in sorted(self.programs.values(), key=lambda p: p['id']):
                if prog.get('name') == name:
                    return {'id': prog['id'],
                            'running': self._running(prog)}
        return None

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
        elif path == '/devices':
            self._json(self._devices_payload())
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

        elif parts == ['devices']:
            # 設定を保存する。{"apply": true} なら保存後に該当プログラムを再起動。
            body = self._read_json()
            if body is None:
                return
            try:
                cfg = device_config.validate(body.get('config', body))
                path = device_config.save(cfg)
            except ValueError as e:
                self._json({'ok': False, 'message': f'設定が不正です: {e}'})
                return
            except OSError as e:
                self._json({'ok': False, 'message': f'保存できませんでした: {e}'})
                return
            msg = f'デバイス設定を保存しました ({path})'
            if body.get('apply'):
                ok, amsg = self._apply_devices(body.get('target', 'all'))
                self._json({'ok': ok, 'message': f'{msg} / {amsg}',
                            'devices': self._devices_payload()})
            else:
                self._json({'ok': True, 'message': f'{msg}。反映は「即反映」または再起動で',
                            'devices': self._devices_payload()})

        elif parts == ['devices', 'apply']:
            body = self._read_json() if self.headers.get('Content-Length') else {}
            if body is None:
                return
            ok, msg = self._apply_devices((body or {}).get('target', 'all'))
            self._json({'ok': ok, 'message': msg, 'devices': self._devices_payload()})

        elif parts == ['system', 'reboot']:
            self._json({'ok': True, 'message': 'Rebooting...'})
            threading.Timer(1.0, lambda: os.system('sudo reboot')).start()

        elif parts == ['system', 'shutdown']:
            self._json({'ok': True, 'message': 'Shutting down...'})
            threading.Timer(1.0, lambda: os.system('sudo shutdown -h now')).start()

        else:
            self._not_found()

    # ── デバイス設定 ─────────────────────────────────────
    def _read_json(self):
        """リクエストボディを JSON として読む。失敗時は応答を返して None。"""
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            return json.loads(raw or b'{}')
        except Exception as e:
            self._json({'ok': False, 'message': f'JSON を解釈できません: {e}'})
            return None

    def _devices_payload(self):
        """UI 用: 設定・候補・解決結果・デバイス存在確認 + 対象プログラムの状態。"""
        try:
            st = device_config.status()
        except Exception as e:
            return {'error': str(e)}
        st['programs'] = {name: self.pm.find_by_name(name) for name in ('camera', 'mic')}
        return st

    def _apply_devices(self, target):
        """設定を即反映する。該当プログラムを再起動 (停止中なら起動) する。"""
        targets = ('camera', 'mic') if target not in ('camera', 'mic') else (target,)
        done, missing = [], []
        for name in targets:
            info = self.pm.find_by_name(name)
            if not info:
                missing.append(name)
                continue
            if info['running']:
                ok, _ = self.pm.restart(info['id'])
                done.append(f'{name} を再起動' if ok else f'{name} の再起動に失敗')
            else:
                ok, _ = self.pm.start(info['id'])
                done.append(f'{name} を起動' if ok else f'{name} の起動に失敗')
        msg = '、'.join(done) if done else '対象プログラムがありません'
        if missing:
            msg += f" (programs.json に {'/'.join(missing)} がありません)"
        return (not any('失敗' in d for d in done)) and bool(done), msg

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
