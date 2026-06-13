#!/usr/bin/env python3
"""
WebRTCカメラパブリッシャ (Raspberry Pi 用 / GStreamer webrtcbin)

カメラ映像をH.264でエンコードし、relayサーバーへWebRTCで送信する。
パブリッシャは offerer (webrtcbinがオファーを生成)。

パイプラインは環境変数で差し替える:
  SOURCE  : カメラ入力部 (例: libcamerasrc / v4l2src device=/dev/video0 / videotestsrc)
  ENCODER : エンコード部 (例: v4l2h264enc[Pi4 HW] / x264enc[SW] )
  SERVER  : relayサーバーのWS URL (例: ws://192.168.1.10:8080/ws)

通常はラッパースクリプト(publish-pi4.sh等)経由で起動する。
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
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import websockets

Gst.init(None)

SERVER = os.environ.get("SERVER", "ws://127.0.0.1:8080/ws")

# --- カメラ入力 (機種/カメラに応じて差し替え) ---
SOURCE = os.environ.get(
    "SOURCE",
    "videotestsrc is-live=true pattern=ball ! video/x-raw,width=1280,height=720,framerate=30/1",
)

# --- エンコード (機種に応じて差し替え) ---
#   Pi4: v4l2h264enc (ハードウェアエンコード)
#   Pi5/PC: x264enc (ソフトウェア, zerolatency)
ENCODER = os.environ.get(
    "ENCODER",
    "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30",
)

# webrtcbinへ渡すRTPペイロード。H.264, packetization-mode=1。
PIPELINE_DESC = (
    f"{SOURCE} ! videoconvert ! {ENCODER} ! "
    "video/x-h264,profile=constrained-baseline ! "
    "h264parse config-interval=-1 ! "
    "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
    "application/x-rtp,media=video,encoding-name=H264,payload=96 ! "
    "webrtcbin name=sendrecv bundle-policy=max-bundle latency=0"
)


class Publisher:
    def __init__(self, loop):
        self.loop = loop          # asyncioイベントループ (WS送信に使用)
        self.ws = None
        self.pipe = None
        self.webrtc = None

    # ---- GStreamer 側 ----
    def start_pipeline(self):
        print("[pipeline]", PIPELINE_DESC, flush=True)
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name("sendrecv")
        self.webrtc.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self.on_ice_candidate)

        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

        self.pipe.set_state(Gst.State.PLAYING)

    def on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[gst ERROR] {err}: {dbg}", file=sys.stderr, flush=True)
            self.loop.call_soon_threadsafe(self.loop.stop)
        elif t == Gst.MessageType.EOS:
            print("[gst] EOS", flush=True)
            self.loop.call_soon_threadsafe(self.loop.stop)

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

    # ---- シグナリング受信処理 (GLibスレッドへ橋渡し) ----
    def handle_answer(self, sdp_text):
        res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
        )
        self.webrtc.emit("set-remote-description", answer, None)
        print("[signal] applied answer", flush=True)

    def handle_remote_candidate(self, mlineindex, candidate):
        self.webrtc.emit("add-ice-candidate", mlineindex, candidate)

    # ---- WS送信 (GLibスレッドからasyncioへ) ----
    def send_async(self, obj):
        data = json.dumps(obj)
        asyncio.run_coroutine_threadsafe(self._send(data), self.loop)

    async def _send(self, data):
        if self.ws:
            await self.ws.send(data)


async def run():
    loop = asyncio.get_running_loop()
    pub = Publisher(loop)

    # GStreamerはGLibメインループを別スレッドで回す
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, daemon=True).start()

    print(f"[ws] connecting to {SERVER}", flush=True)
    async with websockets.connect(SERVER) as ws:
        pub.ws = ws
        await ws.send(json.dumps({"type": "hello", "role": "publisher"}))
        # helloを送ってからパイプラインを起動 (on-negotiation-neededでオファー生成)
        pub.start_pipeline()

        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "answer":
                pub.handle_answer(msg["sdp"]["sdp"])
            elif mtype == "candidate":
                c = msg["candidate"]
                pub.handle_remote_candidate(
                    c.get("sdpMLineIndex", 0), c["candidate"]
                )
            elif mtype == "error":
                print("[signal ERROR]", msg.get("message"), file=sys.stderr, flush=True)

    glib_loop.quit()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[exit] interrupted", flush=True)
