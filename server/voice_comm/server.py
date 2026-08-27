"""音声中継サーバー — 操作画面用サーバーとは独立した単体サーバー

最大5台の端末からの音声(16kHz/モノラル/Int16 PCM)をWebSocketで受け取り、
送信元以外の全端末へ中継する。

起動:  py -3.10 server.py   (ポート 8766)

プロトコル:
  接続      : ws://<host>:8766/voice?role=<表示名>
  受信(text): {"type":"welcome","id":N,"peers":[...]}  接続直後に自分のIDを通知
              {"type":"join"|"leave","id":N,"role":..} 他端末の入退室
              {"type":"talk","id":N,"role":..,"active":bool} 他端末の送話状態
              {"type":"full"}                          満員時(直後に切断)
  送信(text): {"type":"talk","active":bool}            自分の送話状態
  音声(binary):
      クライアント→サーバー : Int16LE PCM そのまま
      サーバー→クライアント : 先頭1バイトに送信元ID、続けてPCM
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, WebSocket

MAX_CLIENTS = 5
PORT = 8766

app = FastAPI(title="Voice Relay Server")

clients: dict[int, dict[str, Any]] = {}


def _free_id() -> int | None:
    for i in range(MAX_CLIENTS):
        if i not in clients:
            return i
    return None


async def _broadcast_text(payload: dict[str, Any], exclude: int | None = None) -> None:
    msg = json.dumps(payload, ensure_ascii=False)
    for cid, c in list(clients.items()):
        if cid == exclude:
            continue
        try:
            await c["ws"].send_text(msg)
        except Exception:
            pass


async def _broadcast_bytes(data: bytes, sender: int) -> None:
    framed = bytes([sender]) + data
    for cid, c in list(clients.items()):
        if cid == sender:
            continue
        try:
            await c["ws"].send_bytes(framed)
        except Exception:
            pass


@app.websocket("/voice")
async def voice_endpoint(websocket: WebSocket, role: str = "unknown") -> None:
    await websocket.accept()
    cid = _free_id()
    if cid is None:
        await websocket.send_text(json.dumps({"type": "full"}, ensure_ascii=False))
        await websocket.close(code=1008)
        return

    clients[cid] = {"ws": websocket, "role": role}
    await websocket.send_text(
        json.dumps(
            {
                "type": "welcome",
                "id": cid,
                "peers": [
                    {"id": i, "role": c["role"]}
                    for i, c in clients.items()
                    if i != cid
                ],
            },
            ensure_ascii=False,
        )
    )
    await _broadcast_text({"type": "join", "id": cid, "role": role}, exclude=cid)

    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                await _broadcast_bytes(data, cid)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "talk":
                await _broadcast_text(
                    {
                        "type": "talk",
                        "id": cid,
                        "role": role,
                        "active": bool(obj.get("active")),
                    },
                    exclude=cid,
                )
    except Exception:
        pass
    finally:
        clients.pop(cid, None)
        await _broadcast_text({"type": "leave", "id": cid, "role": role})


if __name__ == "__main__":
    import os
    from pathlib import Path

    import uvicorn

    # 操作画面サーバーと同じ certs/ を共有（プロジェクト直下）。
    # 証明書があれば wss(HTTPS) で起動する。SSL_CERT / SSL_KEY で上書き可。
    base = Path(__file__).resolve().parent.parent
    cert = os.environ.get("SSL_CERT") or str(base / "certs" / "cert.pem")
    key = os.environ.get("SSL_KEY") or str(base / "certs" / "key.pem")
    ssl_args = {}
    if Path(cert).exists() and Path(key).exists():
        ssl_args = {"ssl_certfile": cert, "ssl_keyfile": key}
        print(f"[WSS] {cert} を使用します")
    uvicorn.run(app, host="0.0.0.0", port=PORT, **ssl_args)
