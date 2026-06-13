# webrtc-camera — Raspberry Pi カメラの低遅延WebRTC配信

Raspberry Pi (Ubuntu 24.04, Pi 4/5想定) に接続したカメラ映像を、
**高性能な中継サーバー(SFU)経由でブラウザへWebRTC配信**するシステム。
低遅延・低CPU負荷を重視し、可能な範囲でハードウェアアクセラレーションを活用する。

> 前提: ローカルネットワーク内・管理された端末での利用。NAT越え/TURN/WebRTC不可時の
> フォールバックは想定しない（STUN/TURN不要）。

## アーキテクチャ

```
┌──────────────────┐  WebRTC(H.264)   ┌────────────────────┐  WebRTC(H.264)  ┌──────────┐
│ Raspberry Pi      │ ───publish────▶ │  中継サーバー (SFU)  │ ───fan-out────▶ │ ブラウザ  │
│ camera → H.264    │                 │  Go + Pion          │                 │ (複数可)  │
│ GStreamer/webrtcbin│                │  署名/中継/Web配信   │                 │          │
└──────────────────┘                 └────────────────────┘                 └──────────┘
```

- **2レグともWebRTC。** Piは上り1本を送るだけで、視聴者が増えてもPiの負荷は一定。
  ファンアウトは中継サーバーが担当する。
- **SFU方式**: 中継サーバーは再エンコードせずRTPを転送するだけ（=低遅延・低CPU）。
- **コーデックはH.264に統一**: Pi4のHWエンコーダがH.264、全ブラウザがH.264対応、
  低遅延設定(constrained-baseline / zerolatency / Bフレーム無し)が容易。
- **シグナリング**: WebSocket(JSON)。publisherはofferer、viewerはanswerer
  （サーバーがトラックを乗せてオファーを作る）。LAN内なのでICEサーバ不要。

## ハードウェアアクセラレーション方針（重要）

| 機種   | H.264エンコード             | 備考 |
|--------|----------------------------|------|
| Pi 4   | `v4l2h264enc`（HWエンコーダ）| VideoCoreの専用H.264エンコーダ。CPUほぼ不使用。|
| Pi 5   | `x264enc`（ソフトウェア）    | **Pi5は専用H.264 HWエンコーダを廃止**。CPU(A76)が高速なので720p/30は実用的。|

USBカメラ自体がH.264出力できる場合は、それを無加工で流せば最小負荷。

## ディレクトリ構成

```
webrtc-camera/
├── relay/                 # 中継サーバー (Go + Pion SFU)
│   ├── main.go            #   WebSocketシグナリング + SFUファンアウト + Web静的配信
│   └── relay              #   ビルド済みバイナリ
├── web/
│   └── index.html         # ブラウザ視聴クライアント (素のWebRTC, ビルド不要)
├── publisher/             # Piパブリッシャ (GStreamer webrtcbin)
│   ├── publish.py         #   本体 (シグナリング + パイプライン)
│   ├── publish-pi4.sh     #   Pi4用: HWエンコード(v4l2h264enc)
│   ├── publish-pi5.sh     #   Pi5用: SWエンコード(x264enc)
│   └── publish-test.sh    #   このPCでの動作確認用 (Webカメラ/videotestsrc)
├── tools/
│   └── headless-viewer/   # ブラウザ無しで配信経路を検証するテスト用視聴クライアント
└── README.md
```

## 必要パッケージ

中継サーバー(ビルド/実行):
```bash
sudo apt install golang-go
```

パブリッシャ(Pi側):
```bash
sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-nice \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-base gstreamer1.0-libav \
  python3-gi gir1.2-gst-plugins-bad-1.0 python3-websockets
# Pi4のHWエンコードには v4l2h264enc (gstreamer1.0-plugins-good / カーネルV4L2) が必要
# CSIカメラには libcamera / gstreamer1.0-libcamera
```

## 使い方

### 1. 中継サーバーを起動（高性能なマシンで）

```bash
cd relay
go build -o relay .          # 初回のみ
./relay -addr :8080 -web ../web
```

### 2. ブラウザで視聴

中継サーバーの `http://<サーバーIP>:8080/` を開く（自動接続）。

### 3. Raspberry Piでパブリッシャを起動

```bash
# Pi4 (HWエンコード)
SERVER=ws://<サーバーIP>:8080/ws ./publisher/publish-pi4.sh

# Pi5 (SWエンコード)
SERVER=ws://<サーバーIP>:8080/ws ./publisher/publish-pi5.sh
```

カメラやパラメータは環境変数で上書きできる（各スクリプト冒頭のコメント参照）:
- `SOURCE`  … カメラ入力部（`libcamerasrc` / `v4l2src device=/dev/video0` 等）
- `ENCODER` … エンコード部（ビットレート/キーフレーム間隔の調整）
- `SERVER`  … 中継サーバーのWS URL

## このPC(Ubuntu 24.04)での動作確認

ラズパイ実機が無いため、このPCのWebカメラを「Piのカメラ役」として全経路を検証する。

```bash
# 1) 中継サーバー
./relay/relay -addr :8080 -web web &

# 2) パブリッシャ (実Webカメラ + x264enc。USE_TEST=1 で合成映像)
SERVER=ws://127.0.0.1:8080/ws ./publisher/publish-test.sh &

# 3a) ブラウザで http://localhost:8080/ を開く
# 3b) または、ヘッドレス視聴クライアントで経路を機械的に検証
go build -o tools/headless-viewer/headless-viewer ./tools/headless-viewer
tools/headless-viewer/headless-viewer -dur 6s -out /tmp/received.h264
ffmpeg -i /tmp/received.h264 -frames:v 1 /tmp/frame.png   # → 実映像が取れる
```

### 検証結果（このPCで実施済み）

- 実Webカメラ → GStreamer(x264) → webrtcbin → relay(Pion SFU) → 視聴 の
  フルチェーンが疎通。受信ストリームは **1280x720 / H.264 Constrained Baseline /
  約2.5Mbps** で、デコードして実フレーム取得を確認。
- **複数視聴者へのファンアウトを確認**: 2クライアント同時接続で双方が同一ストリームを
  受信。publisherの上りは1本のまま（=視聴者が増えてもPi負荷は一定）。
- ヘッドレス視聴クライアントはブラウザと同一のシグナリング/SDP/ICE/RTP経路
  （サーバーがオファー、クライアントがアンサー、H.264ネゴシエーション）を通るため、
  ブラウザ視聴経路の検証を兼ねる。

## 低遅延のための設計

- `tune=zerolatency` / `speed-preset=ultrafast` / `constrained-baseline` / Bフレーム無し
- `rtph264pay aggregate-mode=zero-latency` で送出を遅延させない
- SFUは再エンコードせず転送のみ → LANで概ね **<100ms**
- 新規視聴者の接続時とその後の定期PLIで、キーフレームを確実に届ける
- ビットレート/解像度/フレームレートは各スクリプトのENV/ENCODERで調整可能
