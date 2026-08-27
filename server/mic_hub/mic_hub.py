#!/usr/bin/env python3
# =============================================================================
#  mic_hub.py — 号機マイク音声の集約ハブ（kkrtx 上で 1 プロセス / 1 ポート）
# -----------------------------------------------------------------------------
#  役割:
#    1) 号機(publisher)が push してくる 16kHz/mono/S16LE の生 PCM を受け取る
#         POST /ingest/<unit>   (Transfer-Encoding: chunked)
#    2) 同じポートで、パスによって号機を選んで再配信する
#         GET /listen/<unit>    (streaming WAV)   … 既定
#         GET /<unit>           (上のエイリアス)
#         GET /listen/<unit>?format=raw           … ヘッダ無しの生 S16LE
#    3) 号機ごとに WAV を常時録音する（セグメント分割＋古い分の自動削除）
#    4) 稼働状況を JSON と Web UI で見せる
#         GET /api/status  /  GET /healthz  /  GET /  (ブラウザ試聴つき)
#
#  設計の要点:
#    - **依存ゼロ**。Python 標準ライブラリのみ。GStreamer も numpy も要らない。
#      (16kHz/mono/S16LE = 256kbps の生 PCM をそのまま流すのでコーデックが不要)
#    - **ポートは 1 つだけ**。号機の選択はパス(`/3` `/4` `/5`)で行う。
#      号機ごとに下流ポートを開けていた旧 relay 方式を置き換える。
#    - **号機が push**。ハブは待ち受けるだけなので、号機の IP が変わっても
#      ハブ側の設定変更は要らないし、起動順にも依存しない。
#    - **遅い購読者はハブを止めない**。購読者ごとに固定長キューを持ち、
#      溢れたらその購読者だけを切る（取り込みと録音は絶対に止めない）。
#
#  使い方:
#    python3 hub/mic_hub.py --port 8770 --outdir ~/kk_ws/logs/mic-recordings
# =============================================================================
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import signal
import socket
import struct
import sys
import threading
import time
import wave
from array import array
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- ワイヤ契約（publisher / 購読者と共有する固定値）------------------------
PROTOCOL_VERSION = "1"
SAMPLE_RATE = 16000          # Hz。detector が最終的に使うレートに送信段で合わせる
CHANNELS = 1
SAMPLE_WIDTH = 2             # bytes (S16LE)
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH   # 32000 B/s = 256 kbps

# 号機 ID に許すトークン。パスに埋まるので厳しめに絞る。
UNIT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 購読者ごとのキュー段数。1 段 = publisher が送ってきた 1 チャンク(既定 100ms)。
# 64 段 ≒ 6.4 秒。これを超えて詰まる購読者は死んでいるとみなして切断する。
SUBSCRIBER_QUEUE_CHUNKS = 64

# 取り込みソケットの読み取りタイムアウト。無音のまま固まった publisher を
# 掴んだままにしないための上限（正常時は 100ms ごとにデータが来る）。
INGEST_READ_TIMEOUT_SEC = 10.0

# 現職 publisher が「まだ生きている」とみなす無通信の猶予。これを過ぎて
# データが来ていなければ、新しい publisher に号機を明け渡す（奪取）。
# Wi-Fi 断で取り残されたゾンビ接続からの復帰時間がこの値になる。
INGEST_STALE_SEC = 3.0

# 購読者への書き込みタイムアウト。TCP 送信バッファが詰まったまま
# ブロックし続けるのを防ぐ。
SUBSCRIBER_WRITE_TIMEOUT_SEC = 10.0

# レベル計(dBFS)の更新間隔。UI とステータス API 用の表示値。
LEVEL_WINDOW_SEC = 0.1

LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """時刻付きの 1 行ログ（systemd の journal にそのまま流れる）。"""
    with LOG_LOCK:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now() -> float:
    return time.monotonic()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def streaming_wav_header(rate: int = SAMPLE_RATE, channels: int = CHANNELS,
                         width: int = SAMPLE_WIDTH) -> bytes:
    """長さ未定(ストリーミング)の WAV ヘッダ 44 バイトを組み立てる。

    RIFF/data のサイズ欄には 0xFFFFFFFF を入れる。ffmpeg・VLC・ブラウザは
    これを「終わりが分からないストリーム」として扱い、EOF まで読み続ける。
    """
    byte_rate = rate * channels * width
    block_align = channels * width
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, width * 8)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


def rms_dbfs(pcm: bytes) -> float:
    """S16LE のバイト列から RMS を dBFS で返す（無音は -inf ではなく -120）。"""
    if len(pcm) < SAMPLE_WIDTH:
        return -120.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)])
    if sys.byteorder == "big":       # array は native endian。S16LE に合わせる
        samples.byteswap()
    acc = 0
    for s in samples:
        acc += s * s
    rms = math.sqrt(acc / len(samples)) / 32768.0
    return 20.0 * math.log10(rms) if rms > 1e-6 else -120.0


# ---- 録音（号機ごと・セグメント分割・自動削除）------------------------------
class SegmentRecorder:
    """号機 1 台ぶんの WAV 連続録音。

    `segment_sec` ごとにファイルを切り替え、`retention_hours` を超えた
    古いファイルと、合計 `max_bytes` を超えたぶんの古いファイルを消す。
    購読者が 1 人も居なくても録音は動き続ける。
    """

    def __init__(self, unit: str, outdir: str, segment_sec: int,
                 retention_hours: float, max_bytes: int):
        self.unit = unit
        self.dir = os.path.join(outdir, f"unit{unit}")
        self.segment_sec = segment_sec
        self.retention_hours = retention_hours
        self.max_bytes = max_bytes
        self.segment_bytes = segment_sec * BYTES_PER_SEC
        self._wav: wave.Wave_write | None = None
        self._path: str | None = None
        self._written = 0
        os.makedirs(self.dir, exist_ok=True)

    @property
    def path(self) -> str | None:
        return self._path

    def _open(self) -> None:
        name = f"unit{self.unit}_{datetime.now():%Y%m%d-%H%M%S}.wav"
        self._path = os.path.join(self.dir, name)
        self._wav = wave.open(self._path, "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(SAMPLE_RATE)
        self._written = 0

    def write(self, pcm: bytes) -> None:
        if self._wav is None:
            self._open()
        assert self._wav is not None
        self._wav.writeframesraw(pcm)
        self._written += len(pcm)
        if self._written >= self.segment_bytes:
            self.rotate()

    def rotate(self) -> None:
        self.close()
        self._prune()

    def close(self) -> None:
        if self._wav is not None:
            try:
                self._wav.close()          # ここで正しいサイズ欄が書き込まれる
            except Exception as e:         # noqa: BLE001 録音失敗で配信は止めない
                log(f"[{self.unit}] recorder close failed: {e}")
            self._wav = None
            self._path = None

    def _prune(self) -> None:
        """保持期間と総容量の両方で古いセグメントを削除する。"""
        try:
            files = []
            for name in os.listdir(self.dir):
                if not name.endswith(".wav"):
                    continue
                p = os.path.join(self.dir, name)
                try:
                    st = os.stat(p)
                except FileNotFoundError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
            files.sort()

            deadline = time.time() - self.retention_hours * 3600
            total = sum(f[1] for f in files)
            for mtime, size, p in list(files):
                too_old = self.retention_hours > 0 and mtime < deadline
                too_big = 0 < self.max_bytes < total
                if not (too_old or too_big):
                    break
                try:
                    os.remove(p)
                    total -= size
                    files.pop(0)
                except OSError as e:
                    log(f"[{self.unit}] prune failed for {p}: {e}")
                    break
        except OSError as e:
            log(f"[{self.unit}] prune failed: {e}")


# ---- 購読者 -----------------------------------------------------------------
class Subscriber:
    """1 本の GET /listen 接続。固定長キューで取り込み側から切り離す。"""

    __slots__ = ("q", "peer", "fmt", "dropped", "started", "closed")

    def __init__(self, peer: str, fmt: str):
        self.q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_CHUNKS)
        self.peer = peer
        self.fmt = fmt
        self.dropped = 0
        self.started = now()
        self.closed = False

    def offer(self, chunk: bytes) -> bool:
        """満杯なら False を返す（呼び出し側がこの購読者を切る）。"""
        try:
            self.q.put_nowait(chunk)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def stop(self) -> None:
        self.closed = True
        try:
            self.q.put_nowait(None)      # 送出スレッドの get() を起こす番兵
        except queue.Full:
            pass


# ---- 号機 1 台ぶんの状態 -----------------------------------------------------
class Unit:
    def __init__(self, name: str, hub: "Hub"):
        self.name = name
        self.hub = hub
        self.lock = threading.Lock()
        self.subs: set[Subscriber] = set()

        self.epoch = 0                    # publisher の世代。後勝ちの判定に使う
        self.online = False
        self.publisher_peer = ""
        self.publisher_source = ""
        self.connected_at = 0.0
        self.connected_at_wall = ""
        self.last_data_at = 0.0
        self.total_bytes = 0
        self.session_bytes = 0
        self.level_dbfs = -120.0
        self.disconnects = 0

        self._level_buf = bytearray()
        self.recorder: SegmentRecorder | None = None
        if hub.record:
            self.recorder = SegmentRecorder(
                name, hub.outdir, hub.segment_sec,
                hub.retention_hours, hub.max_bytes)

    # -- publisher の受け入れ（現職が沈黙していれば奪取）--------------------
    def claim(self, peer: str, source: str) -> int:
        """新しい publisher を受け入れ、その世代番号を返す（拒否なら 0）。

        現職が INGEST_STALE_SEC 以内にデータを送っていれば新参を拒否する。
        「常に後勝ち」にすると、二重起動した publisher 同士が 1 秒おきに
        互いを蹴り合って音が途切れ続けるため。
        逆に現職が沈黙していれば奪取を許す。Wi-Fi 断でハブ側にだけ残った
        ゾンビ接続はデータを送ってこないので、この経路で数秒で置き換わる
        （取りこぼした場合も INGEST_READ_TIMEOUT_SEC で刈られる）。
        """
        with self.lock:
            if self.online:
                idle = now() - self.last_data_at
                if idle < INGEST_STALE_SEC:
                    log(f"[{self.name}] publisher rejected: {peer} — "
                        f"{self.publisher_peer} is active (idle {idle:.1f}s)")
                    return 0
                log(f"[{self.name}] publisher taken over: {self.publisher_peer} "
                    f"(silent {idle:.1f}s) -> {peer}")
            self.epoch += 1
            self.online = True
            self.publisher_peer = peer
            self.publisher_source = source
            self.connected_at = now()
            self.connected_at_wall = utc_iso()
            self.last_data_at = now()
            self.session_bytes = 0
            self._level_buf = bytearray()
            return self.epoch

    def release(self, epoch: int) -> None:
        with self.lock:
            if epoch != self.epoch:
                return                     # すでに後発に置き換えられている
            self.online = False
            self.level_dbfs = -120.0
            self.disconnects += 1
            subs = list(self.subs)
            self.subs.clear()
            rec = self.recorder
        for s in subs:                     # 上流が切れたら購読者にも EOF を返す
            s.stop()
        if rec is not None:
            rec.rotate()

    def is_current(self, epoch: int) -> bool:
        with self.lock:
            return epoch == self.epoch

    # -- 取り込み → 配信・録音 --------------------------------------------
    def push(self, chunk: bytes, epoch: int) -> None:
        with self.lock:
            if epoch != self.epoch:
                return
            self.last_data_at = now()
            self.total_bytes += len(chunk)
            self.session_bytes += len(chunk)
            self._level_buf += chunk
            if len(self._level_buf) >= int(BYTES_PER_SEC * LEVEL_WINDOW_SEC):
                self.level_dbfs = rms_dbfs(bytes(self._level_buf))
                self._level_buf = bytearray()
            subs = list(self.subs)
            rec = self.recorder

        stalled = [s for s in subs if not s.offer(chunk)]
        if stalled:
            with self.lock:
                for s in stalled:
                    self.subs.discard(s)
            for s in stalled:
                log(f"[{self.name}] subscriber {s.peer} too slow — dropped")
                s.stop()

        if rec is not None:
            try:
                rec.write(chunk)
            except OSError as e:           # 録音が死んでも配信は続ける
                log(f"[{self.name}] recording write failed: {e}")

    # -- 購読者の出入り ----------------------------------------------------
    def add_subscriber(self, sub: Subscriber) -> bool:
        with self.lock:
            if not self.online:
                return False
            self.subs.add(sub)
            return True

    def remove_subscriber(self, sub: Subscriber) -> None:
        with self.lock:
            self.subs.discard(sub)

    def snapshot(self) -> dict:
        with self.lock:
            age = (now() - self.last_data_at) if self.online else None
            uptime = (now() - self.connected_at) if self.online else 0.0
            return {
                "unit": self.name,
                "online": self.online,
                "publisher": self.publisher_peer if self.online else None,
                "source": self.publisher_source if self.online else None,
                "connected_at": self.connected_at_wall if self.online else None,
                "uptime_sec": round(uptime, 1),
                "last_data_age_sec": round(age, 2) if age is not None else None,
                "level_dbfs": round(self.level_dbfs, 1),
                "listeners": len(self.subs),
                "session_bytes": self.session_bytes,
                "total_bytes": self.total_bytes,
                "disconnects": self.disconnects,
                "recording": (os.path.basename(self.recorder.path)
                              if self.recorder and self.recorder.path else None),
            }


# ---- ハブ本体 ---------------------------------------------------------------
class Hub:
    def __init__(self, args: argparse.Namespace):
        self.record = not args.no_record
        self.outdir = os.path.expanduser(args.outdir)
        self.segment_sec = args.segment
        self.retention_hours = args.retention_hours
        self.max_bytes = int(args.max_gb * 1024 ** 3)
        self.started_wall = utc_iso()
        self.started = now()
        self._units: dict[str, Unit] = {}
        self._lock = threading.Lock()
        for name in args.unit:             # 事前宣言（UI に offline として出る）
            self.unit(name)

    def unit(self, name: str) -> Unit:
        """号機を取得し、無ければ作る。**取り込み側だけ**が呼ぶこと。"""
        with self._lock:
            u = self._units.get(name)
            if u is None:
                u = Unit(name, self)
                self._units[name] = u
            return u

    def find(self, name: str) -> Unit | None:
        """既知の号機を引く（作らない）。

        購読・HEAD からはこちらを使う。参照だけで号機を作ると、
        ポートスキャンや打ち間違いのパスが幽霊号機として UI に並び続ける。
        """
        with self._lock:
            return self._units.get(name)

    def known(self) -> list[Unit]:
        with self._lock:
            return [self._units[k] for k in sorted(self._units)]

    def status(self) -> dict:
        return {
            "protocol": PROTOCOL_VERSION,
            "rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "format": "S16LE",
            "started_at": self.started_wall,
            "uptime_sec": round(now() - self.started, 1),
            "recording": self.record,
            "outdir": self.outdir if self.record else None,
            "units": [u.snapshot() for u in self.known()],
        }

    def close(self) -> None:
        for u in self.known():
            if u.recorder is not None:
                u.recorder.close()


# ---- HTTP ハンドラ ----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MicHub/2"
    sys_version = ""
    hub: Hub                                # ThreadingHTTPServer 側で注入

    # BaseHTTPRequestHandler の既定は stderr へ 1 リクエスト 1 行。
    # 長時間接続が主なので、こちらで必要なものだけログする。
    def log_message(self, fmt: str, *a) -> None:  # noqa: A003
        return

    def peer(self) -> str:
        return f"{self.client_address[0]}:{self.client_address[1]}"

    # -- 小物 --------------------------------------------------------------
    def send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200,
                  ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:              # noqa: N802 (BaseHTTPRequestHandler の規約)
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)

        if path == "/healthz":
            self.send_text("ok\n")
            return
        if path in ("/api/status", "/status.json"):
            self.send_json(self.hub.status())
            return
        if path == "/":
            self.serve_ui()
            return

        unit = self.match_listen(path)
        if unit is not None:
            self.serve_listen(unit, (qs.get("format") or ["wav"])[0].lower())
            return

        self.send_text(
            "not found\n\n"
            "  GET  /                    status UI\n"
            "  GET  /api/status          JSON\n"
            "  GET  /<unit>              streaming WAV   (例: /5)\n"
            "  GET  /listen/<unit>       同上\n"
            "  GET  /listen/<unit>?format=raw   ヘッダ無し S16LE\n"
            "  POST /ingest/<unit>       publisher からの取り込み\n",
            status=404)

    def do_HEAD(self) -> None:             # noqa: N802
        # ブラウザ/プレイヤの事前問い合わせ用。本文は返さない。
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        unit = self.match_listen(path)
        if unit is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = self.hub.find(unit)
        online = bool(u and u.snapshot()["online"])
        self.send_response(200 if online else 503)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def match_listen(path: str) -> str | None:
        """`/listen/<unit>`・`/<unit>`・`/<unit>.wav` から号機 ID を取り出す。"""
        for prefix in ("/listen/", "/"):
            if path.startswith(prefix):
                tok = path[len(prefix):]
                if tok.endswith(".wav"):
                    tok = tok[:-4]
                if UNIT_RE.match(tok) and not tok.startswith("api"):
                    return tok
        return None

    def serve_listen(self, name: str, fmt: str) -> None:
        if fmt not in ("wav", "raw"):
            self.send_text(f"unknown format {fmt!r}; use wav or raw\n", status=400)
            return
        unit = self.hub.find(name)
        sub = Subscriber(self.peer(), fmt)
        if unit is None or not unit.add_subscriber(sub):
            # 上流が居ないうちは 503 を返して切る。購読側(detector など)は
            # 自前の再接続ループで繋ぎ直すので、無音を配って生きているように
            # 見せかけるより正直で復帰も速い。
            self.send_text(f"unit {name} is offline\n", status=503)
            return

        log(f"[{name}] listener + {sub.peer} ({fmt})")
        try:
            self.send_response(200)
            self.send_header("Content-Type",
                             "audio/wav" if fmt == "wav"
                             else f"audio/L16; rate={SAMPLE_RATE}; channels={CHANNELS}")
            self.send_header("X-Mic-Protocol", PROTOCOL_VERSION)
            self.send_header("X-Mic-Rate", str(SAMPLE_RATE))
            self.send_header("X-Mic-Channels", str(CHANNELS))
            self.send_header("X-Mic-Format", "S16LE")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            if fmt == "wav":
                self.wfile.write(streaming_wav_header())
            self.wfile.flush()

            self.connection.settimeout(SUBSCRIBER_WRITE_TIMEOUT_SEC)
            while not sub.closed:
                try:
                    # timeout つきで待つ: 上流断で番兵を積めなかった場合でも
                    # closed フラグを見て必ず抜けられるようにする。
                    chunk = sub.q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if chunk is None:          # 上流断 or 切断指示（番兵）
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            unit.remove_subscriber(sub)
            self.close_connection = True
            log(f"[{name}] listener - {sub.peer}"
                + (f" (dropped {sub.dropped})" if sub.dropped else ""))

    def serve_ui(self) -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "static", "index.html"), "rb") as f:
                body = f.read()
        except OSError:
            self.send_text("UI not installed (hub/static/index.html missing)\n",
                           status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- POST /ingest/<unit> ------------------------------------------------
    def do_POST(self) -> None:             # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if not path.startswith("/ingest/"):
            self.send_text("not found\n", status=404)
            return
        name = path[len("/ingest/"):]
        if not UNIT_RE.match(name):
            self.send_text(f"bad unit id {name!r}\n", status=400)
            return

        rate = self.headers.get("X-Mic-Rate")
        if rate and rate != str(SAMPLE_RATE):
            self.send_text(
                f"rate mismatch: hub expects {SAMPLE_RATE}, publisher sent {rate}\n",
                status=400)
            log(f"[{name}] rejected publisher {self.peer()}: rate={rate}")
            return

        source = self.headers.get("X-Mic-Source", "")
        unit = self.hub.unit(name)
        epoch = unit.claim(self.peer(), source)
        if epoch == 0:
            self.send_text(
                f"unit {name} already has an active publisher\n",
                status=HTTPStatus.CONFLICT)
            self.close_connection = True
            return
        log(f"[{name}] publisher + {self.peer()} src={source or '-'}")

        total = 0
        reason = "eof"
        try:
            self.connection.settimeout(INGEST_READ_TIMEOUT_SEC)
            for chunk in self.read_body():
                if not unit.is_current(epoch):
                    reason = "replaced"
                    break
                unit.push(chunk, epoch)
                total += len(chunk)
        except socket.timeout:
            reason = "read timeout"
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            reason = f"{type(e).__name__}"
        except ValueError as e:            # 壊れた chunked フレーミング
            reason = f"protocol error: {e}"
        finally:
            unit.release(epoch)
            secs = total / BYTES_PER_SEC if total else 0.0
            log(f"[{name}] publisher - {self.peer()} ({reason}, "
                f"{total} bytes / {secs:.1f}s)")
            self.close_connection = True
            # 相手はもう送るのをやめている。応答は best-effort。
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                pass

    def read_body(self):
        """chunked / Content-Length / EOF まで、の 3 通りを吸収して読む。

        publisher は chunked を使う（長さが事前に決まらないため）。curl や
        ffmpeg から手で流し込むときのために他の 2 つも受け付ける。
        """
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            while True:
                line = self.rfile.readline(64)
                if not line:
                    return
                token = line.strip().split(b";")[0]
                if not token:
                    continue
                try:
                    size = int(token, 16)
                except ValueError as e:
                    raise ValueError(f"bad chunk size {token!r}") from e
                if size == 0:
                    self.rfile.readline(64)          # 終端 CRLF
                    return
                remaining = size
                while remaining:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                    yield data
                self.rfile.readline(8)               # チャンク末尾の CRLF

        length = self.headers.get("Content-Length")
        if length is not None:
            remaining = int(length)
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    return
                remaining -= len(data)
                yield data
            return

        while True:                                   # EOF まで
            data = self.rfile.read(65536)
            if not data:
                return
            yield data


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 号機 3 台 + 購読者数本程度。上限は事故時の暴走止め。
    request_queue_size = 32


def main() -> int:
    ap = argparse.ArgumentParser(
        description="号機マイク音声の集約ハブ（単一ポート・パスで号機選択）")
    ap.add_argument("--host", default="0.0.0.0", help="待受アドレス")
    ap.add_argument("--port", type=int, default=8770, help="待受ポート（既定 8770）")
    ap.add_argument("--unit", action="append", default=[],
                    help="事前宣言する号機 ID（省略可・複数可。"
                         "宣言しなくても push が来た時点で自動登録される）")
    ap.add_argument("--outdir", default="~/kk_ws/logs/mic-recordings",
                    help="録音の出力先")
    ap.add_argument("--segment", type=int, default=60,
                    help="1 WAV あたりの秒数（既定 60）")
    ap.add_argument("--retention-hours", type=float, default=24.0,
                    help="この時間より古い録音を消す（0 で無効）")
    ap.add_argument("--max-gb", type=float, default=8.0,
                    help="号機ごとの録音の総容量上限 GB（0 で無効）")
    ap.add_argument("--no-record", action="store_true", help="録音しない")
    args = ap.parse_args()

    for name in args.unit:
        if not UNIT_RE.match(name):
            print(f"bad --unit {name!r}", file=sys.stderr)
            return 2

    hub = Hub(args)
    Handler.hub = hub
    srv = Server((args.host, args.port), Handler)

    log(f"mic hub listening on http://{args.host}:{args.port}  "
        f"(rate={SAMPLE_RATE} mono S16LE, "
        f"{'recording -> ' + hub.outdir if hub.record else 'no recording'})")
    log(f"  publisher : POST http://<hub>:{args.port}/ingest/<unit>")
    log(f"  listener  : GET  http://<hub>:{args.port}/<unit>")

    stopping = threading.Event()

    def shutdown(_sig, _frm):
        if stopping.is_set():
            return
        stopping.set()
        log("shutting down")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        srv.serve_forever(poll_interval=0.3)
    finally:
        srv.server_close()
        hub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
