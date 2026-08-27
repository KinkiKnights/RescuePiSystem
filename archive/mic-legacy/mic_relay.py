#!/usr/bin/env python3
# =============================================================================
#  mic_relay.py — 号機(Pi)群の FLAC/TCP 配信を「中継 & 常時録音」する中継サーバ
# -----------------------------------------------------------------------------
#  役割:
#    1) 各号機(publisher=tcpserversink)へクライアントとして接続してFLACストリームを取り込む
#    2) 号機ごとに「別々の下流TCPポート」で再配信する（tcpserversink）
#         → 解析プログラム(mic_receiver.py)は --port を変えるだけで号機を選択できる
#         → 1ポートに複数の解析PCが同時接続でき、途中参加(late-join)も可能
#    3) 号機ごとに10秒単位の WAV を常時録音する（解析PCの接続有無に関係なく）
#    4) 上流が切れた号機だけを自動再接続する（他号機・プロセス全体は落とさない）
#
#  経路(号機ごと):
#      tcpclientsrc(号機) → flacparse → tee ┬→ queue → tcpserversink(下流再配信)
#                                           └→ queue → flacdec → audioconvert
#                                                     → S16LE → appsink(録音)
#
#    - flacparse の出力には streamheader が付くため、tcpserversink はそれを
#      各クライアント（途中参加含む）へ先頭配信する。よって未改変の
#      mic_receiver.py が号機直結時と全く同じようにデコードできる。
#    - FLAC は可逆圧縮なので、appsink で得た S16LE は元マイクのPCMとビット完全一致。
#      それをそのまま wave(標準ライブラリ)で WAV 化するので追加依存は不要。
#
#  必要パッケージ(Ubuntu):
#    sudo apt install python3-gi python3-numpy gstreamer1.0-plugins-base \
#                     gstreamer1.0-plugins-good gstreamer1.0-tools
#    （relay 自体は numpy 不要。解析側 mic_receiver.py が numpy を使う）
#
#  使い方(既定: 号機3/4/5 を上流5005・下流5003/5004/5005 で中継):
#    python3 mic_relay.py \
#        --unit 3:pi3.local:5005:5003 \
#        --unit 4:pi4.local:5005:5004 \
#        --unit 5:pi5.local:5005:5005 \
#        --rate 48000 --outdir ./recordings --segment 10
#
#  上流フェイルオーバー(2026-08 追加):
#    ホスト部に "auto" を指定すると号機Nの標準4候補
#      192.168.10.11N(ドングル) > 192.168.10.13N(内蔵無線) >
#      192.168.10.12N(有線)     > kk0N.local(mDNS)
#    へ優先順に TCP プローブして到達した候補に接続する。カンマ区切りの
#    明示リスト("h1,h2,...")も可。切断時の再接続でも同様に候補選択する
#    (直近成功ホストを最優先、全滅時は先頭から試し直し)。
#    例: python3 mic_relay.py --unit 3:auto:5005:5003 --unit 5:auto:5005:5005 --no-record
# =============================================================================
import argparse
import os
import signal
import socket
import sys
import time
import wave
from datetime import datetime

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

Gst.init(None)


def log(msg):
    """時刻付きの標準出力ログ（号機タグは呼び出し側で付ける）。"""
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# ---- 号機1台ぶんの中継パイプライン -----------------------------------------
class UnitRelay:
    """1号機ぶんの取り込み→再配信→録音を担うGStreamerパイプライン。

    上流切断時は自分のパイプラインだけを畳んで一定間隔で再構築する
    （他号機や全体のmainループには影響しない）。
    """

    def __init__(self, unit, pub_hosts, pub_port, down_port,
                 rate, outdir, segment_sec, reconnect_sec=2, record=True,
                 probe_timeout=1.0):
        self.unit = unit                    # 号機番号(表示用)
        # 上流(号機)ホスト候補リスト(優先順)。str 1個でも受ける(後方互換)。
        if isinstance(pub_hosts, str):
            pub_hosts = [pub_hosts]
        self.pub_hosts = list(pub_hosts)
        self.pub_host = self.pub_hosts[0]   # 現在選択中の上流ホスト
        self.preferred_host = None          # 直近接続に成功したホスト(次回優先)
        self.probe_timeout = probe_timeout  # 候補TCPプローブのタイムアウト秒
        self.pub_port = pub_port            # 上流(号機)ポート
        self.down_port = down_port          # 下流(解析PC向け)再配信ポート
        self.rate = rate                    # サンプリングレート
        self.outdir = outdir                # 録音出力ディレクトリ
        self.segment_sec = segment_sec      # 1WAVあたりの秒数
        self.reconnect_sec = reconnect_sec  # 再接続までの待ち秒数
        self.record = record                # 録音WAV書き出しの有効/無効(--no-recordで無効)

        # 1セグメント分のバイト数（mono・16bit=2byte）
        self.segment_bytes = rate * segment_sec * 2
        self._rec_buf = bytearray()         # 録音用の未書き出しPCMバッファ

        self.pipe = None
        self._bus = None
        self._reconnect_pending = False     # 再接続の二重スケジュール防止
        self._stopping = False              # シャットダウン中フラグ

    def tag(self, msg):
        return f"[unit{self.unit}] {msg}"

    # ---- パイプライン構築/起動 ------------------------------------------
    def _build(self):
        # tee で「下流再配信」と「録音デコード」の2系統に分岐する。
        # sync=false: 解析はリアルタイム性より確実な全サンプル配送を優先。
        # 系統A(下流再配信=解析/viewer向け)は常に構築する。
        # 系統B(録音WAV書き出し)は self.record が真のときだけ追加する。
        desc = (
            f"tcpclientsrc host={self.pub_host} port={self.pub_port} "
            f"    name=src ! "
            "flacparse ! tee name=t "
            # 系統A: 下流へそのまま再配信（未改変の受信側がそのままデコード可）
            "t. ! queue ! "
            f"    tcpserversink host=0.0.0.0 port={self.down_port} "
            "        sync=false recover-policy=keyframe "
        )
        if self.record:
            desc += (
                # 系統B: 録音用にデコードして生PCM(S16LE)を appsink へ
                "t. ! queue ! flacdec ! audioconvert ! "
                f"    audio/x-raw,format=S16LE,channels=1,rate={self.rate} ! "
                "    appsink name=rec emit-signals=true sync=false "
                "        max-buffers=50 drop=false"
            )
        self.pipe = Gst.parse_launch(desc)

        if self.record:
            rec = self.pipe.get_by_name("rec")
            rec.connect("new-sample", self._on_sample)

        self._bus = self.pipe.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._on_bus)

    # ---- フェイルオーバー: 候補ホストをTCPプローブして到達先を選ぶ ---------
    def _select_host(self):
        """候補を優先順にTCPプローブし、最初に到達できたホストを返す。

        直近接続に成功したホスト(preferred_host)があればそれを最優先で試し、
        ダメなら定義順(ドングル > 内蔵無線 > 有線 > mDNS)に戻って全候補を試す。
        全滅なら None(呼び出し側が reconnect_sec 後に再試行)。
        """
        order = list(self.pub_hosts)
        if self.preferred_host in order:
            order.remove(self.preferred_host)
            order.insert(0, self.preferred_host)
        for host in order:
            try:
                with socket.create_connection((host, self.pub_port),
                                              timeout=self.probe_timeout):
                    pass
                log(self.tag(f"probe OK: {host}:{self.pub_port}"))
                return host
            except OSError as e:
                log(self.tag(f"probe NG: {host}:{self.pub_port} ({e})"))
        return None

    def start(self):
        host = self._select_host()
        if host is None:
            log(self.tag(f"no reachable upstream among {self.pub_hosts} "
                         f"-> retrying"))
            # 全候補不達: 次回は先頭(最優先候補)から試し直す
            self.preferred_host = None
            self._schedule_reconnect()
            return
        self.pub_host = host
        self.preferred_host = host
        log(self.tag(f"connecting upstream tcp://{self.pub_host}:{self.pub_port} "
                     f"-> serving on :{self.down_port}"))
        try:
            self._build()
            self.pipe.set_state(Gst.State.PLAYING)
        except Exception as e:  # 構築失敗時も再接続ループに乗せる
            log(self.tag(f"build failed: {e}"))
            self._schedule_reconnect()

    # ---- 録音: appsink から復元PCMを受け取り、10秒ごとにWAV化 --------------
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            self._rec_buf += bytes(info.data)  # 復元済みS16LEを溜める
            buf.unmap(info)
            # segment_bytes 溜まるごとに1ファイル書き出す（正確に10秒区切り）
            while len(self._rec_buf) >= self.segment_bytes:
                chunk = self._rec_buf[:self.segment_bytes]
                del self._rec_buf[:self.segment_bytes]
                self._write_wav(chunk)
        return Gst.FlowReturn.OK

    def _write_wav(self, pcm_bytes):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.outdir, f"rec_unit{self.unit}_{ts}.wav")
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)      # mono
                w.setsampwidth(2)      # 16bit
                w.setframerate(self.rate)
                w.writeframes(pcm_bytes)
            log(self.tag(f"wrote {os.path.basename(path)} "
                         f"({len(pcm_bytes)//2} samples)"))
        except Exception as e:
            log(self.tag(f"WAV write error: {e}"))

    # ---- バス監視: 上流切断/エラーで自号機だけ再接続 --------------------
    def _on_bus(self, _bus, msg):
        if self._stopping:
            return
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(self.tag(f"upstream error: {err} ({dbg}) -> reconnecting"))
            self._schedule_reconnect()
        elif t == Gst.MessageType.EOS:
            log(self.tag("upstream EOS -> reconnecting"))
            self._schedule_reconnect()

    def _teardown(self):
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None

    def _schedule_reconnect(self):
        if self._reconnect_pending or self._stopping:
            return
        self._reconnect_pending = True
        self._teardown()
        log(self.tag(f"reconnecting in {self.reconnect_sec}s ..."))
        # mainループ上でN秒後に再構築（他号機をブロックしない）
        GLib.timeout_add_seconds(self.reconnect_sec, self._do_reconnect)

    def _do_reconnect(self):
        self._reconnect_pending = False
        if self._stopping:
            return False
        self.start()
        return False  # ワンショット

    def stop(self):
        self._stopping = True
        self._teardown()


# ---- 設定パース -------------------------------------------------------------
def expand_hosts(n, host_field):
    """--unit のホスト部を候補ホストリスト(優先順)に展開する。

    - "auto"                → 号機Nの標準4候補
                              (ドングル .11N > 内蔵無線 .13N > 有線 .12N > kk0N.local)
    - "h1,h2,..."(カンマ)  → 指定順の候補リスト
    - "host"(単一)         → その1候補のみ(従来互換)
    """
    if host_field == "auto":
        return [
            f"192.168.10.{110 + n}",   # USBドングル
            f"192.168.10.{130 + n}",   # 内蔵無線
            f"192.168.10.{120 + n}",   # 有線
            f"kk0{n}.local",           # mDNS
        ]
    hosts = [h.strip() for h in host_field.split(",") if h.strip()]
    if not hosts:
        raise argparse.ArgumentTypeError(
            f"--unit のホスト指定が空です: {host_field!r}")
    return hosts


def parse_unit(spec, default_pub_port):
    """--unit の値をパースする。

    受理形式:
      "N:pub_host:pub_port:down_port"  (完全指定)
      "N:pub_host:down_port"           (pub_port は既定を使用)
      "N:pub_host"                     (pub_port=既定, down_port=500N)
    pub_host は "auto" / "h1,h2,..."(カンマ区切り) / 単一ホスト のいずれか。
    """
    parts = spec.split(":")
    if len(parts) == 4:
        n, host, pub_port, down_port = parts
        return int(n), expand_hosts(int(n), host), int(pub_port), int(down_port)
    if len(parts) == 3:
        n, host, down_port = parts
        return int(n), expand_hosts(int(n), host), default_pub_port, int(down_port)
    if len(parts) == 2:
        n, host = parts
        return int(n), expand_hosts(int(n), host), default_pub_port, 5000 + int(n)
    raise argparse.ArgumentTypeError(
        f"--unit の形式が不正: {spec!r} "
        "(N:host[:pub_port][:down_port])")


def build_default_units(pub_host, default_pub_port):
    """--unit 未指定時の既定: 号機3/4/5 を下流5003/5004/5005 で中継。"""
    return [
        (3, [pub_host], default_pub_port, 5003),
        (4, [pub_host], default_pub_port, 5004),
        (5, [pub_host], default_pub_port, 5005),
    ]


def main():
    ap = argparse.ArgumentParser(
        description="号機のFLAC/TCP配信を号機別ポートで再配信し常時録音する中継サーバ")
    ap.add_argument(
        "--unit", action="append", default=[],
        help="号機定義 'N:pub_host[:pub_port][:down_port]'。複数回指定可。"
             "pub_host は 'auto'(号機Nの標準4候補 .11N>.13N>.12N>kk0N.local に"
             "フェイルオーバー) / 'h1,h2,...'(カンマ区切り候補) / 単一ホスト。"
             "省略時は 3/4/5 を --pub-host の :--pub-port から下流5003/5004/5005へ")
    ap.add_argument("--pub-host", default="127.0.0.1",
                    help="既定号機群の上流ホスト(--unit省略時に使用)")
    ap.add_argument("--pub-port", type=int, default=5005,
                    help="上流ポート既定値(号機のtcpserversink既定=5005)")
    ap.add_argument("--rate", type=int, default=48000,
                    help="サンプリングレート(号機のRATEと合わせる)")
    ap.add_argument("--outdir", default="./recordings",
                    help="録音WAVの出力ディレクトリ")
    ap.add_argument("--segment", type=int, default=10,
                    help="1WAVあたりの秒数(既定10)")
    ap.add_argument("--reconnect", type=int, default=2,
                    help="上流切断時の再接続待ち秒数")
    ap.add_argument("--probe-timeout", type=float, default=1.0,
                    help="上流候補ホストのTCPプローブのタイムアウト秒(既定1.0)")
    ap.add_argument("--no-record", dest="record", action="store_false",
                    help="録音WAVの書き出しを無効化する(下流再配信=解析/viewerは維持)。"
                         "既定は録音ON(後方互換)。")
    ap.set_defaults(record=True)
    args = ap.parse_args()

    if args.unit:
        units = [parse_unit(s, args.pub_port) for s in args.unit]
    else:
        units = build_default_units(args.pub_host, args.pub_port)

    if args.record:
        os.makedirs(args.outdir, exist_ok=True)
    log(f"[relay] outdir={os.path.abspath(args.outdir)} "
        f"rate={args.rate} segment={args.segment}s "
        f"record={'ON' if args.record else 'OFF (--no-record)'}")

    relays = []
    for n, hosts, pub_port, down_port in units:
        r = UnitRelay(n, hosts, pub_port, down_port,
                      args.rate, args.outdir, args.segment, args.reconnect,
                      record=args.record, probe_timeout=args.probe_timeout)
        relays.append(r)
        log(f"[relay] unit{n}: upstream candidates {hosts} :{pub_port} "
            f"-> downstream :{down_port}")

    for r in relays:
        r.start()

    loop = GLib.MainLoop()

    def shutdown(*_):
        log("[relay] SIGINT -> shutting down")
        for r in relays:
            r.stop()
        loop.quit()

    # SIGINT/SIGTERM でクリーン終了
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown)

    try:
        loop.run()
    except KeyboardInterrupt:
        shutdown()
    log("[relay] stopped")


if __name__ == "__main__":
    main()
