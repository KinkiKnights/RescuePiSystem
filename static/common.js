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
    createWsClient: createWsClient,
    setConnBadge: setConnBadge,
    renderTasks: renderTasks,
    createNotifier: createNotifier,
    ROOM_NAMES: ROOM_NAMES,
    COLOR_OPTIONS: COLOR_OPTIONS,
    COLORS: COLORS,
  };
})(window);
