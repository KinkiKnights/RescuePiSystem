// webrtc-camera.js — WebRTCカメラ視聴クライアント・ライブラリ
//
// 使い方:
//   const cam = new WebRTCCamera();                 // serverは現在のホストから自動推定
//   cam.connect(videoElement, "KK01");              // ビデオ要素と号機ID(KK0N)を渡すと自動接続
//   cam.camChange(0);                               // カメラ番号切替 (0=スクリーン, 1=カメラ, 2..)
//   cam.disconnect();
//
// オプション:
//   new WebRTCCamera({ server: "ws://host:8080/ws", onStatus: (s)=>{}, onStats: (st)=>{} })
//     onStatus(state): "connecting" | "connected" | "closed" | "error:<msg>"
//     onStats(stats):  { width, height, kbps, fps, jitterMs }  (1秒ごと)

class WebRTCCamera {
  constructor(opts = {}) {
    const defaultServer =
      (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/ws";
    this.server = opts.server || defaultServer;
    this.onStatus = opts.onStatus || (() => {});
    this.onStats = opts.onStats || null;

    this.ws = null;
    this.pc = null;
    this.video = null;
    this.id = null;
    this.currentCam = null;
    this._statsTimer = null;
  }

  // ビデオ要素とPiのIDを渡して接続する。
  connect(videoElement, id) {
    this.disconnect(); // 多重接続を防ぐ
    this.video = videoElement;
    this.id = id;
    this._setStatus("connecting");

    this.ws = new WebSocket(this.server);
    this.pc = new RTCPeerConnection(); // LAN内なのでICEサーバ不要

    this.pc.ontrack = (e) => {
      this.video.srcObject = e.streams[0] || new MediaStream([e.track]);
    };
    this.pc.onicecandidate = (e) => {
      if (e.candidate) this._send({ type: "candidate", candidate: e.candidate });
    };
    this.pc.onconnectionstatechange = () => {
      const s = this.pc.connectionState;
      if (s === "connected") {
        this._setStatus("connected");
        this._startStats();
      } else if (s === "failed" || s === "disconnected" || s === "closed") {
        this._setStatus("closed");
        this._stopStats();
      }
    };

    this.ws.onopen = () =>
      this._send({ type: "hello", role: "viewer", id: this.id });

    this.ws.onmessage = async (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "offer") {
        // ビュアーはアンサー側 (サーバーが配信トラックを乗せてオファーを作る)
        await this.pc.setRemoteDescription(msg.sdp);
        const answer = await this.pc.createAnswer();
        await this.pc.setLocalDescription(answer);
        this._send({ type: "answer", sdp: this.pc.localDescription });
      } else if (msg.type === "candidate") {
        try {
          await this.pc.addIceCandidate(msg.candidate);
        } catch (e) {
          console.warn("addIceCandidate", e);
        }
      } else if (msg.type === "error") {
        this._setStatus("error:" + msg.message);
      }
    };

    this.ws.onerror = () => this._setStatus("error:websocket");
    this.ws.onclose = () => this._stopStats();
    return this;
  }

  // カメラ番号を切替える (Pi側へ転送される)。0=スクリーン, 1=カメラ, 2..=追加カメラ
  camChange(num) {
    this.currentCam = num;
    this._send({ type: "camChange", cam: num });
    return this;
  }

  disconnect() {
    this._stopStats();
    if (this.pc) {
      try { this.pc.close(); } catch (e) {}
      this.pc = null;
    }
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
    }
    if (this.video) this.video.srcObject = null;
  }

  // 接続中のPi ID一覧を取得 (UI補助)。
  async listPis() {
    const base = this.server.replace(/^ws/, "http").replace(/\/ws$/, "");
    const res = await fetch(base + "/pis");
    return (await res.json()).pis || [];
  }

  // ---- 内部 ----
  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }
  _setStatus(s) { this.onStatus(s); }

  _startStats() {
    if (!this.onStats) return;
    let lastBytes = 0, lastTs = 0;
    this._stopStats();
    this._statsTimer = setInterval(async () => {
      const stats = await this.pc.getStats();
      stats.forEach((r) => {
        if (r.type === "inbound-rtp" && r.kind === "video") {
          const now = r.timestamp;
          let kbps = 0;
          if (lastTs) kbps = Math.round(((r.bytesReceived - lastBytes) * 8) / (now - lastTs));
          lastBytes = r.bytesReceived; lastTs = now;
          this.onStats({
            width: this.video.videoWidth,
            height: this.video.videoHeight,
            kbps,
            fps: Math.round(r.framesPerSecond || 0),
            jitterMs: r.jitter ? Math.round(r.jitter * 1000) : null,
          });
        }
      });
    }, 1000);
  }
  _stopStats() {
    if (this._statsTimer) { clearInterval(this._statsTimer); this._statsTimer = null; }
  }
}

// モジュール/グローバル両対応
if (typeof module !== "undefined" && module.exports) module.exports = { WebRTCCamera };
if (typeof window !== "undefined") window.WebRTCCamera = WebRTCCamera;
