"""レスキューロボコン ダミー操作画面 — FastAPI サーバー"""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
VIDEO_DIR = BASE_DIR / "video"

GROUP_TASK_NAMES = ["現着", "音声解析", "QR解析", "顔色", "搬送"]


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
        # 映像ソース: "file"(動画ファイル) または "webrtc"(機体上カメラ中継)
        self.video_mode: str = "file"
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
            "video_mode": self.video_mode,
            "webrtc_server": self.webrtc_server,
            "analysis": dict(self.analysis),
            "master_overlay": deepcopy(self.master_overlay),
            "analysis_request": dict(self.analysis_request),
            "control_request": dict(self.control_request),
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
        self.video_mode = "file"
        self.webrtc_server = ""
        self.analysis = deepcopy(DEFAULT_ANALYSIS)
        self.master_overlay = {"visible": False, "title": "", "lines": []}
        self.analysis_request = {"pending": False, "unit": 0, "timestamp": 0}
        self.control_request = {"pending": False, "unit": 0, "timestamp": 0}


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
app.mount("/video", StaticFiles(directory=str(VIDEO_DIR)), name="video")
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


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    async with state.lock:
        return state.snapshot()


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
        elif action == "set_video_mode":
            mode = str(body.get("mode", "file"))
            if mode in ("file", "webrtc"):
                state.video_mode = mode
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
