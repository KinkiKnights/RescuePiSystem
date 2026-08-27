# camera_publisher

Pi 上で USB カメラを H.264 化し、外部 relay(SFU)へ WebRTC 配信する publisher。

## 出自と対向コンポーネント

- 出自: `sanjofumihiro/ClaudeShareContents` の `webrtc-camera/publisher/`
  （2026-08 の統合で本リポジトリへ移設。以後こちらが唯一の実体）
- 対向: relay(Go 製 SFU)と視聴クライアントも**同じリポジトリ内**にあります
  → [`server/webrtc_relay/`](../../server/webrtc_relay/)

## プロトコル契約(relay との互換性)

publisher と relay は WebSocket シグナリング(`ws://<relay>:8080/ws`)で接続します。
契約は [docs/protocols/webrtc.md](../../docs/protocols/webrtc.md) が単一の真実で、
publisher / relay / クライアント JS はすべて本リポジトリにあります。
**片側だけ変えず、同じコミットで 3 者を揃えてください。**

## カメラの指定

カメラは **master_control の Web UI(DEVICE CONFIGURATION)で候補から選ぶ**。
選択内容は `~/.config/rescue-pi/devices.json` に記録され、`publish.py` が
起動時に読む(`/dev/v4l/by-id/...` の安定パスなので USB の抜き差しやカメラ交換に
追従する)。詳細は [docs/devices.md](../../docs/devices.md)。

一時的に上書きしたいときだけ `CAM1="..."` を環境変数で渡す
(優先順: `CAM*` 環境変数 > devices.json > 自動検出)。

## 起動

master_control の programs.json から起動されます:

```bash
PI_ID=KK05 SERVER=ws://192.168.137.1:8080/ws ./publish-pi5.sh
```

- `publish-pi5.sh` — Raspberry Pi 5 用(ソフトウェアエンコード)
- `publish-pi4.sh` — Raspberry Pi 4 用(v4l2 ハードウェアエンコード)
- `publish-test.sh` — テストパターン配信

## 既知の未修正

`publish.py` の `set_cam` は request pad を `get_static_pad` で取得しようとして失敗する
(複数カメラの camChange のみ影響。単一カメラは input-selector の自動選択で動作)。
