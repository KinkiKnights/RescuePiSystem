/**
 * VoiceComm — 音声通信クライアントライブラリ
 *
 * 音声中継サーバー(voice_comm/server.py, ポート8766)とWebSocketで接続し、
 * スペースキー押下中だけマイク音声を送信する(プッシュ・トゥ・トーク)。
 * 受信した他端末の音声は自動で再生する。
 *
 * 使い方(各モードのHTMLで):
 *   <script src="/voice/voice-client.js"></script>
 *   <script>VoiceComm.init({ role: "操縦" });</script>
 *
 * オプション:
 *   role      : 表示名(他端末の送話インジケータに出る)
 *   serverUrl : 中継サーバーURL(省略時 ws://<hostname>:8766/voice)
 *   indicator : false で画面右下の状態表示を出さない
 *   onStatus  : function(status) 接続状態コールバック
 *   onTalk    : function({id, role, active}) 他端末の送話状態コールバック
 */
(function (global) {
  "use strict";

  const SAMPLE_RATE = 16000;
  const DEFAULT_PORT = 8766;

  let opts = {};
  let role = "unknown";
  let ws = null;
  let myId = -1;
  let status = "idle";
  let talking = false;
  let micError = false;
  let reconnectTimer = 0;
  let audioCtx = null;
  let micStream = null;
  let processor = null;
  let sourceNode = null;
  const playheads = {};
  const remoteTalkers = {};
  let indicatorEl = null;

  function defaultUrl() {
    const host = location.hostname || "localhost";
    return "ws://" + host + ":" + DEFAULT_PORT + "/voice";
  }

  function getAudioCtx() {
    if (!audioCtx) {
      const AC = global.AudioContext || global.webkitAudioContext;
      audioCtx = new AC();
    }
    if (audioCtx.state === "suspended") {
      audioCtx.resume().catch(function () {});
    }
    return audioCtx;
  }

  function setStatus(s) {
    status = s;
    if (opts.onStatus) opts.onStatus(s);
    updateIndicator();
  }

  /* ---------- 接続 ---------- */

  function connect() {
    setStatus("connecting");
    const base = opts.serverUrl || defaultUrl();
    const url = base + (base.indexOf("?") >= 0 ? "&" : "?") + "role=" + encodeURIComponent(role);
    try {
      ws = new WebSocket(url);
    } catch (e) {
      setStatus("error");
      scheduleReconnect();
      return;
    }
    ws.binaryType = "arraybuffer";

    ws.addEventListener("open", function () {
      setStatus("connected");
    });

    ws.addEventListener("message", function (ev) {
      if (typeof ev.data === "string") {
        handleText(ev.data);
      } else {
        handleBinary(ev.data);
      }
    });

    ws.addEventListener("close", function () {
      if (status !== "full") setStatus("error");
      scheduleReconnect();
    });

    ws.addEventListener("error", function () {
      setStatus("error");
    });
  }

  function scheduleReconnect() {
    if (status === "full") return;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(function () {
      if (ws) {
        try { ws.close(); } catch (_) {}
      }
      ws = null;
      connect();
    }, 3000);
  }

  function handleText(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch (_) {
      return;
    }
    if (msg.type === "welcome") {
      myId = msg.id;
    } else if (msg.type === "full") {
      setStatus("full");
    } else if (msg.type === "talk") {
      if (msg.active) {
        remoteTalkers[msg.id] = msg.role || "?";
      } else {
        delete remoteTalkers[msg.id];
      }
      if (opts.onTalk) opts.onTalk(msg);
      updateIndicator();
    } else if (msg.type === "leave") {
      delete remoteTalkers[msg.id];
      delete playheads[msg.id];
      updateIndicator();
    }
  }

  /* ---------- 受信音声の再生 ---------- */

  function handleBinary(buf) {
    // 送話中は受話しない（半二重）。自分の声が別端末経由で戻ってくる
    // ループバックを防ぐ。送話を止めれば通常どおり受話する。
    if (talking) return;
    if (buf.byteLength < 3) return;
    const senderId = new Uint8Array(buf, 0, 1)[0];
    const pcm = new Int16Array(buf.slice(1));
    const ctx = getAudioCtx();
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) {
      f32[i] = pcm[i] / 0x8000;
    }
    const buffer = ctx.createBuffer(1, f32.length, SAMPLE_RATE);
    buffer.copyToChannel(f32, 0);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    let t = playheads[senderId] || 0;
    const now = ctx.currentTime;
    if (t < now + 0.05) t = now + 0.05;
    src.start(t);
    playheads[senderId] = t + buffer.duration;
  }

  /* ---------- マイク取得・送信 ---------- */

  function ensureMic() {
    if (micStream) return Promise.resolve(true);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      micError = true;
      updateIndicator();
      return Promise.resolve(false);
    }
    return navigator.mediaDevices
      .getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
        video: false,
      })
      .then(function (stream) {
        micStream = stream;
        micError = false;
        const ctx = getAudioCtx();
        sourceNode = ctx.createMediaStreamSource(stream);
        processor = ctx.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (e) {
          if (!talking || !ws || ws.readyState !== WebSocket.OPEN) return;
          const input = e.inputBuffer.getChannelData(0);
          const ratio = ctx.sampleRate / SAMPLE_RATE;
          const outLen = Math.floor(input.length / ratio);
          const out = new Int16Array(outLen);
          for (let i = 0; i < outLen; i++) {
            let v = input[Math.floor(i * ratio)];
            if (v > 1) v = 1;
            if (v < -1) v = -1;
            out[i] = v * 0x7fff;
          }
          ws.send(out.buffer);
        };
        sourceNode.connect(processor);
        // ScriptProcessor は出力先に繋がないと動かないブラウザがあるため
        // 無音のゲインを経由して destination に接続する
        const mute = ctx.createGain();
        mute.gain.value = 0;
        processor.connect(mute);
        mute.connect(ctx.destination);
        return true;
      })
      .catch(function () {
        micError = true;
        updateIndicator();
        return false;
      });
  }

  function sendTalk(active) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "talk", active: active }));
    }
  }

  function startTalk() {
    if (talking) return;
    talking = true;
    getAudioCtx();
    ensureMic();
    sendTalk(true);
    updateIndicator();
  }

  function stopTalk() {
    if (!talking) return;
    talking = false;
    sendTalk(false);
    updateIndicator();
  }

  /* ---------- キー操作(スペースでPTT) ---------- */

  function bindKeys() {
    document.addEventListener("keydown", function (e) {
      if (e.code !== "Space") return;
      if (e.repeat) return;
      if (e.target.matches && e.target.matches("input, textarea, select")) return;
      startTalk();
    });
    document.addEventListener("keyup", function (e) {
      if (e.code !== "Space") return;
      stopTalk();
    });
    global.addEventListener("blur", stopTalk);
  }

  /* ---------- 状態インジケータ ---------- */

  function buildIndicator() {
    const style = document.createElement("style");
    style.textContent =
      ".voice-indicator{position:fixed;right:10px;bottom:10px;z-index:9999;" +
      "padding:0.35rem 0.7rem;font-size:0.74rem;font-weight:600;" +
      "font-family:inherit;color:#fff;background:#35353a;" +
      "border:1px solid #fff;display:flex;align-items:center;gap:0.5rem;" +
      "pointer-events:none;}" +
      ".voice-indicator__dot{width:9px;height:9px;border-radius:50%;" +
      "background:#6fcf6f;flex-shrink:0;}" +
      ".voice-indicator.is-error .voice-indicator__dot{background:#e85d5d;}" +
      ".voice-indicator.is-talking{background:#c46a1a;}" +
      "@keyframes voiceBlink{0%,100%{opacity:1}50%{opacity:0.35}}" +
      ".voice-indicator.is-talking .voice-indicator__dot{" +
      "background:#fff;animation:voiceBlink 0.8s infinite;}";
    document.head.appendChild(style);
    indicatorEl = document.createElement("div");
    indicatorEl.className = "voice-indicator";
    document.body.appendChild(indicatorEl);
  }

  function updateIndicator() {
    if (!indicatorEl) return;
    let cls = "voice-indicator";
    let text;
    if (status === "full") {
      cls += " is-error";
      text = "音声: 満員(5台)";
    } else if (status !== "connected") {
      cls += " is-error";
      text = "音声: 未接続";
    } else if (micError) {
      cls += " is-error";
      text = location.protocol === "https:"
        ? "音声: マイク不可"
        : "音声: マイク不可（HTTPSが必要）";
    } else if (talking) {
      cls += " is-talking";
      text = "送話中 — " + role;
    } else {
      text = "音声: 待機 (スペースで送話)";
    }
    const talkers = Object.keys(remoteTalkers).map(function (k) {
      return remoteTalkers[k];
    });
    if (talkers.length) {
      text += " ｜ 受話: " + talkers.join(", ");
    }
    indicatorEl.className = cls;
    indicatorEl.innerHTML =
      '<span class="voice-indicator__dot"></span><span>' +
      text.replace(/&/g, "&amp;").replace(/</g, "&lt;") +
      "</span>";
  }

  /* ---------- 公開API ---------- */

  global.VoiceComm = {
    init: function (options) {
      opts = options || {};
      role = opts.role || "unknown";
      if (opts.indicator !== false) buildIndicator();
      bindKeys();
      connect();
      updateIndicator();
      return this;
    },
    startTalk: startTalk,
    stopTalk: stopTalk,
    getStatus: function () {
      return { status: status, id: myId, talking: talking, micError: micError };
    },
  };
})(window);
