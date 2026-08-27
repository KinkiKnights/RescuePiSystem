#!/usr/bin/env python3
# =============================================================================
#  mic_receiver.py — Pi から FLAC(TCP) で届く音声を受け取り numpy で解析する（受信側）
# -----------------------------------------------------------------------------
#  経路: tcpclientsrc → flacparse → flacdec → appsink → numpy(int16)
#    - GStreamer(C) が受信・FLAC可逆デコードを行い、Python は復元済み PCM を numpy で受け取る。
#    - FLAC は可逆なので、Pi のマイクが出した S16LE サンプルがビット完全に再現される。
#    - 解析は analyze() に書く（既定は RMS レベル[dBFS] と主要周波数[Hz] を表示）。
#
#  必要パッケージ(Ubuntu 受信側):
#    sudo apt install python3-gi gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
#                     gstreamer1.0-tools python3-numpy
#
#  使い方:
#    python3 mic_receiver.py --host <PiのIP> --port 5005 --rate 48000
# =============================================================================
import argparse
import sys
import time

import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

Gst.init(None)


# ---- 解析部（ここを書き換える）---------------------------------------------
class Analyzer:
    """復元された音声フレーム(numpy int16, mono)を受け取って解析する。

    appsink から来たブロックを溜め、HOP サンプルごとに analyze() を呼ぶ。
    """
    def __init__(self, rate, hop_sec=0.5):
        self.rate = rate
        self.hop = int(rate * hop_sec)
        self._buf = np.empty(0, dtype=np.int16)

    def feed(self, samples):
        self._buf = np.concatenate([self._buf, samples])
        while len(self._buf) >= self.hop:
            self.analyze(self._buf[:self.hop])
            self._buf = self._buf[self.hop:]

    def analyze(self, frame):
        # 例: RMS レベル(dBFS) と 主要周波数(Hz)
        x = frame.astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(x * x)) + 1e-12
        dbfs = 20.0 * np.log10(rms)
        win = x * np.hanning(len(x))
        spec = np.abs(np.fft.rfft(win))
        freqs = np.fft.rfftfreq(len(x), 1.0 / self.rate)
        peak_hz = freqs[int(np.argmax(spec))]
        bar = "#" * int(np.clip((dbfs + 60) / 60 * 40, 0, 40))
        print(f"level={dbfs:6.1f} dBFS  peak={peak_hz:6.0f} Hz  |{bar:<40}|", flush=True)


# ---- GStreamer 受信 ---------------------------------------------------------
class Receiver:
    def __init__(self, host, port, rate, analyzer):
        self.host = host
        self.port = port
        self.rate = rate
        self.analyzer = analyzer
        self.pipe = None
        self.loop = GLib.MainLoop()

    def _build(self):
        desc = (
            f"tcpclientsrc host={self.host} port={self.port} ! "
            "flacparse ! flacdec ! "
            "audioconvert ! "
            f"audio/x-raw,format=S16LE,channels=1,rate={self.rate} ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=20 drop=false"
        )
        self.pipe = Gst.parse_launch(desc)
        sink = self.pipe.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            samples = np.frombuffer(info.data, dtype=np.int16).copy()
            buf.unmap(info)
            self.analyzer.feed(samples)
        return Gst.FlowReturn.OK

    def _on_bus(self, _bus, msg):
        # 切断・エラー時はパイプラインを畳んで再接続ループに戻す
        if msg.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                print(f"[recv] error: {err} ({dbg})", file=sys.stderr, flush=True)
            else:
                print("[recv] stream ended", file=sys.stderr, flush=True)
            self.loop.quit()

    def run_forever(self):
        # 送信側(Pi)が落ちても再接続を試み続ける
        while True:
            try:
                print(f"[recv] connecting tcp://{self.host}:{self.port} ...", flush=True)
                self._build()
                self.pipe.set_state(Gst.State.PLAYING)
                self.loop = GLib.MainLoop()
                self.loop.run()
            except KeyboardInterrupt:
                break
            finally:
                if self.pipe is not None:
                    self.pipe.set_state(Gst.State.NULL)
                    self.pipe = None
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="FLAC/TCP マイク音声を受信して解析")
    ap.add_argument("--host", required=True, help="送信側 Pi の IP")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--rate", type=int, default=48000, help="送信側 RATE と合わせる")
    args = ap.parse_args()

    analyzer = Analyzer(args.rate)
    Receiver(args.host, args.port, args.rate, analyzer).run_forever()


if __name__ == "__main__":
    main()
