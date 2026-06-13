(function (global) {
  "use strict";

  const COLORS = ["#3a5068", "#4a4068", "#3a5860", "#504838", "#384838"];

  function wsUrl(role) {
    const params = new URLSearchParams(location.search);
    if (params.get("ws")) return params.get("ws");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const host = location.host || "localhost:8765";
    return proto + "//" + host + "/ws/" + role;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/"/g, "&quot;");
  }

  function videoFileUrl(filename) {
    return "/video/" + encodeURIComponent(filename);
  }

  function unitVideoUrl(unit) {
    const n = Number(unit);
    if (!(n >= 1 && n <= 5)) return null;
    return videoFileUrl(n + "号機.mp4");
  }

  function overviewVideoUrl() {
    return videoFileUrl("全体カメラ.mp4");
  }

  function videoBox(unitOrOverview, extraClass, opts) {
    opts = opts || {};
    const cls = extraClass ? " " + extraClass : "";
    const isOverview =
      unitOrOverview === "overview" || opts.kind === "overview";
    let label;
    let src;

    if (isOverview) {
      label = opts.label || "全体カメラ";
      src = overviewVideoUrl();
    } else {
      const n = Number(unitOrOverview) || 1;
      label = opts.label || n + "号機";
      src = unitVideoUrl(n);
    }

    const dataUnit = isOverview ? ' data-unit="overview"' : ' data-unit="' + Number(unitOrOverview || 1) + '"';

    if (!src) {
      return (
        '<div class="video-box' +
        cls +
        '"' +
        dataUnit +
        '">' +
        '<span class="video-box__label">' +
        escapeHtml(label) +
        "</span>" +
        '<span class="video-box__placeholder">映像<br>No Connect</span>' +
        "</div>"
      );
    }

    // src は initVideos / attachVideo がモード(file/webrtc)に応じて後付けする
    return (
      '<div class="video-box' +
      cls +
      '"' +
      dataUnit +
      ">" +
      '<span class="video-box__label">' +
      escapeHtml(label) +
      "</span>" +
      '<video loop muted autoplay playsinline></video>' +
      '<span class="video-box__placeholder" hidden>映像<br>No Connect</span>' +
      "</div>"
    );
  }

  function videoPlaceholder(unit, extraClass, opts) {
    return videoBox(unit, extraClass, opts);
  }

  function videoGrid2x2(units) {
    const list = units || [1, 2, 3, 4];
    return (
      '<div class="video-grid video-grid--2x2">' +
      list.map(function (u) {
        return videoBox(u);
      }).join("") +
      "</div>"
    );
  }

  // WebRTC中継サーバーの ws URL を解決する。
  //  空        → ws://<現在のホスト>:8080/ws
  //  ws(s)://… → そのまま
  //  host[:port] → ws://host[:port|:8080]/ws
  function resolveWebrtcServer(s) {
    if (!s) return "ws://" + (location.hostname || "localhost") + ":8080/ws";
    if (/^wss?:\/\//.test(s)) return s;
    const hasPort = s.indexOf(":") >= 0;
    return "ws://" + s + (hasPort ? "" : ":8080") + "/ws";
  }

  function _tryPlay(video) {
    const p = video.play();
    if (p && typeof p.catch === "function") p.catch(function () {});
  }

  // 1つの <video> に映像ソースを割り当てる。
  //  mode="file"  : video/<号機>.mp4 をループ再生（従来動作）
  //  mode="webrtc": WebRTCCamera で RES<号機> に接続（号機1〜5のみ。overview等は file にフォールバック）
  function attachVideo(video, mode, server) {
    const box = video.closest(".video-box");
    const placeholder = box && box.querySelector(".video-box__placeholder");
    const du = box ? box.getAttribute("data-unit") : null;
    const isOverview = du === "overview";
    const unitNum = isOverview ? null : Number(du);

    // 既存のWebRTC接続を破棄
    if (video._rtc) {
      try { video._rtc.disconnect(); } catch (e) {}
      video._rtc = null;
    }

    function showFile() {
      const src = isOverview ? overviewVideoUrl() : unitVideoUrl(unitNum);
      video.srcObject = null;
      video.loop = true;
      video.muted = true;
      if (!src) {
        video.hidden = true;
        if (placeholder) placeholder.hidden = false;
        return;
      }
      video.hidden = false;
      if (placeholder) placeholder.hidden = true;
      video.src = src;
      video.addEventListener(
        "error",
        function () {
          video.hidden = true;
          if (placeholder) placeholder.hidden = false;
        },
        { once: true }
      );
      // src を JS で後付けすると autoplay 属性が発火しないため、
      // 再生可能になった時点で明示的に play() する。
      video.addEventListener("canplay", function () { _tryPlay(video); }, { once: true });
      _tryPlay(video);
    }

    if (mode === "webrtc" && unitNum >= 1 && unitNum <= 5 && global.WebRTCCamera) {
      video.loop = false;
      video.removeAttribute("src");
      video.hidden = false;
      if (placeholder) placeholder.hidden = false; // 接続するまでは「No Connect」表示
      const cam = new global.WebRTCCamera({
        server: resolveWebrtcServer(server),
        onStatus: function (s) {
          if (s === "connected") {
            if (placeholder) placeholder.hidden = true;
          } else if (s === "closed" || s.indexOf("error") === 0) {
            if (placeholder) placeholder.hidden = false;
          }
        },
      });
      video._rtc = cam;
      cam.connect(video, "RES" + unitNum);
      _tryPlay(video);
    } else {
      showFile();
    }
  }

  // root 配下の全 .video-box video にソースを割り当てる。
  // mode 省略時は "file"（従来呼び出しと互換）。
  function initVideos(root, mode, server) {
    const scope = root || document;
    const m = mode || "file";
    scope.querySelectorAll(".video-box video").forEach(function (video) {
      attachVideo(video, m, server);
    });
  }

  /* ---------- QR 解析（カメラ映像から自動読取） ---------- */

  // getVideo() で対象 <video> を返すと、一定間隔で QR を読取り onDetect(text) を呼ぶ。
  // BarcodeDetector 非対応ブラウザでは何もしない（supported:false）。
  function createQrScanner(getVideo, onDetect, intervalMs) {
    // 1) ブラウザ標準 BarcodeDetector（あれば最優先）
    let detector = null;
    if ("BarcodeDetector" in global) {
      try {
        detector = new global.BarcodeDetector({ formats: ["qr_code"] });
      } catch (e) {
        detector = null;
      }
    }
    // 2) フォールバック: jsQR（static/jsqr.js）
    const useJsqr = !detector && typeof global.jsQR === "function";
    let canvas = null, ctx = null;

    function scanWithJsqr(video) {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return;
      if (!canvas) {
        canvas = document.createElement("canvas");
        ctx = canvas.getContext("2d", { willReadFrequently: true });
      }
      const scale = Math.min(1, 640 / w); // 処理軽量化のため縮小
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      let img;
      try {
        img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      } catch (e) {
        return; // クロスオリジンで汚染されている等
      }
      const code = global.jsQR(img.data, img.width, img.height);
      if (code && code.data) onDetect(code.data);
    }

    let busy = false;
    const timer = setInterval(function () {
      const video = getVideo();
      if (!video || video.readyState < 2 || !video.videoWidth) return;
      if (detector) {
        if (busy) return;
        busy = true;
        detector
          .detect(video)
          .then(function (codes) {
            if (codes && codes.length && codes[0].rawValue) onDetect(codes[0].rawValue);
          })
          .catch(function () {})
          .then(function () { busy = false; });
      } else if (useJsqr) {
        scanWithJsqr(video);
      }
    }, intervalMs || 700);

    return {
      supported: !!detector || useJsqr,
      method: detector ? "BarcodeDetector" : useJsqr ? "jsQR" : "none",
      stop: function () { clearInterval(timer); },
    };
  }

  // 経過時間を「たった今 / N秒前 / N分M秒前」表記にする。
  function formatAgo(ts) {
    if (!ts) return "—";
    const s = Math.floor((Date.now() - ts) / 1000);
    if (s < 1) return "たった今";
    if (s < 60) return s + "秒前";
    const m = Math.floor(s / 60);
    return m + "分" + (s % 60) + "秒前";
  }

  function createWsClient(role, handlers) {
    let socket = null;
    let reconnectTimer = 0;
    let onState = handlers.onState || function () {};
    let onStatus = handlers.onStatus || function () {};

    function connect() {
      onStatus("connecting");
      try {
        socket = new WebSocket(wsUrl(role));
      } catch (e) {
        onStatus("error");
        scheduleReconnect();
        return;
      }

      socket.addEventListener("open", function () {
        onStatus("connected");
      });

      socket.addEventListener("message", function (ev) {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "state" && msg.payload) {
            onState(msg.payload);
          }
        } catch (_) {
          /* ignore */
        }
      });

      socket.addEventListener("close", function () {
        onStatus("error");
        scheduleReconnect();
      });

      socket.addEventListener("error", function () {
        onStatus("error");
      });
    }

    function scheduleReconnect() {
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(function () {
        if (socket) {
          try {
            socket.close();
          } catch (_) {}
        }
        socket = null;
        connect();
      }, 3000);
    }

    function send(obj) {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(obj));
      }
    }

    connect();

    return {
      send: send,
      close: function () {
        clearTimeout(reconnectTimer);
        if (socket) socket.close();
      },
    };
  }

  function setConnBadge(el, status) {
    if (!el) return;
    el.className = "conn-badge " + status;
    const labels = {
      connected: "接続中",
      connecting: "接続中…",
      error: "切断",
    };
    el.textContent = labels[status] || status;
  }

  /* ---------- 共有定数 ---------- */

  const ROOM_NAMES = { A: "広場", B: "暗室", C: "2階" };

  const COLOR_OPTIONS = [
    { name: "不明", swatch: "#888888" },
    { name: "黒", swatch: "#111111" },
    { name: "赤", swatch: "#e85d5d" },
    { name: "緑", swatch: "#4caf50" },
    { name: "青", swatch: "#3b6fd4" },
    { name: "黄", swatch: "#e8c63a" },
    { name: "紫", swatch: "#9b59d0" },
    { name: "水", swatch: "#5bc8e8" },
    { name: "白", swatch: "#f5f5f5" },
  ];

  /* ---------- タスク進行状況の描画 ---------- */

  function renderTaskRows(tasks) {
    let currentMarked = false;
    return tasks
      .map(function (t) {
        let cls = "";
        if (t.done) {
          cls = " is-done";
        } else if (!currentMarked) {
          cls = " is-current";
          currentMarked = true;
        }
        return (
          '<div class="task-row' + cls + '" data-task-id="' + t.id +
          '"><span class="task-row__check">✓</span>' +
          '<span class="task-row__label">' + escapeHtml(t.text) + "</span></div>"
        );
      })
      .join("");
  }

  function renderTaskGroup(title, tasks, badge) {
    const done = tasks.filter(function (t) { return t.done; }).length;
    const total = tasks.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const complete = done === total && total > 0;
    return (
      '<div class="task-group">' +
      '<div class="task-group__head"><span class="task-group__title"><span>' +
      escapeHtml(title) + "</span>" +
      (badge ? '<span class="task-group__badge">' + escapeHtml(badge) + "</span>" : "") +
      '</span><span class="task-group__count' + (complete ? " is-complete" : "") + '">' +
      done + "/" + total + "</span></div>" +
      '<div class="task-group__bar"><i style="width:' + pct + '%"></i></div>' +
      renderTaskRows(tasks) + "</div>"
    );
  }

  // タスク一覧を targetEl に描画する。roomUnits で各ルームの対応号機バッジを出す。
  function renderTasks(targetEl, tasks, roomUnits) {
    const list = tasks || [];
    const units = roomUnits || {};
    const common = list.filter(function (t) { return !t.room; });
    let html = "";
    if (common.length) html += renderTaskGroup("共通", common);
    ["A", "B", "C"].forEach(function (room) {
      const group = list.filter(function (t) { return t.room === room; });
      if (group.length) {
        const badge = units[room] ? units[room] + "号機対応中" : null;
        html += renderTaskGroup("ルーム" + room, group, badge);
      }
    });
    targetEl.innerHTML = html;
  }

  /* ---------- 通知バー ---------- */

  // 通知バー要素を渡すと showNotification(notification) 関数を返す。
  // 新着(active かつ timestamp が前回と異なる)時だけパルスさせる。
  function createNotifier(barEl, textEl) {
    let lastTs = 0;
    return function (n) {
      if (!n || !n.text) {
        textEl.textContent = "通知はありません";
        textEl.className = "notify-bar__empty";
        barEl.classList.remove("is-active");
        return;
      }
      textEl.textContent = n.text;
      textEl.className = "";
      if (n.active && n.timestamp !== lastTs) {
        lastTs = n.timestamp;
        barEl.classList.remove("is-active");
        void barEl.offsetWidth;
        barEl.classList.add("is-active");
        setTimeout(function () { barEl.classList.remove("is-active"); }, 3600);
      }
    };
  }

  global.RescueCommon = {
    wsUrl: wsUrl,
    escapeHtml: escapeHtml,
    videoBox: videoBox,
    videoPlaceholder: videoPlaceholder,
    videoGrid2x2: videoGrid2x2,
    initVideos: initVideos,
    attachVideo: attachVideo,
    resolveWebrtcServer: resolveWebrtcServer,
    createWsClient: createWsClient,
    setConnBadge: setConnBadge,
    renderTasks: renderTasks,
    createNotifier: createNotifier,
    createQrScanner: createQrScanner,
    formatAgo: formatAgo,
    ROOM_NAMES: ROOM_NAMES,
    COLOR_OPTIONS: COLOR_OPTIONS,
    COLORS: COLORS,
  };
})(window);
