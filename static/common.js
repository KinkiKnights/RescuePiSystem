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

    return (
      '<div class="video-box' +
      cls +
      '"' +
      dataUnit +
      ">" +
      '<span class="video-box__label">' +
      escapeHtml(label) +
      "</span>" +
      '<video src="' +
      src +
      '" loop muted autoplay playsinline></video>' +
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

  function initVideos(root) {
    const scope = root || document;
    scope.querySelectorAll(".video-box video").forEach(function (video) {
      video.addEventListener(
        "error",
        function () {
          video.hidden = true;
          const box = video.closest(".video-box");
          const fallback = box && box.querySelector(".video-box__placeholder");
          if (fallback) fallback.hidden = false;
        },
        { once: true }
      );
      const playPromise = video.play();
      if (playPromise && typeof playPromise.catch === "function") {
        playPromise.catch(function () {
          /* autoplay blocked */
        });
      }
    });
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

  global.RescueCommon = {
    wsUrl: wsUrl,
    escapeHtml: escapeHtml,
    videoBox: videoBox,
    videoPlaceholder: videoPlaceholder,
    videoGrid2x2: videoGrid2x2,
    initVideos: initVideos,
    createWsClient: createWsClient,
    setConnBadge: setConnBadge,
    COLORS: COLORS,
  };
})(window);
