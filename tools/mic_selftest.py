#!/usr/bin/env python3
# =============================================================================
#  selftest.py — 実機・マイク無しで系全体を検証するスモークテスト
# -----------------------------------------------------------------------------
#  ハブを立て、号機 3/4/5 を模した publisher（それぞれ別周波数のトーン）を
#  push させ、下記をまとめて確認する:
#
#    1. パスで号機を選べていること（/3 /4 /5 が別々の音を返す）
#    2. ストリーミング WAV のヘッダが正しいこと
#    3. 1 号機に複数の購読者が同時に付けること
#    4. オフライン号機は 503、未知パスは 404 を返すこと
#    5. 同じ号機への二重 publisher が 409 で弾かれ、現職が無傷なこと
#    6. 号機ごとの WAV が実際に録音されていること
#
#  周波数の判定は Goertzel 法を手書きしているので numpy も要らない。
#  依存は Python 標準ライブラリだけ。
#
#  使い方:  python3 tools/mic_selftest.py          （成功なら rc=0）
# =============================================================================
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HUB = os.path.join(ROOT, "server", "mic_hub", "mic_hub.py")
PUB = os.path.join(ROOT, "robot", "mic_publisher", "mic_publisher.py")
PORT = int(os.environ.get("SELFTEST_PORT", "8779"))
BASE = f"http://127.0.0.1:{PORT}"
UNITS = {"3": 660.0, "4": 880.0, "5": 1100.0}
RATE = 16000

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {label}{('  — ' + detail) if detail else ''}", flush=True)
    if not ok:
        failures.append(label)
    return ok


def goertzel(samples, hz: int | float, rate: int = RATE) -> float:
    """1 周波数ぶんのパワーを返す（FFT 不要・標準ライブラリのみ）。"""
    n = len(samples)
    k = int(0.5 + n * hz / rate)
    w = 2 * math.pi * k / n
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / (n * n)


def dominant(pcm: bytes, candidates) -> float:
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return max(candidates, key=lambda hz: goertzel(samples, hz))


def get(path: str, timeout: float = 5.0):
    req = urllib.request.Request(BASE + path)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (localhost)


def status() -> dict:
    with get("/api/status") as r:
        return json.load(r)


def read_stream(path: str, seconds: float) -> bytes:
    """指定秒ぶん読んで切る（ストリームは終端が来ないので自分で切る）。"""
    want = int(RATE * 2 * seconds)
    buf = b""
    with get(path, timeout=seconds + 5) as r:
        head = dict(r.headers)
        if path.endswith(".wav") or "format=raw" not in path:
            buf = r.read(44)                 # WAV ヘッダを先に読む
            if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
                raise AssertionError(f"bad WAV header: {buf[:12]!r}")
            fmt = struct.unpack("<HHIIHH", buf[20:36])
            if fmt[:4] != (1, 1, RATE, RATE * 2):
                raise AssertionError(f"bad fmt chunk: {fmt}")
            buf = b""
        while len(buf) < want:
            chunk = r.read(min(4096, want - len(buf)))
            if not chunk:
                break
            buf += chunk
    read_stream.last_headers = head          # 呼び出し側の追加検査用
    return buf


def wait_online(names, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        online = {u["unit"] for u in status()["units"] if u["online"]}
        if set(names) <= online:
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    outdir = tempfile.mkdtemp(prefix="mic-selftest-")
    procs: list[subprocess.Popen] = []
    print(f"hub port {PORT}, recordings -> {outdir}\n")

    try:
        procs.append(subprocess.Popen(
            [sys.executable, HUB, "--port", str(PORT), "--outdir", outdir,
             "--segment", "3", "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(50):
            try:
                with get("/healthz", timeout=1):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        else:
            print("hub did not start", file=sys.stderr)
            return 1

        print("1) 号機 3 台を push させる")
        for unit, hz in UNITS.items():
            procs.append(subprocess.Popen(
                [sys.executable, PUB, "--hub", BASE, "--unit", unit,
                 "--source", "tone", "--tone-hz", str(hz)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check(wait_online(UNITS), "3 台とも online になる")

        print("\n2) パスで号機を選べる（1 ポート・パス指定）")
        for unit, hz in UNITS.items():
            pcm = read_stream(f"/{unit}", 0.5)
            got = dominant(pcm, list(UNITS.values()))
            check(got == hz and len(pcm) >= RATE, f"GET /{unit} が {hz:g} Hz",
                  f"検出 {got:g} Hz / {len(pcm)} bytes")
            hdrs = read_stream.last_headers
            check(hdrs.get("X-Mic-Rate") == str(RATE)
                  and hdrs.get("Content-Type") == "audio/wav",
                  f"GET /{unit} のヘッダ",
                  f"{hdrs.get('Content-Type')} rate={hdrs.get('X-Mic-Rate')}")

        pcm = read_stream("/listen/4?format=raw", 0.5)
        check(dominant(pcm, list(UNITS.values())) == UNITS["4"],
              "GET /listen/4?format=raw（ヘッダ無し S16LE）")

        print("\n3) 同じ号機に購読者を 3 本同時に付ける")
        import threading
        got: dict[int, bytes] = {}

        def grab(i):
            got[i] = read_stream("/5", 0.4)
        ts = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        check(len(got) == 3 and all(len(v) >= RATE * 0.7 for v in got.values()),
              "3 本とも同じ音を受け取れる",
              " / ".join(str(len(v)) for v in got.values()))
        check(all(dominant(v, list(UNITS.values())) == UNITS["5"]
                  for v in got.values()), "3 本とも 1100 Hz")
        time.sleep(0.5)
        listeners = [u["listeners"] for u in status()["units"] if u["unit"] == "5"]
        check(listeners == [0], "切断後に listeners が 0 に戻る", str(listeners))

        print("\n4) 異常系")
        try:
            read_stream("/9", 0.1)
            check(False, "オフライン号機は 503")
        except urllib.error.HTTPError as e:
            check(e.code == 503, "オフライン号機は 503", f"http {e.code}")
        try:
            get("/nope.txt")
            check(False, "未知パスは 404")
        except urllib.error.HTTPError as e:
            check(e.code == 404, "未知パスは 404", f"http {e.code}")

        print("\n5) 二重 publisher は 409 で弾かれ、現職は無傷")
        before = next(u for u in status()["units"] if u["unit"] == "5")
        dup = subprocess.Popen(
            [sys.executable, PUB, "--hub", BASE, "--unit", "5",
             "--source", "tone", "--tone-hz", "440", "--once"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = dup.communicate(timeout=20)
        check("409" in out, "publisher が 409 を受け取る",
              out.strip().splitlines()[-1] if out.strip() else "(no output)")
        after = next(u for u in status()["units"] if u["unit"] == "5")
        check(after["disconnects"] == before["disconnects"]
              and after["uptime_sec"] > before["uptime_sec"],
              "現職の接続が切られていない",
              f"disconnects {before['disconnects']}->{after['disconnects']}")
        pcm = read_stream("/5", 0.3)
        check(dominant(pcm, [440.0, *UNITS.values()]) == UNITS["5"],
              "/5 の音が奪われていない")

        print("\n6) 号機ごとに録音されている")
        time.sleep(3.5)                      # セグメント(3s)が 1 本閉じるまで
        for unit in UNITS:
            d = os.path.join(outdir, f"unit{unit}")
            wavs = sorted(f for f in os.listdir(d)) if os.path.isdir(d) else []
            closed = [w for w in wavs
                      if os.path.getsize(os.path.join(d, w)) > 44]
            ok = bool(closed)
            detail = f"{len(wavs)} files"
            if ok:
                with wave.open(os.path.join(d, closed[0]), "rb") as w:
                    params = (w.getframerate(), w.getnchannels(),
                              w.getsampwidth())
                    frames = w.getnframes()
                ok = params == (RATE, 1, 2) and frames > 0
                detail = f"{closed[0]} {params} {frames} frames"
            check(ok, f"unit{unit} の WAV", detail)

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(outdir, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
