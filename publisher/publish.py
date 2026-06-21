#!/usr/bin/env python3
"""
WebRTCカメラパブリッシャ (Raspberry Pi 用 / GStreamer webrtcbin) — 複数カメラ + 切替対応

カメラ/スクリーン映像をH.264でエンコードし、relayサーバーへWebRTCで送信する。
パブリッシャは offerer (webrtcbinがオファーを生成)。

特徴:
  - 接続時に自分のID(PI_ID, 4文字程度)をrelayへ申告。ビュアーはこのIDで視聴対象を選ぶ。
  - 複数の入力ソースを input-selector に束ね、camChangeで無停止に切替える(再ネゴ不要)。
  - カメラ番号: 1 = カメラ(初期値), 2.. = 追加カメラ。
    画面取得(0)は既定で無効(待機ソースのCPU上積みを避けるため)。CAM0設定で有効化可。
  - 全ソースを共通解像度にスケールするので、エンコーダ/トラックは安定したまま。

環境変数:
  PI_ID       : このPiのID (既定 "PI01")
  SERVER      : relayのWS URL (既定 ws://127.0.0.1:8080/ws)
  WIDTH/HEIGHT/FPS : 共通出力解像度 (既定 1280/720/30)
  ENCODER     : エンコード部 (既定 x264enc ... / Pi4は v4l2h264enc)
  DEFAULT_CAM : 起動時の選択番号 (既定 1)
  CAM0..CAM9  : 各番号の入力ソース部。CAM0はスクリーン、CAM1以降はカメラ。
                (定義された番号だけが選択肢になる)

通常はラッパースクリプト(publish-pi4.sh 等)経由で起動する。
"""
import os
import sys
import json
import asyncio
import threading

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib  # noqa: E402

import websockets  # noqa: E402

Gst.init(None)

PI_ID = os.environ.get("PI_ID", "PI01")
SERVER = os.environ.get("SERVER", "ws://127.0.0.1:8080/ws")
WIDTH = os.environ.get("WIDTH", "1280")
HEIGHT = os.environ.get("HEIGHT", "720")
FPS = os.environ.get("FPS", "30")
DEFAULT_CAM = int(os.environ.get("DEFAULT_CAM", "1"))

# エンコード部 (機種に応じて差し替え)
#   Pi4: v4l2h264enc (HW)  /  Pi5・PC: x264enc (SW, zerolatency)
ENCODER = os.environ.get(
    "ENCODER",
    "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30",
)

# 入力ソースの収集。CAM0..CAM9 のうち定義されたものだけ採用する。
#   既定では画面取得(0)は無効。CAM0を設定すれば有効化される。
#   0 = スクリーン (例: CAM0="ximagesrc use-damage=false" / Waylandは pipewiresrc)
#   1 = カメラ (既定 /dev/video0)
DEFAULT_SOURCES = {
    1: "v4l2src device=/dev/video0",
}


def collect_sources():
    sources = {}
    for n in range(10):
        v = os.environ.get(f"CAM{n}")
        if v:
            sources[n] = v
    if not sources:
        sources = dict(DEFAULT_SOURCES)
    return sources


SOURCES = collect_sources()

# 共通の生映像caps。全ソースをこれに揃える(=エンコーダ入力が常に一定)。
COMMON = f"video/x-raw,width={WIDTH},height={HEIGHT},framerate={FPS}/1"


def build_pipeline_desc():
    # input-selector -> 共通エンコード -> webrtcbin
    parts = [
        f"input-selector name=sel ! videoconvert ! {ENCODER} ! "
        "video/x-h264,profile=constrained-baseline ! "
        "h264parse config-interval=-1 ! "
        "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
        "application/x-rtp,media=video,encoding-name=H264,payload=96 ! "
        "webrtcbin name=sendrecv bundle-policy=max-bundle latency=0"
    ]
    # 各ソースを共通解像度に正規化して selector.sink_<番号> へ
    for num, src in sorted(SOURCES.items()):
        parts.append(
            f"{src} ! queue max-size-buffers=2 leaky=downstream ! "
            f"videoconvert ! videoscale ! {COMMON} ! sel.sink_{num}"
        )
    return "  ".join(parts)


class Publisher:
    def __init__(self, loop):
        self.loop = loop
        self.ws = None
        self.pipe = None
        self.webrtc = None
        self.selector = None
        self.current_cam = None

    # ---- GStreamer ----
    def start_pipeline(self):
        desc = build_pipeline_desc()
        print("[pipeline]", desc, flush=True)
        self.pipe = Gst.parse_launch(desc)
        self.webrtc = self.pipe.get_by_name("sendrecv")
        self.selector = self.pipe.get_by_name("sel")
        self.webrtc.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self.on_ice_candidate)

        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

        self.pipe.set_state(Gst.State.PLAYING)
        # 初期カメラを選択 (既定1)
        cam0 = DEFAULT_CAM if DEFAULT_CAM in SOURCES else min(SOURCES)
        self.set_cam(cam0)

    def set_cam(self, num):
        if num not in SOURCES:
            print(f"[cam] number {num} not configured (have {sorted(SOURCES)})", flush=True)
            return
        pad = self.selector.get_static_pad(f"sink_{num}")
        if not pad:
            print(f"[cam] no pad sink_{num}", flush=True)
            return
        self.selector.set_property("active-pad", pad)
        self.current_cam = num
        kind = "screen" if num == 0 else "camera"
        print(f"[cam] switched to {num} ({kind})", flush=True)

    def stop_pipeline(self):
        """パイプラインを停止しカメラ/エンコーダを解放する (再接続前の後始末)。"""
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
            self.webrtc = None
            self.selector = None

    def _trigger_reconnect(self):
        """WSを閉じてrun()の再接続ループに制御を戻す (プロセスは終了させない)。"""
        ws = self.ws
        if ws is not None:
            asyncio.run_coroutine_threadsafe(ws.close(), self.loop)

    def on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[gst ERROR] {err}: {dbg}", file=sys.stderr, flush=True)
            self._trigger_reconnect()
        elif t == Gst.MessageType.EOS:
            print("[gst] EOS", flush=True)
            self._trigger_reconnect()

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def on_offer_created(self, promise, element, _):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        element.emit("set-local-description", offer, None)
        text = offer.sdp.as_text()
        self.send_async({"type": "offer", "sdp": {"type": "offer", "sdp": text}})
        print("[signal] sent offer", flush=True)

    def on_ice_candidate(self, _element, mlineindex, candidate):
        self.send_async({
            "type": "candidate",
            "candidate": {"candidate": candidate, "sdpMLineIndex": mlineindex},
        })

    # ---- シグナリング受信 ----
    def handle_answer(self, sdp_text):
        _res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
        )
        self.webrtc.emit("set-remote-description", answer, None)
        print("[signal] applied answer", flush=True)

    def handle_remote_candidate(self, mlineindex, candidate):
        self.webrtc.emit("add-ice-candidate", mlineindex, candidate)

    def handle_cam_change(self, num):
        # GLibスレッドで実行 (パイプライン操作)
        GLib.idle_add(lambda: (self.set_cam(num), False)[1])

    # ---- WS送信 ----
    def send_async(self, obj):
        asyncio.run_coroutine_threadsafe(self._send(json.dumps(obj)), self.loop)

    async def _send(self, data):
        if self.ws:
            await self.ws.send(data)


async def _session(loop):
    """relayへ1回接続し、切断されるまでシグナリングを処理する。"""
    pub = Publisher(loop)
    try:
        async with websockets.connect(SERVER) as ws:
            print(f"[ws] connected to {SERVER} as id={PI_ID} (cams={sorted(SOURCES)})", flush=True)
            pub.ws = ws
            await ws.send(json.dumps({"type": "hello", "role": "publisher", "id": PI_ID}))
            pub.start_pipeline()

            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "answer":
                    pub.handle_answer(msg["sdp"]["sdp"])
                elif mtype == "candidate":
                    c = msg["candidate"]
                    pub.handle_remote_candidate(c.get("sdpMLineIndex", 0), c["candidate"])
                elif mtype == "camChange":
                    pub.handle_cam_change(int(msg["cam"]))
                elif mtype == "error":
                    print("[signal ERROR]", msg.get("message"), file=sys.stderr, flush=True)
    finally:
        # 接続が切れたら必ずカメラ/エンコーダを解放してから再接続する
        pub.stop_pipeline()


async def run():
    loop = asyncio.get_running_loop()
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, daemon=True).start()

    # relayが落ちる/瞬断しても止まらないよう、指数バックオフで自動再接続する
    backoff = 1
    while True:
        try:
            print(f"[ws] connecting to {SERVER} ...", flush=True)
            await _session(loop)
            print("[ws] disconnected", flush=True)
            backoff = 1  # 正常にセッションが回った後の切断はすぐ再接続
        except (OSError, websockets.exceptions.WebSocketException) as e:
            print(f"[ws] connection failed/lost: {e}", file=sys.stderr, flush=True)
        except Exception as e:  # 想定外でもプロセスは落とさず再接続
            print(f"[error] unexpected: {e}", file=sys.stderr, flush=True)
        print(f"[ws] reconnecting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 10)  # 1,2,4,8,10,10...秒

    glib_loop.quit()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[exit] interrupted", flush=True)
