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

  function videoBox(unitOrOverview, extraClass, opts) {
    opts = opts || {};
    const cls = extraClass ? " " + extraClass : "";
    const isOverview =
      unitOrOverview === "overview" || opts.kind === "overview";
    const label = isOverview
      ? opts.label || "全体カメラ"
      : opts.label || (Number(unitOrOverview) || 1) + "号機";

    const dataUnit = isOverview ? ' data-unit="overview"' : ' data-unit="' + Number(unitOrOverview || 1) + '"';

    // 映像ソースは WebRTC（実機カメラ）。src は initVideos / attachVideo が後付けする。
    return (
      '<div class="video-box' +
      cls +
      '"' +
      dataUnit +
      ">" +
      '<span class="video-box__label">' +
      escapeHtml(label) +
      "</span>" +
      '<video muted autoplay playsinline></video>' +
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
  // 映像ソースは WebRTC（実機カメラ中継）。号機 1〜5 → カメラ ID KK01〜KK05。
  // 号機以外（overview 等）は WebRTC 非対応のため「No Connect」表示のまま。
  function attachVideo(video, server) {
    const box = video.closest(".video-box");
    const placeholder = box && box.querySelector(".video-box__placeholder");
    const du = box ? box.getAttribute("data-unit") : null;
    const unitNum = du === "overview" ? null : Number(du);

    // 既存のWebRTC接続を破棄
    if (video._rtc) {
      try { video._rtc.disconnect(); } catch (e) {}
      video._rtc = null;
    }

    if (unitNum >= 1 && unitNum <= 5 && global.WebRTCCamera) {
      video.loop = false;
      video.srcObject = null;
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
      cam.connect(video, "KK" + String(unitNum).padStart(2, "0"));
      _tryPlay(video);
    } else {
      // WebRTC 対象外（号機以外）は信号なし表示
      video.srcObject = null;
      video.removeAttribute("src");
      video.hidden = true;
      if (placeholder) placeholder.hidden = false;
    }
  }

  // root 配下の全 .video-box video に WebRTC ソースを割り当てる。
  function initVideos(root, server) {
    const scope = root || document;
    scope.querySelectorAll(".video-box video").forEach(function (video) {
      attachVideo(video, server);
    });
  }

  /* ---------- QR 解析（カメラ映像から自動読取） ---------- */

  // getVideo() で対象 <video> を返すと、一定間隔で QR を読取り onDetect(text) を呼ぶ。
  // BarcodeDetector 非対応ブラウザでは何もしない（supported:false）。
  // jsQR の生バイト（code.binaryData）から文字列を復元する。
  // バイトモード QR は Shift-JIS/CP932 のこともあるため、まず厳格 UTF-8 で試し、
  // 失敗（＝非 UTF-8）なら Shift-JIS で解釈する。どちらも不可なら code.data を使う。
  function decodeQrText(code) {
    if (!code) return "";
    const bin = code.binaryData;
    if (bin && bin.length) {
      const bytes = Uint8Array.from(bin);
      try {
        return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch (e) {
        try {
          return new TextDecoder("shift-jis", { fatal: false }).decode(bytes);
        } catch (e2) {
          /* 両デコーダ失敗 → code.data へフォールバック */
        }
      }
    }
    return code.data || "";
  }

  function createQrScanner(getVideo, onDetect, intervalMs) {
    // QR デコードは jsQR（static/jsqr.js）で行う。
    const useJsqr = typeof global.jsQR === "function";
    let canvas = null, ctx = null;

    function scanWithJsqr(video) {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return;
      if (!canvas) {
        canvas = document.createElement("canvas");
        ctx = canvas.getContext("2d", { willReadFrequently: true });
      }
      const scale = Math.min(1, 960 / w); // 縮小しすぎると小さいQRを検出できないため 960px 上限
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
      // Shift-JIS 等の非 UTF-8 QR では jsQR の code.data が空文字になるため、
      // 検出判定は binaryData で行う（code.data をゲートにすると読めない）。
      if (!code) return;
      const hasBytes = code.binaryData && code.binaryData.length;
      if (!hasBytes && !code.data) return;
      const text = decodeQrText(code);
      if (text) onDetect(text);
    }

    const timer = setInterval(function () {
      if (!useJsqr) return;
      const video = getVideo();
      if (!video || video.readyState < 2 || !video.videoWidth) return;
      scanWithJsqr(video);
    }, intervalMs || 700);

    return {
      supported: useJsqr,
      method: useJsqr ? "jsQR" : "none",
      stop: function () { clearInterval(timer); },
    };
  }

  // 読み取り時刻を「分:秒」(mm:ss・ゼロ埋め) で返す。QR を読み取った瞬間の
  // 時刻を示すためのもの（経過時間ではない）。
  function formatClockMMSS(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return mm + ":" + ss;
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

  /* ---------- 暗室座標マップ（全モード共有） ----------
     フィールドは 1800×1800mm の「全面暗室」（正方形）。それを模した自作 SVG
     （外部アセット・通信なし）。座標はマップ表面に対する 0..1 正規化値（x,y）で
     保持し、画面サイズやモードに依存せず同じ位置にマーカーを描ける（ワイヤ契約は
     正規化値のまま。表示のみ mm 換算する→ formatCoord）。engineer は
     interactive:true でクリック指定、他モードは読み取り専用でマーカーのみ表示する。
     入口はマスターが選ぶ field_side（"red"/"blue"/null）で辺上に描く。 */
  const FIELD_MAP_VB = 200; // 正方形 viewBox（1800×1800mm を表す論理座標）
  const FIELD_MM = 1800;    // 実フィールド寸法(mm)。表示の mm 換算にのみ使用

  function createFieldMap(containerEl, opts) {
    opts = opts || {};
    containerEl.classList.add("field-map");
    if (opts.interactive) containerEl.classList.add("field-map--interactive");
    const VB = FIELD_MAP_VB;
    containerEl.innerHTML =
      '<svg class="field-map__svg" viewBox="0 0 ' + VB + " " + VB +
      '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">' +
        '<rect class="fm-field" x="1" y="1" width="' + (VB - 2) + '" height="' + (VB - 2) + '"/>' +
        '<g class="fm-entrance"></g>' +
        '<g class="fm-marker" hidden>' +
          '<line class="fm-marker__cross" x1="-11" y1="0" x2="11" y2="0"/>' +
          '<line class="fm-marker__cross" x1="0" y1="-11" x2="0" y2="11"/>' +
          '<circle class="fm-marker__dot" cx="0" cy="0" r="6"/>' +
          '<text class="fm-marker__tag" x="10" y="-10">暗室</text>' +
        "</g>" +
      "</svg>";
    const svg = containerEl.querySelector("svg");
    const marker = containerEl.querySelector(".fm-marker");
    const entrance = containerEl.querySelector(".fm-entrance");

    function setCoord(coord) {
      if (coord && typeof coord.x === "number" && typeof coord.y === "number") {
        const px = coord.x * VB;
        const py = coord.y * VB;
        marker.setAttribute("transform", "translate(" + px + "," + py + ")");
        marker.removeAttribute("hidden");
      } else {
        marker.setAttribute("hidden", "");
      }
    }

    // 入口「入口」を辺上に描く。SVG 原点は左上なので「下半分」は y 大きい側。
    //   side==="red"  → 右辺の下半分
    //   side==="blue" → 左辺の下半分
    //   それ以外(null) → 入口なし
    function setSide(side) {
      const seg = 8;          // 入口帯の太さ
      const y0 = VB / 2;      // 下半分の開始 y
      const ty = VB * 0.75;   // 入口ラベルの y（下半分の中央）
      let html = "";
      if (side === "red") {
        html =
          '<rect class="fm-entrance__seg fm-entrance__seg--red" x="' + (VB - seg) +
          '" y="' + y0 + '" width="' + seg + '" height="' + (VB - y0) + '"/>' +
          '<text class="fm-entrance__tag" x="' + (VB - seg - 4) + '" y="' + ty +
          '" text-anchor="end">入口</text>';
      } else if (side === "blue") {
        html =
          '<rect class="fm-entrance__seg fm-entrance__seg--blue" x="0" y="' + y0 +
          '" width="' + seg + '" height="' + (VB - y0) + '"/>' +
          '<text class="fm-entrance__tag" x="' + (seg + 4) + '" y="' + ty +
          '" text-anchor="start">入口</text>';
      }
      entrance.innerHTML = html;
    }

    if (opts.interactive && typeof opts.onPick === "function") {
      svg.addEventListener("click", function (e) {
        const rect = svg.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        // 要素のアスペクト比は CSS で viewBox(1:1) に固定してあるため、
        // 要素内の相対位置がそのまま 0..1 正規化座標になる。
        let x = (e.clientX - rect.left) / rect.width;
        let y = (e.clientY - rect.top) / rect.height;
        x = Math.min(1, Math.max(0, x));
        y = Math.min(1, Math.max(0, y));
        opts.onPick({ x: x, y: y });
      });
    }

    setSide(opts.side || null); // 初期 side を反映

    return { setCoord: setCoord, setSide: setSide };
  }

  // 正規化座標を mm 表記にする（未設定は "未設定"）。
  // フィールドは 1800×1800mm。ワイヤ/保存値は正規化 0..1 のままで、表示だけ mm 換算。
  // 例: "x 1234 ／ y 567 mm（正規化 0.686, 0.315）"
  function formatCoord(coord) {
    if (!coord || typeof coord.x !== "number" || typeof coord.y !== "number") {
      return "未設定";
    }
    const xmm = Math.round(coord.x * FIELD_MM);
    const ymm = Math.round(coord.y * FIELD_MM);
    return (
      "x " + xmm + " ／ y " + ymm + " mm（正規化 " +
      coord.x.toFixed(3) + ", " + coord.y.toFixed(3) + "）"
    );
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
    createFieldMap: createFieldMap,
    formatCoord: formatCoord,
    formatAgo: formatAgo,
    formatClockMMSS: formatClockMMSS,
    ROOM_NAMES: ROOM_NAMES,
    COLOR_OPTIONS: COLOR_OPTIONS,
    COLORS: COLORS,
  };
})(window);
