"""レスキューロボコン ダミー操作画面 — FastAPI サーバー"""

from __future__ import annotations

import asyncio
import json
import platform
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"
PING_CONFIG_PATH = BASE_DIR / "ping_devices.json"

GROUP_TASK_NAMES = ["現着", "音声解析", "QR解析", "顔色", "搬送"]


def _parse_norm_coord(coord: Any) -> dict[str, float] | None:
    """暗室座標ペイロードを検証して正規化座標へ整形する。

    受理: {"x": num, "y": num}（x,y ともに 0..1 の実数）→ float 化した dict。
    それ以外（型不正・範囲外・bool 等）は None を返す（＝不正として拒否）。
    ※ None を「クリア」と区別するため、呼び出し側は coord is None を先に判定する。
    """
    if not isinstance(coord, dict):
        return None
    x = coord.get("x")
    y = coord.get("y")
    # bool は int のサブクラスなので明示的に除外する
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return None
    return {"x": float(x), "y": float(y)}


# ===== 5号機 自動走行（暗室座標を目標に joy_node_web へ set_goal / cancel_goal） =====
# フィールドは 1800×1800mm の正方形。暗室座標は 0..1 正規化（左上=(0,0)・右下=(1,1)、
# nx=右方向・ny=下方向）。目標コマンドはメートル・map フレームで送る。
# 座標系は kk_rescue26_pi:ros2/joy_node_web/docs/COMMUNICATION_SPEC.md「4. 座標系」に従う。
FIELD_SIZE_M = 1.8  # 1800mm

# ロボットへコマンドを送る WebSocket（既存の joy 接続と同一 = ws://<ip>:8700/joys）
ROBOT_WS_PORT = 8700
ROBOT_WS_PATH = "joys"


def _dark_room_goal(coord: dict[str, float], field_side: str | None) -> dict[str, Any]:
    """正規化暗室座標(0..1) を joy_node_web の set_goal 用フィールド座標[m]へ変換する。

    COMMUNICATION_SPEC.md「4. 座標系」より:
      - 青フィールド: 原点=左上, X=下向き正, Y=右向き正 → x = ny*1.8, y = nx*1.8
      - 赤フィールド: 原点=右上, X=下向き正, Y=左向き正 → x = ny*1.8, y = (1-nx)*1.8
    暗室座標には向き情報が無いため yaw=0.0（要確認）。field_side 未選択時は
    青(左上原点)の規約で暫定変換する（運用側で map フレーム基準を確定すること）。
    """
    nx = float(coord["x"])
    ny = float(coord["y"])
    gx = ny * FIELD_SIZE_M
    if field_side == "red":
        gy = (1.0 - nx) * FIELD_SIZE_M
    else:
        gy = nx * FIELD_SIZE_M
    return {"x": round(gx, 4), "y": round(gy, 4), "yaw": 0.0, "frame_id": "map"}


async def _send_robot_command(ip: str, payload: dict[str, Any]) -> bool:
    """号機の joy_node_web（ws://<ip>:8700/joys）へコマンド JSON を 1 回送信する。

    COMMUNICATION_SPEC.md「2.2 コマンド」に準拠（`command` フィールドで種別指定）。
    エッジトリガのため接続→送信→切断する。IP 未設定・接続失敗・ライブラリ不在時は
    False を返し、サーバー起動は妨げない（ログに理由を残す）。
    """
    if not ip:
        print("[AUTO-RUN] 5号機 IP が未設定のためコマンドを送信できません（config.json 要確認）。")
        return False
    try:
        import websockets  # uvicorn[standard] 同梱。無ければ送信をスキップ。
    except ImportError:
        print("[AUTO-RUN] websockets ライブラリが無いためロボット送信をスキップしました（要確認）。")
        return False

    url = f"ws://{ip}:{ROBOT_WS_PORT}/{ROBOT_WS_PATH}"
    try:
        async with websockets.connect(url, open_timeout=2, close_timeout=2) as ws:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        print(f"[AUTO-RUN] 送信成功 {url} ← {payload}")
        return True
    except Exception as exc:
        print(f"[AUTO-RUN] 送信失敗 {url}（{exc}）。ロボット未接続の可能性。")
        return False


def load_unit_ips() -> dict[int, str]:
    """config.json から号機ごとの固定 IP を読み込む。

    IP は運用上の固定設定であり、マスター等クライアントからは変更不可。
    config.json が無い/壊れている場合は警告を出し、空 IP で起動を継続する。
    """
    defaults = {i: "" for i in range(1, 6)}
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {CONFIG_PATH} が見つかりません。号機 IP は空で起動します。")
        return defaults
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {CONFIG_PATH} を読み込めませんでした（{exc}）。号機 IP は空で起動します。")
        return defaults

    ips = defaults
    raw = data.get("unit_ips", {}) if isinstance(data, dict) else {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                n = int(k)
            except (ValueError, TypeError):
                continue
            if 1 <= n <= 5:
                ips[n] = str(v).strip()
    else:
        print(f"[WARN] {CONFIG_PATH} の unit_ips が不正な形式です。号機 IP は空で起動します。")
    return ips


# 号機ごとの固定 IP（config.json 由来・変更不可の設定値）
UNIT_IPS = load_unit_ips()


# ===== Ping 監視（ネットワーク機器の死活監視。旧 ping-monitor から統合） =====
PING_DEFAULTS: dict[str, Any] = {"interval_sec": 5, "ping_timeout_sec": 1, "devices": []}


def load_ping_config() -> dict[str, Any]:
    """ping_devices.json から監視対象と間隔・タイムアウトを読み込む。

    ファイルが無い/壊れている場合は既定値（5秒間隔・1秒タイムアウト・機器なし）で
    起動を継続する。号機 IP と同じく「設定ファイル → 読み込み」方式に合わせている。
    """
    cfg: dict[str, Any] = dict(PING_DEFAULTS)
    cfg["devices"] = []
    try:
        with PING_CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {PING_CONFIG_PATH} が見つかりません。ping 監視対象は空で起動します。")
        return cfg
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {PING_CONFIG_PATH} を読み込めませんでした（{exc}）。ping 監視対象は空で起動します。")
        return cfg

    if not isinstance(data, dict):
        print(f"[WARN] {PING_CONFIG_PATH} が不正な形式です。ping 監視対象は空で起動します。")
        return cfg

    try:
        cfg["interval_sec"] = float(data.get("interval_sec", 5)) or 5
    except (TypeError, ValueError):
        cfg["interval_sec"] = 5
    try:
        cfg["ping_timeout_sec"] = float(data.get("ping_timeout_sec", 1)) or 1
    except (TypeError, ValueError):
        cfg["ping_timeout_sec"] = 1

    devices: list[dict[str, str]] = []
    raw_devices = data.get("devices", [])
    if isinstance(raw_devices, list):
        for d in raw_devices:
            if isinstance(d, dict):
                ip = str(d.get("ip", "")).strip()
                if ip:
                    devices.append({"name": str(d.get("name", "")).strip(), "ip": ip})
    cfg["devices"] = devices
    return cfg


def validate_ping_config(cfg: Any) -> dict[str, Any]:
    """クライアントから受け取った ping 設定を検証・正規化する。不正なら ValueError。"""
    if not isinstance(cfg, dict):
        raise ValueError("オブジェクト形式ではありません")
    try:
        interval = float(cfg.get("interval_sec", 5))
        timeout = float(cfg.get("ping_timeout_sec", 1))
    except (TypeError, ValueError):
        raise ValueError("interval_sec / ping_timeout_sec は数値で指定してください")
    if not (1 <= interval <= 3600):
        raise ValueError("interval_sec は 1〜3600 の範囲で指定してください")
    if not (1 <= timeout <= 60):
        raise ValueError("ping_timeout_sec は 1〜60 の範囲で指定してください")

    devices_in = cfg.get("devices", [])
    if not isinstance(devices_in, list):
        raise ValueError("devices は配列で指定してください")
    devices: list[dict[str, str]] = []
    for i, d in enumerate(devices_in):
        if not isinstance(d, dict):
            raise ValueError("devices[%d] がオブジェクトではありません" % i)
        ip = str(d.get("ip", "")).strip()
        if not ip:
            raise ValueError("devices[%d] の ip が空です" % i)
        devices.append({"name": str(d.get("name", "")).strip(), "ip": ip})

    return {
        "interval_sec": interval if interval % 1 else int(interval),
        "ping_timeout_sec": timeout if timeout % 1 else int(timeout),
        "devices": devices,
    }


def save_ping_config(cfg: dict[str, Any]) -> None:
    """ping 設定を ping_devices.json へ保存する（上書き）。"""
    with PING_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


class PingState:
    """最新の ping 結果キャッシュ。バックグラウンドタスクが更新し、API が配信する。"""

    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = []  # [{name, ip, online(bool|None)}]
        self.updated: float = 0.0
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {"devices": [dict(d) for d in self.devices], "updated": self.updated}


ping_state = PingState()


async def _ping_once(ip: str, timeout_sec: float) -> bool:
    """ip に 1 回 ping を送り、応答があれば True。OS 差異を吸収（macOS/Linux/Windows）。"""
    system = platform.system().lower()
    if system == "windows":
        # Windows: -n 回数, -w タイムアウト(ミリ秒)
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), ip]
    elif system == "darwin":
        # macOS: -c 回数, -W タイムアウト(ミリ秒), -t 全体タイムアウト(秒)
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_sec * 1000)),
               "-t", str(max(1, int(timeout_sec))), ip]
    else:
        # Linux 等: -c 回数, -W タイムアウト(秒)
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_sec))), ip]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return False
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout_sec + 2)
        return rc == 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    except Exception:
        return False


async def _ping_loop() -> None:
    """各機器を interval 秒周期でまとめて ping し、結果を ping_state にキャッシュする。

    イベントループをブロックしないよう asyncio サブプロセスで並行実行する。
    ping_devices.json は毎周期読み直すため、設定変更（POST）は次周期で反映される。
    """
    while True:
        cfg = load_ping_config()
        interval = float(cfg["interval_sec"])
        timeout = float(cfg["ping_timeout_sec"])
        devices = cfg["devices"]

        # まだ ping していない機器は online=None（確認中）として即時に反映する。
        async with ping_state.lock:
            prev = {(d["ip"], d["name"]): d.get("online") for d in ping_state.devices}
            ping_state.devices = [
                {"name": d["name"], "ip": d["ip"],
                 "online": prev.get((d["ip"], d["name"]))}
                for d in devices
            ]
            ping_state.updated = time.time()

        if devices:
            results = await asyncio.gather(
                *[_ping_once(d["ip"], timeout) for d in devices]
            )
            async with ping_state.lock:
                ping_state.devices = [
                    {"name": d["name"], "ip": d["ip"], "online": bool(ok)}
                    for d, ok in zip(devices, results)
                ]
                ping_state.updated = time.time()

        await asyncio.sleep(max(1.0, interval))


def _default_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = [
        {"id": 1, "text": "ブレーカー", "room": None, "done": False},
        {"id": 2, "text": "天カメ展開", "room": None, "done": False},
    ]
    for r_idx, room in enumerate(["A", "B", "C"]):
        for t_idx, name in enumerate(GROUP_TASK_NAMES):
            tasks.append(
                {
                    "id": (r_idx + 1) * 10 + t_idx + 1,
                    "text": name,
                    "room": room,
                    "done": False,
                }
            )
    return tasks


DEFAULT_TASKS = _default_tasks()

DEFAULT_ANALYSIS = {
    "stove": "",
    "injury": "",
    "color": "",
    "audio": "",
    "pattern": "",
    "notes": "",
    "status": "待機中",
}


def _default_room_analysis() -> dict[str, dict[str, Any]]:
    return {
        r: {
            "stove": "",
            "stoveDone": False,
            "qr": "",
            "injuryDone": False,
            "color": "",
            "colorDone": False,
            "notes": "",
        }
        for r in ("A", "B", "C")
    }


def _default_units() -> dict[int, dict[str, Any]]:
    methods = ["WiFi", "TPIP", "WiFi", "TPIP", "WiFi"]
    delays = [32, 48, 55, 41, 67]
    other_ops = {3}  # 別オペレータが操縦中のダミー設定（1つまで）
    return {
        i: {
            "unit": i,
            "delay_ms": delays[i - 1],
            "connected": i != 2,
            "method": methods[i - 1],
            "disabled": False,
            "other_op": i in other_ops,
        }
        for i in range(1, 6)
    }


class AppState:
    def __init__(self) -> None:
        self.control_video_unit: int = 1
        self.control_operating_unit: int = 1
        self.analytics_target_unit: int = 1
        self.notification: dict[str, Any] = {
            "text": "",
            "active": False,
            "timestamp": 0,
        }
        self.tasks: list[dict[str, Any]] = deepcopy(DEFAULT_TASKS)
        self.units: dict[int, dict[str, Any]] = _default_units()
        self.room_units: dict[str, int] = {"A": 4, "B": 3, "C": 5}
        self.room_analysis: dict[str, dict[str, Any]] = _default_room_analysis()
        # 映像ソースは WebRTC(機体上カメラ中継)のみ。
        # WebRTC中継サーバー(空ならクライアントが ws://<host>:8080/ws を自動推定)
        self.webrtc_server: str = ""
        self.analysis: dict[str, str] = deepcopy(DEFAULT_ANALYSIS)
        self.master_overlay: dict[str, Any] = {
            "visible": False,
            "title": "",
            "lines": [],
        }
        self.analysis_request: dict[str, Any] = {
            "pending": False,
            "unit": 0,
            "timestamp": 0,
        }
        self.control_request: dict[str, Any] = {
            "pending": False,
            "unit": 0,
            "timestamp": 0,
        }
        # 号機 IP は config.json 由来の固定設定（クライアントからは変更不可）
        self.unit_ips: dict[int, str] = dict(UNIT_IPS)
        # 暗室座標（エンジニアがマップ上をクリックして指定・全モードで共有）。
        # None=未設定。値は {"x": float, "y": float}（マップ表面に対する 0..1 正規化座標）。
        self.dark_room_coord: dict[str, float] | None = None
        # フィールド陣営（マスターが選択・全モードで共有）。
        # None=未選択、"red"=赤フィールド（入口＝右辺下半分）、"blue"=青フィールド（入口＝左辺下半分）。
        self.field_side: str | None = None
        # 5号機 自動走行の状態（サーバー権威・全モード共有）。
        # "off"=消灯（既定）/ "armed"=点滅（暗室座標が入力/変更された）/ "lit"=点灯（自動走行中）。
        self.unit5_auto_run: str = "off"
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "control_video_unit": self.control_video_unit,
            "control_operating_unit": self.control_operating_unit,
            "analytics_target_unit": self.analytics_target_unit,
            "notification": dict(self.notification),
            "tasks": deepcopy(self.tasks),
            "units": deepcopy(self.units),
            "room_units": dict(self.room_units),
            "room_analysis": deepcopy(self.room_analysis),
            "webrtc_server": self.webrtc_server,
            "analysis": dict(self.analysis),
            "master_overlay": deepcopy(self.master_overlay),
            "analysis_request": dict(self.analysis_request),
            "control_request": dict(self.control_request),
            "unit_ips": {str(k): v for k, v in self.unit_ips.items()},
            "dark_room_coord": (
                dict(self.dark_room_coord) if self.dark_room_coord else None
            ),
            "field_side": self.field_side,
            "unit5_auto_run": self.unit5_auto_run,
        }

    async def push_notification(self, text: str) -> None:
        async with self.lock:
            self.notification = {
                "text": text,
                "active": True,
                "timestamp": time.time(),
            }

    async def clear_notification_pulse(self) -> None:
        async with self.lock:
            if self.notification["active"]:
                self.notification = {
                    **self.notification,
                    "active": False,
                }

    def reset(self) -> None:
        self.control_video_unit = 1
        self.control_operating_unit = 1
        self.analytics_target_unit = 1
        self.notification = {"text": "", "active": False, "timestamp": 0}
        self.tasks = deepcopy(DEFAULT_TASKS)
        self.units = _default_units()
        self.room_units = {"A": 4, "B": 3, "C": 5}
        self.room_analysis = _default_room_analysis()
        self.webrtc_server = ""
        self.analysis = deepcopy(DEFAULT_ANALYSIS)
        self.master_overlay = {"visible": False, "title": "", "lines": []}
        self.analysis_request = {"pending": False, "unit": 0, "timestamp": 0}
        self.control_request = {"pending": False, "unit": 0, "timestamp": 0}
        # 暗室座標はランタイム注記のためリセットで解除する（固定設定ではない）
        self.dark_room_coord = None
        # フィールド陣営もオペレータ設定のためリセットで未選択に戻す（固定設定ではない）
        self.field_side = None
        # 5号機 自動走行もランタイム状態のためリセットで消灯へ戻す
        self.unit5_auto_run = "off"
        # 号機 IP は固定設定のためリセットしない（config.json の値を維持）


state = AppState()
connections: dict[str, set[WebSocket]] = {
    "control": set(),
    "analytics": set(),
    "engineer": set(),
    "reporter": set(),
    "master": set(),
    "all": set(),
}


VOICE_DIR = BASE_DIR / "voice_comm"

app = FastAPI(title="Rescue Robot Dummy Apps")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/voice", StaticFiles(directory=str(VOICE_DIR)), name="voice")


async def broadcast(message: dict[str, Any], channel: str = "all") -> None:
    payload = json.dumps(message, ensure_ascii=False)
    targets: set[WebSocket] = set()
    if channel == "all":
        targets = connections["all"]
    else:
        targets = connections.get(channel, set()) | connections["all"]

    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        for bucket in connections.values():
            bucket.discard(ws)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/control")
async def control_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "control.html")


@app.get("/analytics")
async def analytics_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "analytics.html")


@app.get("/engineer")
async def engineer_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "engineer.html")


@app.get("/reporter")
async def reporter_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "reporter.html")


@app.get("/master")
async def master_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "master.html")


@app.get("/ping-monitor")
async def ping_monitor_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "ping-monitor.html")


@app.on_event("startup")
async def _start_ping_monitor() -> None:
    # ping 監視のバックグラウンドタスクを起動する。
    asyncio.create_task(_ping_loop())


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    async with state.lock:
        return state.snapshot()


@app.get("/api/ping/status")
async def get_ping_status() -> dict[str, Any]:
    """各機器の最新 ping 結果（online: true/false/null）をまとめて返す。"""
    async with ping_state.lock:
        return ping_state.snapshot()


@app.get("/api/ping/config")
async def get_ping_config() -> dict[str, Any]:
    """現在の ping 監視設定（間隔・タイムアウト・機器一覧）を返す。"""
    return load_ping_config()


@app.post("/api/ping/config")
async def post_ping_config(body: dict[str, Any]) -> Any:
    """ping 監視設定を保存する。次のポーリング周期から反映される。"""
    try:
        cfg = validate_ping_config(body)
    except (ValueError, KeyError) as exc:
        return JSONResponse(status_code=400, content={"error": f"設定が不正です: {exc}"})
    try:
        save_ping_config(cfg)
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return {"ok": True}


@app.post("/api/notify")
async def post_notify(body: dict[str, Any]) -> dict[str, str]:
    text = str(body.get("text", "")).strip()
    if not text:
        return {"status": "ignored"}
    await state.push_notification(text)
    snap = state.snapshot()
    await broadcast({"type": "state", "payload": snap})
    return {"status": "ok"}


@app.post("/api/analysis")
async def post_analysis(body: dict[str, Any]) -> dict[str, str]:
    async with state.lock:
        for key in DEFAULT_ANALYSIS:
            if key in body:
                state.analysis[key] = str(body[key])
    snap = state.snapshot()
    await broadcast({"type": "state", "payload": snap})
    return {"status": "ok"}


@app.post("/api/master")
async def post_master(body: dict[str, Any]) -> dict[str, str]:
    action = body.get("action")
    async with state.lock:
        if action == "show_overlay":
            state.master_overlay = {
                "visible": True,
                "title": str(body.get("title", "撮影指示")),
                "lines": list(body.get("lines", [])),
            }
        elif action == "hide_overlay":
            state.master_overlay = {"visible": False, "title": "", "lines": []}
        elif action == "set_analysis":
            preset = body.get("preset", {})
            for key, val in preset.items():
                if key in state.analysis:
                    state.analysis[key] = str(val)
            state.analysis["status"] = str(body.get("status", "解析中"))
        elif action == "set_analytics_target":
            unit = int(body.get("unit", 1))
            if 1 <= unit <= 5:
                state.analytics_target_unit = unit
        elif action == "set_webrtc_server":
            state.webrtc_server = str(body.get("server", "")).strip()
        elif action == "complete_task":
            task_id = int(body.get("task_id", 0))
            for t in state.tasks:
                if t["id"] == task_id:
                    t["done"] = True
        elif action == "complete_next":
            room = body.get("room") or None
            for t in state.tasks:
                if t["room"] == room and not t["done"]:
                    t["done"] = True
                    break
        elif action == "set_unit_ips":
            # 号機 IP は config.json 固定・変更不可。クライアントからの変更要求は無視。
            print("[WARN] set_unit_ips は無効化されています（号機 IP は config.json 固定）。要求を無視しました。")
        elif action == "reset":
            state.reset()
    snap = state.snapshot()
    await broadcast({"type": "state", "payload": snap})
    return {"status": "ok"}


async def apply_client_message(msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    changed = False

    if msg_type == "notify":
        text = str(msg.get("text", "")).strip()
        if text:
            await state.push_notification(text)
            changed = True

    elif msg_type == "set_analytics_target":
        unit = int(msg.get("unit", 1))
        async with state.lock:
            if 1 <= unit <= 5:
                state.analytics_target_unit = unit
                changed = True

    elif msg_type == "set_control_video":
        unit = int(msg.get("unit", 1))
        async with state.lock:
            if 1 <= unit <= 5:
                state.control_video_unit = unit
                changed = True

    elif msg_type == "set_control_operating":
        unit = int(msg.get("unit", 1))
        async with state.lock:
            if 1 <= unit <= 5:
                state.control_operating_unit = unit
                changed = True

    elif msg_type == "engineer_action":
        action = msg.get("action")
        unit = int(msg.get("unit", 1))
        notify_text = ""
        async with state.lock:
            if action == "interrupt1":
                state.control_request = {
                    "pending": True,
                    "unit": unit,
                    "timestamp": time.time(),
                }
            elif action == "interrupt2":
                state.control_request = {
                    "pending": True,
                    "unit": unit,
                    "timestamp": time.time(),
                }
            elif action == "analysis1":
                state.analysis_request = {
                    "pending": True,
                    "unit": unit,
                    "timestamp": time.time(),
                }
            elif action == "analysis2":
                state.analysis_request = {
                    "pending": True,
                    "unit": unit,
                    "timestamp": time.time(),
                }
            elif action == "toggle_method":
                u = state.units.get(unit)
                if u:
                    u["method"] = "TPIP" if u["method"] == "WiFi" else "WiFi"
                    notify_text = f"{unit}号機 通信方法 → {u['method']}"
            elif action == "disable_unit":
                u = state.units.get(unit)
                if u:
                    u["disabled"] = not u["disabled"]
                    u["connected"] = not u["disabled"]
                    label = "行動不能" if u["disabled"] else "復活"
                    notify_text = f"{unit}号機 {label}"
            elif action == "reboot_pi":
                notify_text = f"{unit}号機 Raspberry Pi 再起動要求"
            elif action == "set_room":
                room = str(msg.get("room", "A"))
                if room in state.room_units:
                    # 1機体は1ルームのみ：他ルームから外す
                    for r in state.room_units:
                        if state.room_units[r] == unit:
                            state.room_units[r] = 0
                    state.room_units[room] = unit
                    notify_text = f"ルーム{room} 対応 → {unit}号機"
            changed = True
        if notify_text:
            await state.push_notification(notify_text)

    elif msg_type == "accept_control_request":
        async with state.lock:
            req = state.control_request
            if req.get("pending") and 1 <= req.get("unit", 0) <= 5:
                state.control_video_unit = req["unit"]
                state.control_operating_unit = req["unit"]
                state.control_request = {
                    "pending": False,
                    "unit": 0,
                    "timestamp": 0,
                }
                changed = True

    elif msg_type == "accept_analysis_request":
        async with state.lock:
            req = state.analysis_request
            if req.get("pending") and 1 <= req.get("unit", 0) <= 5:
                state.analytics_target_unit = req["unit"]
                state.analysis_request = {
                    "pending": False,
                    "unit": 0,
                    "timestamp": 0,
                }
                changed = True

    elif msg_type == "update_analysis":
        async with state.lock:
            for key in DEFAULT_ANALYSIS:
                if key in msg:
                    state.analysis[key] = str(msg[key])
            changed = True

    elif msg_type == "set_room_analysis":
        room = str(msg.get("room", ""))
        async with state.lock:
            ra = state.room_analysis.get(room)
            if ra is not None:
                for key in ("stove", "color", "notes", "qr"):
                    if key in msg:
                        ra[key] = str(msg[key])
                for key in ("stoveDone", "injuryDone", "colorDone"):
                    if key in msg:
                        ra[key] = bool(msg[key])
                changed = True

    elif msg_type == "complete_task":
        task_id = int(msg.get("task_id", 0))
        async with state.lock:
            for t in state.tasks:
                if t["id"] == task_id:
                    t["done"] = True
                    changed = True

    elif msg_type == "set_dark_room_coord":
        # 暗室座標をマップ上のクリックで設定／解除（全モードへ共有）。
        # coord が null → 解除、{x,y}(0..1) → 設定、それ以外の不正値は無視。
        coord = msg.get("coord")
        auto_cancel = False
        async with state.lock:
            if coord is None:
                if state.dark_room_coord is not None:
                    state.dark_room_coord = None
                    changed = True
                    # 座標クリア＝走行対象が消えるため自動走行を解除。
                    # 走行中(lit)なら経路をキャンセルする。
                    if state.unit5_auto_run == "lit":
                        auto_cancel = True
                    if state.unit5_auto_run != "off":
                        state.unit5_auto_run = "off"
            else:
                parsed = _parse_norm_coord(coord)
                if parsed is not None:
                    prev = state.dark_room_coord
                    is_new_or_changed = prev is None or prev != parsed
                    state.dark_room_coord = parsed
                    changed = True
                    if is_new_or_changed:
                        # 新規設定/変更 → 点滅(armed)。走行中(lit)に目標が変わった
                        # 場合は旧ゴールが陳腐化するためキャンセルし、再確認を促す。
                        if state.unit5_auto_run == "lit":
                            auto_cancel = True
                        state.unit5_auto_run = "armed"
        if auto_cancel:
            # 送信はエッジトリガの副作用。UI 反映(broadcast)を待たせないよう非同期発火。
            asyncio.create_task(
                _send_robot_command(state.unit_ips.get(5, ""), {"command": "cancel_goal"})
            )

    elif msg_type == "set_field_side":
        # フィールド陣営を設定／解除（全モード共有・主にマスターから送信）。
        # side が "red"/"blue" なら設定、null なら未選択に解除。それ以外は無視。
        side = msg.get("side")
        async with state.lock:
            if side is None:
                if state.field_side is not None:
                    state.field_side = None
                    changed = True
            elif side in ("red", "blue"):
                if state.field_side != side:
                    state.field_side = side
                    changed = True

    elif msg_type == "unit5_auto_run_toggle":
        # 5号機自動走行ボタンのトグル。
        #   armed(点滅) → lit(点灯): 暗室座標を目標に set_goal を送信し走行開始。
        #   lit(点灯)  → off(消灯): cancel_goal を送信し経路走行をキャンセル。
        #   off(消灯)         : 何もしない（暗室座標未入力＝走行対象なし）。
        goal_payload: dict[str, Any] | None = None
        do_cancel = False
        async with state.lock:
            cur = state.unit5_auto_run
            if cur == "armed" and state.dark_room_coord is not None:
                state.unit5_auto_run = "lit"
                goal_payload = _dark_room_goal(
                    state.dark_room_coord, state.field_side
                )
                changed = True
            elif cur == "lit":
                state.unit5_auto_run = "off"
                do_cancel = True
                changed = True
        ip = state.unit_ips.get(5, "")
        # 送信はエッジトリガの副作用。UI 反映(broadcast)を待たせないよう非同期発火。
        if goal_payload is not None:
            asyncio.create_task(
                _send_robot_command(ip, {"command": "set_goal", **goal_payload})
            )
        if do_cancel:
            asyncio.create_task(_send_robot_command(ip, {"command": "cancel_goal"}))

    elif msg_type == "reporter_cue":
        room = str(msg.get("room", ""))
        text = str(msg.get("text", ""))
        if text:
            await state.push_notification(f"[ルーム{room}] {text}")
            changed = True

    if changed:
        snap = state.snapshot()
        await broadcast({"type": "state", "payload": snap})


@app.websocket("/ws/{role}")
async def websocket_endpoint(websocket: WebSocket, role: str) -> None:
    if role not in connections:
        role = "all"
    await websocket.accept()
    connections[role].add(websocket)
    connections["all"].add(websocket)

    snap = state.snapshot()
    await websocket.send_text(
        json.dumps({"type": "state", "payload": snap}, ensure_ascii=False)
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await apply_client_message(msg)
    except WebSocketDisconnect:
        pass
    finally:
        connections[role].discard(websocket)
        connections["all"].discard(websocket)


def _ssl_args() -> dict[str, str]:
    """certs/cert.pem と certs/key.pem があれば HTTPS で起動する。

    iOS Safari は localhost 以外ではマイク利用に HTTPS(セキュアコンテキスト)が
    必須。自己署名証明書は `python make_cert.py` で生成できる（環境変数
    SSL_CERT / SSL_KEY でパス上書き可）。
    """
    import os

    cert = os.environ.get("SSL_CERT") or str(BASE_DIR / "certs" / "cert.pem")
    key = os.environ.get("SSL_KEY") or str(BASE_DIR / "certs" / "key.pem")
    if Path(cert).exists() and Path(key).exists():
        print(f"[HTTPS] {cert} / {key} を使用します（https://<host>:8765/）")
        return {"ssl_certfile": cert, "ssl_keyfile": key}
    print("[HTTP] 証明書が無いため HTTP で起動します（iPhoneのマイクは不可）")
    return {}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True, **_ssl_args())
