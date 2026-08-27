#!/usr/bin/env python3
# =============================================================================
#  mic_publisher.py — 号機(Pi)のマイク音声を集約ハブへ push する送信側
# -----------------------------------------------------------------------------
#  経路:
#      arecord(ALSA, 48kHz/mono/S16LE)
#        → 3:1 デシメーション(アンチエイリアス FIR + 間引き, numpy)
#        → 16kHz/mono/S16LE の生 PCM
#        → HTTP chunked POST http://<hub>:8770/ingest/<unit>
#
#  設計の要点:
#    - **GStreamer を使わない**。ALSA からの取り込みは `arecord`(alsa-utils)
#      だけ、送信は Python 標準ライブラリの http.client だけ。
#    - **号機側から push する**。ハブは待ち受けるだけなので、号機の IP が
#      変わってもハブの設定は不要。起動順にも依存しない(繋がるまで再試行)。
#    - **16kHz へは送信段で落とす**。解析側(damiyan-detector)が最終的に
#      16kHz を使うので、そこまでの経路を全部 16kHz にすると
#      16000*2 = 32000 B/s = **256 kbps**。旧構成の 48kHz FLAC(約 437 kbps)
#      より軽く、しかもコーデックが要らない。
#    - **リサンプルは自前の FIR で行う**。`arecord -r 16000`(ALSA plug の
#      自動変換)はビルドによって線形補間になり、16kHz 超の成分が可聴帯へ
#      折り返す。ダミヤンの周波数判定はその折り返しノイズに弱いので、
#      きちんと帯域制限してから間引く。
#
#  使い方:
#    python3 publisher/mic_publisher.py --hub http://192.168.10.3:8770 --unit 5
#    # 録音デバイスは arecord -l で確認（USB マイクは通常 hw:1,0）
#
#  依存:
#    alsa-utils (arecord) と python3-numpy。
#    `--capture-rate 16000` を使う場合は numpy 不要（ALSA 側で変換される）。
#    `--source tone|wav` のテスト用ソースも numpy 不要。
# =============================================================================
from __future__ import annotations

import argparse
import http.client
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from array import array
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

# ---- ワイヤ契約（hub/mic_hub.py と一致させること）---------------------------
PROTOCOL_VERSION = "1"
OUT_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
OUT_BYTES_PER_SEC = OUT_RATE * CHANNELS * SAMPLE_WIDTH

# 1 チャンク = 100 ms。ハブのキュー段数(6.4 秒相当)とレベル計の窓もこれ基準。
CHUNK_MS = 100
OUT_CHUNK_BYTES = OUT_BYTES_PER_SEC * CHUNK_MS // 1000     # 3200 B

# 再接続の待ち時間（秒）。掴めないハブに対して CPU を回さない程度に間引く。
BACKOFF_SEC = (1.0, 2.0, 5.0)

# ハブが 409 を返したとき＝その号機は別の publisher が現に送信中。
# 原因は 2 通りあり、待ち時間の正解が逆になる:
#   (a) Wi-Fi 断でハブ側に残ったゾンビ接続がまだ「沈黙 3 秒未満」だった
#       → すぐ再試行すべき（数秒で奪取できる）
#   (b) systemd と手動起動などの二重起動で、相手は本当に送信し続けている
#       → 叩き続けても無駄なので長く待つ
# 最初の数回だけ短く、続くようなら (b) と判断して間隔を伸ばす。
CONFLICT_BACKOFF_SEC = (2.0, 3.0, 5.0, 15.0)

# arecord の stderr をここまで溜めて、異常終了時に原因として出す。
STDERR_KEEP_LINES = 12

_stop = threading.Event()


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [pub] {msg}", flush=True)


# ---- 48k → 16k デシメーション ------------------------------------------------
class Decimator:
    """整数比のアンチエイリアス付きダウンサンプラ（numpy が要る）。

    窓関数法で作った線形位相 FIR ローパスを畳み込んでから間引く。
    ブロック境界をまたいでも波形が繋がるよう、直前の入力を状態として保持する。
    """

    def __init__(self, factor: int, taps: int = 97):
        import numpy as np                  # 遅延 import（16kHz 直取りなら不要）
        self.np = np
        self.factor = factor

        # カットオフは出力ナイキストの 0.9 倍。入力レート基準の正規化周波数で
        # 0.45/factor（0.5 が入力ナイキスト）。
        fc = 0.45 / factor
        k = np.arange(taps) - (taps - 1) / 2.0
        h = 2 * fc * np.sinc(2 * fc * k) * np.hamming(taps)
        self.h = (h / h.sum()).astype(np.float64)

        self.state = np.zeros(taps - 1, dtype=np.float64)
        self.offset = 0                     # 次に残すサンプルのブロック内位置

    def __call__(self, pcm: bytes) -> bytes:
        np = self.np
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if x.size == 0:
            return b""
        xx = np.concatenate([self.state, x])
        y = np.convolve(xx, self.h, mode="valid")       # len(y) == len(x)
        self.state = xx[-(self.h.size - 1):]

        out = y[self.offset::self.factor]
        self.offset = (self.offset - y.size) % self.factor

        np.clip(out, -32768.0, 32767.0, out=out)
        return np.rint(out).astype("<i2").tobytes()


# ---- 音源 --------------------------------------------------------------------
class Source:
    """16kHz/mono/S16LE のチャンクを産む音源の共通インタフェース。"""

    name = "source"

    def chunks(self):
        raise NotImplementedError

    def close(self) -> None:
        pass

    def diagnostics(self) -> str:
        return ""


class AlsaSource(Source):
    """arecord を起こして生 PCM を読み、必要なら 16kHz へ落とす。"""

    def __init__(self, device: str, capture_rate: int, extra: str):
        self.device = device
        self.capture_rate = capture_rate
        self.extra = extra
        self.name = f"arecord {device} @{capture_rate}"
        self.proc: subprocess.Popen | None = None
        self._stderr: deque = deque(maxlen=STDERR_KEEP_LINES)

        if capture_rate == OUT_RATE:
            self.decim = None
            self.in_chunk = OUT_CHUNK_BYTES
        else:
            if capture_rate % OUT_RATE:
                raise SystemExit(
                    f"--capture-rate {capture_rate} は {OUT_RATE} の整数倍で"
                    f"なければならない（例 48000 / 32000 / 16000）")
            factor = capture_rate // OUT_RATE
            try:
                self.decim = Decimator(factor)
            except ImportError as e:
                raise SystemExit(
                    "numpy が無いのでリサンプルできない。"
                    "`sudo apt install python3-numpy` を入れるか、"
                    "`--capture-rate 16000`（ALSA 側で変換）を使う。"
                    f" ({e})") from e
            self.in_chunk = OUT_CHUNK_BYTES * factor

    def _argv(self) -> list[str]:
        argv = ["arecord", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                "-c", str(CHANNELS), "-r", str(self.capture_rate), "-q"]
        argv += self.extra.split()
        return argv

    def chunks(self):
        argv = self._argv()
        log(f"exec: {' '.join(argv)}")
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError as e:
            raise SystemExit(
                "arecord が見つからない。`sudo apt install alsa-utils`") from e

        drain = threading.Thread(target=self._drain_stderr, daemon=True)
        drain.start()

        assert self.proc.stdout is not None
        while not _stop.is_set():
            data = self.proc.stdout.read(self.in_chunk)
            if not data:
                return                       # arecord が落ちた → 上位が再起動
            if len(data) < self.in_chunk:    # 端数はそのまま流す（EOF 直前のみ）
                data = data[: len(data) - (len(data) % SAMPLE_WIDTH)]
                if not data:
                    return
            yield self.decim(data) if self.decim else data

    def _drain_stderr(self) -> None:
        # stderr を読み続けないと OS のパイプバッファが埋まって arecord が
        # 止まる。原因表示のため直近の行だけ残す。
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)

    def diagnostics(self) -> str:
        return " | ".join(self._stderr)

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
        except OSError:
            pass
        for pipe in (p.stdout, p.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


class ToneSource(Source):
    """マイク無しで疎通確認するための正弦波（numpy も arecord も不要）。"""

    def __init__(self, hz: float, amplitude: float = 0.3):
        self.hz = hz
        self.amp = amplitude
        self.name = f"tone {hz:g}Hz"

    def chunks(self):
        n = OUT_CHUNK_BYTES // SAMPLE_WIDTH
        phase = 0.0
        step = 2 * math.pi * self.hz / OUT_RATE
        peak = int(32767 * self.amp)
        next_at = time.monotonic()
        while not _stop.is_set():
            buf = array("h", bytes(OUT_CHUNK_BYTES))
            for i in range(n):
                buf[i] = int(peak * math.sin(phase))
                phase += step
            phase %= 2 * math.pi
            if sys.byteorder == "big":
                buf.byteswap()
            next_at += CHUNK_MS / 1000.0     # 実時間ペースで送る
            delay = next_at - time.monotonic()
            if delay > 0:
                _stop.wait(delay)
            yield buf.tobytes()


class WavSource(Source):
    """16kHz/mono の WAV を実時間で繰り返し再生する（回帰テスト用）。"""

    def __init__(self, path: str, loop: bool = True):
        self.path = path
        self.loop = loop
        self.name = f"wav {os.path.basename(path)}"

    def chunks(self):
        while not _stop.is_set():
            with wave.open(self.path, "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != \
                        (OUT_RATE, CHANNELS, SAMPLE_WIDTH):
                    raise SystemExit(
                        f"{self.path} は {OUT_RATE}Hz/mono/16bit ではない "
                        f"({w.getframerate()}Hz/{w.getnchannels()}ch/"
                        f"{w.getsampwidth() * 8}bit)")
                next_at = time.monotonic()
                while not _stop.is_set():
                    frames = w.readframes(OUT_CHUNK_BYTES // SAMPLE_WIDTH)
                    if not frames:
                        break
                    next_at += CHUNK_MS / 1000.0
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        _stop.wait(delay)
                    yield frames
            if not self.loop:
                return


# ---- ハブへの送出 -------------------------------------------------------------
def publish_once(hub_url: str, unit: str, source: Source, timeout: float) -> str:
    """1 回ぶんの接続。切れた理由を文字列で返す（例外は投げない）。

    http.client は本文がイテラブルで Content-Length が無いとき自動的に
    `Transfer-Encoding: chunked` にする。ジェネレータを渡しているので
    request() は送り続けている間ずっと戻ってこない＝それが定常状態。
    """
    u = urlparse(hub_url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if not host:
        raise SystemExit(f"--hub の URL が不正: {hub_url!r}")
    path = f"{u.path.rstrip('/')}/ingest/{unit}"

    conn_cls = (http.client.HTTPSConnection if u.scheme == "https"
                else http.client.HTTPConnection)
    conn = conn_cls(host, port, timeout=timeout)
    headers = {
        "Content-Type": f"audio/L16; rate={OUT_RATE}; channels={CHANNELS}",
        "X-Mic-Protocol": PROTOCOL_VERSION,
        "X-Mic-Rate": str(OUT_RATE),
        "X-Mic-Channels": str(CHANNELS),
        "X-Mic-Format": "S16LE",
        "X-Mic-Source": f"{socket.gethostname()}:{source.name}",
    }

    sent = 0
    started = time.monotonic()

    def body():
        nonlocal sent
        for chunk in source.chunks():
            if _stop.is_set():
                return
            sent += len(chunk)
            yield chunk

    try:
        log(f"connecting http://{host}:{port}{path} ({source.name})")
        conn.request("POST", path, body=body(), headers=headers)
        # ここに来るのは音源が尽きたとき（tone/wav の loop=False、arecord 死亡）
        try:
            resp = conn.getresponse()
            reason = f"stream ended, hub said {resp.status} {resp.reason}"
            resp.read()
        except OSError as e:
            reason = f"stream ended ({type(e).__name__}: {e})"
    except (BrokenPipeError, ConnectionResetError, socket.timeout,
            http.client.HTTPException, OSError) as e:
        reason = f"{type(e).__name__}: {e}"
        early = peek_response(conn)
        if early:
            # ハブが 400/404 などを即返して切ったケース。設定ミスの本命。
            reason = f"hub rejected: {early}"
    finally:
        source.close()
        try:
            conn.close()
        except OSError:
            pass

    secs = sent / OUT_BYTES_PER_SEC if sent else 0.0
    wall = time.monotonic() - started
    log(f"disconnected after {secs:.1f}s audio / {wall:.1f}s wall — {reason}")
    if sent and wall > 5 and abs(wall - secs) > 0.05 * wall:
        # 送出量と経過時間がずれる＝取りこぼしか詰まり。気づけるようにしておく。
        log(f"WARNING: audio/wall drift {secs - wall:+.1f}s "
            f"(overrun や CPU 不足の疑い)")
    diag = source.diagnostics()
    if diag:
        log(f"source stderr: {diag}")
    return reason


def peek_response(conn: http.client.HTTPConnection) -> str:
    """送信失敗時、ハブが先に返していたエラー応答を拾えるなら拾う。"""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return ""
    try:
        sock.setblocking(False)
        data = sock.recv(512)
    except OSError:
        return ""
    if not data:
        return ""
    text = data.decode("utf-8", "replace")
    head, _, rest = text.partition("\r\n\r\n")
    status = head.splitlines()[0] if head else ""
    return f"{status} {rest.strip()}".strip()


def build_source(args: argparse.Namespace) -> Source:
    if args.source == "tone":
        return ToneSource(args.tone_hz)
    if args.source == "wav":
        if not args.wav:
            raise SystemExit("--source wav には --wav <path> が要る")
        return WavSource(args.wav, loop=not args.no_loop)
    return AlsaSource(args.device, args.capture_rate, args.arecord_extra)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="号機マイクの音声を集約ハブへ push する（16kHz/mono/S16LE）")
    ap.add_argument("--hub", required=True,
                    help="集約ハブの URL（例 http://192.168.10.3:8770）")
    ap.add_argument("--unit", required=True,
                    help="号機 ID。ハブ側のパスになる（例 5 → GET /5）")
    ap.add_argument("--device", default="hw:1,0",
                    help="ALSA 録音デバイス（arecord -l で確認。既定 hw:1,0）")
    ap.add_argument("--capture-rate", type=int, default=48000,
                    help="arecord の取り込みレート。16000 の整数倍。"
                         "16000 なら numpy 不要（ALSA が変換）")
    ap.add_argument("--arecord-extra", default="--buffer-time=200000 --period-time=50000",
                    help="arecord へ追加で渡す引数。空文字で無効化")
    ap.add_argument("--source", choices=("alsa", "tone", "wav"), default="alsa",
                    help="音源。tone/wav はマイク無しの疎通確認用")
    ap.add_argument("--tone-hz", type=float, default=440.0,
                    help="--source tone の周波数")
    ap.add_argument("--wav", help="--source wav で流す 16kHz/mono/16bit WAV")
    ap.add_argument("--no-loop", action="store_true",
                    help="--source wav を繰り返さず 1 周で終了する")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="ハブへの接続・送信タイムアウト秒")
    ap.add_argument("--once", action="store_true",
                    help="切断時に再接続せず終了する（テスト用）")
    args = ap.parse_args()

    def stop(_sig, _frm):
        if not _stop.is_set():
            log("stopping")
            _stop.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    attempt = 0
    conflicts = 0
    while not _stop.is_set():
        source = build_source(args)
        t0 = time.monotonic()
        reason = publish_once(args.hub, args.unit, source, args.timeout)
        if args.once or _stop.is_set():
            break
        # ひとしきり流せていたなら「復帰した」とみなしてバックオフを戻す。
        # 落ちっぱなしのときだけ間隔が伸びるようにする。
        if time.monotonic() - t0 >= 30:
            attempt = 0
        if "409" in reason:
            delay = CONFLICT_BACKOFF_SEC[min(conflicts, len(CONFLICT_BACKOFF_SEC) - 1)]
            conflicts += 1
            if conflicts == len(CONFLICT_BACKOFF_SEC):
                log(f"unit {args.unit} は別の publisher が送信し続けている。"
                    "二重起動（systemd と手動起動の重複など）を確認すること")
        else:
            conflicts = 0
            delay = BACKOFF_SEC[min(attempt, len(BACKOFF_SEC) - 1)]
            attempt += 1
        log(f"reconnecting in {delay:g}s")
        _stop.wait(delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
