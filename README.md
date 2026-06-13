# webrtc-camera — Raspberry Pi カメラの低遅延WebRTC配信

複数のRaspberry Pi (Ubuntu 24.04, Pi 4/5想定) に接続したカメラ映像を、
**高性能な中継サーバー(SFU)経由でブラウザへWebRTC配信**するシステム。
低遅延・低CPU負荷を重視し、可能な範囲でハードウェアアクセラレーションを活用する。

> 前提: ローカルネットワーク内・管理された端末での利用。NAT越え/TURN/WebRTC不可時の
> フォールバックは想定しない（STUN/TURN不要）。

## 主な機能

- **複数ラズパイの多重化**: 各PiはIDを申告して接続。ビュアーはIDを指定して見たいPiを選択。
- **カメラ切替 (camChange)**: ビュアーからPiのカメラ番号を切替（無停止・再ネゴ不要）。
  - 番号 **1 = カメラ(初期値)**、**2.. = 追加カメラ**
  - 番号 **0 = スクリーン**（既定では無効。`CAM0`を設定すると有効化／待機ソースのCPUを避けるため）
- **クライアントライブラリ**: `connect(videoEl, id)` で接続、`camChange(n)` で切替。
- **ハードウェアアクセラレーション**: Pi4はHW H.264エンコード、Pi5はSW(低遅延)。

## アーキテクチャ

```
 複数のRaspberry Pi                高性能な中継サーバー (SFU)            複数ブラウザ
┌──────────────────┐  WebRTC      ┌──────────────────────────┐ WebRTC  ┌──────────┐
│ PI01: cam/screen  │ ──publish──▶ │ ID毎にストリーム管理        │ ──────▶ │ ?id=PI01 │
│ (input-selector)  │             │ ・RTPファンアウト(再エンコ無) │        └──────────┘
├──────────────────┤             │ ・WebSocketシグナリング      │ ──────▶ ┌──────────┐
│ PI02: cam/screen  │ ──publish──▶ │ ・camChangeを該当Piへ転送    │         │ ?id=PI02 │
└──────────────────┘             │ ・Web静的配信               │         └──────────┘
                                  └──────────────────────────┘
   camChange(n) ◀───── 制御メッセージを逆流して該当Piへ転送 ◀─────── ビュアー操作
```

- **2レグともWebRTC。** Piは上り1本だけ送信。視聴者が増えてもPi負荷は一定（ファンアウトはSFU）。
- **SFU方式**: 中継サーバーは再エンコードせずRTPを転送（=低遅延・低CPU）。LANで概ね <100ms。
- **コーデックはH.264統一**: Pi4のHWエンコーダ・全ブラウザが対応、低遅延設定が容易。
- **シグナリング**: WebSocket(JSON)。publisherはofferer、viewerはanswerer。LAN内なのでICEサーバ不要。

## ハードウェアアクセラレーション方針（重要）

| 機種   | H.264エンコード             | 備考 |
|--------|----------------------------|------|
| Pi 4   | `v4l2h264enc`（HWエンコーダ）| VideoCoreの専用H.264エンコーダ。CPUほぼ不使用。|
| Pi 5   | `x264enc`（ソフトウェア）    | **Pi5は専用H.264 HWエンコーダを廃止**。CPU(A76)が高速なので720p/30は実用的。|

USBカメラ自体がH.264出力できる場合は、それを無加工で流せば最小負荷。

## カメラ切替の仕組み

Pi側は全入力ソースを **GStreamerの `input-selector`** に束ね、共通解像度に正規化してから
1つのエンコーダ→webrtcbinへ流す。camChangeは `active-pad` を切替えるだけなので、
**WebRTCの再ネゴシエーション不要・トラックは安定したまま**で、ビュアーも再接続不要。
切替直後はSFUがPLIを送り、webrtcbin→エンコーダにキーフレームを促す。

## ディレクトリ構成

```
webrtc-camera/
├── relay/                 # 中継サーバー (Go + Pion SFU)
│   └── main.go            #   ID多重化 / ファンアウト / camChange転送 / シグナリング / Web配信
├── web/
│   ├── webrtc-camera.js   # 視聴クライアント・ライブラリ (connect / camChange)
│   └── index.html         # ライブラリを使うサンプルUI (ID入力・カメラ切替ボタン)
├── publisher/             # Piパブリッシャ (GStreamer webrtcbin)
│   ├── publish.py         #   ID申告 / input-selector複数ソース / camChange
│   ├── publish-pi4.sh     #   Pi4用: HWエンコード(v4l2h264enc)
│   ├── publish-pi5.sh     #   Pi5用: SWエンコード(x264enc)
│   └── publish-test.sh    #   このPCでの動作確認用 (Webカメラ/videotestsrc)
├── tools/
│   └── headless-viewer/   # ブラウザ無しで配信経路を検証するテスト用視聴クライアント
└── README.md
```

## クライアントライブラリ API (`web/webrtc-camera.js`)

```js
const cam = new WebRTCCamera({
  // server: "ws://host:8080/ws",   // 省略時は現在のホストから自動推定
  onStatus: (s)  => {},              // "connecting"|"connected"|"closed"|"error:<msg>"
  onStats:  (st) => {},              // {width,height,kbps,fps,jitterMs} 1秒ごと
});

cam.connect(videoElement, "PI01");   // ビデオ要素とPiのID(4文字程度)を渡すと自動接続
cam.camChange(0);                    // 0=スクリーン
cam.camChange(1);                    // 1=カメラ(初期値)
cam.camChange(2);                    // 2=追加カメラ
const ids = await cam.listPis();     // 接続中のPi ID一覧
cam.disconnect();
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
# Pi4のHWエンコードには v4l2h264enc (gstreamer1.0-plugins-good / カーネルV4L2)
# CSIカメラには gstreamer1.0-libcamera
```

## 使い方

### 1. 中継サーバーを起動（高性能なマシンで）

```bash
cd relay && go build -o relay .       # 初回のみ
./relay -addr :8080 -web ../web
```

### 2. Raspberry Piでパブリッシャを起動（各Piで、IDを変える）

```bash
# Pi4 (HWエンコード)、ID=PI01
PI_ID=PI01 SERVER=ws://<サーバーIP>:8080/ws ./publisher/publish-pi4.sh
# Pi5 (SWエンコード)、ID=PI02
PI_ID=PI02 SERVER=ws://<サーバーIP>:8080/ws ./publisher/publish-pi5.sh
```

カメラ/パラメータは環境変数で上書き（各スクリプト冒頭のコメント参照）:
- `PI_ID`   … このPiのID（ビュアーが指定する4文字程度の名前）
- `CAM1..`  … カメラ入力（`libcamerasrc` / `v4l2src device=/dev/videoN`。複数台はCAM2,CAM3...）
- `CAM0`    … スクリーン入力（既定で無効。設定時のみ有効：X11は`ximagesrc`、Waylandは`pipewiresrc`等）
- `DEFAULT_CAM` … 起動時の選択番号（既定 1）
- `ENCODER` / `WIDTH` / `HEIGHT` / `FPS` / `SERVER`

### 3. ブラウザで視聴

`http://<サーバーIP>:8080/` を開き、IDを入力して「接続」。
`http://<サーバーIP>:8080/?id=PI02` のようにURLでID指定も可。
ヘッダーのカメラ番号ボタン(0/1/2)で切替。

## このPC(Ubuntu 24.04)での動作確認

ラズパイ実機が無いため、このPCのWebカメラ/合成映像を「Pi役」として全経路を検証する。

```bash
# 中継サーバー
./relay/relay -addr :8080 -web web &

# Pi役1 (PI01): 1=実Webカメラ, 2=ボール (画面取得は既定で無効)
PI_ID=PI01 ./publisher/publish-test.sh &

# Pi役2 (PI02): 合成映像のみ (Webカメラは1台しか開けないため)
PI_ID=PI02 CAM1="videotestsrc pattern=snow" CAM2="videotestsrc pattern=circular" \
  ./publisher/publish-test.sh &

# ブラウザで http://localhost:8080/ (ID=PI01) / ?id=PI02 を開く
# またはヘッドレス視聴クライアントで機械的に検証:
go build -o tools/headless-viewer/headless-viewer ./tools/headless-viewer
tools/headless-viewer/headless-viewer -id PI01 -dur 6s -cam 0 -camAt 1s -out /tmp/x.h264
ffmpeg -i /tmp/x.h264 -update 1 /tmp/last.png   # 切替後(画面=カラーバー)が映る
```

### 検証結果（このPCで実施済み）

- 実Webカメラ → GStreamer(x264/input-selector) → webrtcbin → relay(Pion SFU) → 視聴 が疎通。
  受信は **1280x720 / H.264 Constrained Baseline / 約2.5Mbps** でデコード可能。
- **複数ラズパイのID指定ルーティング**: PI01/PI02を同時接続し、`?id=`で別々の映像を取得。
  `GET /pis` で `["PI01","PI02"]` を確認。
- **camChange**: ビュアーから PI01 を `1`(Webカメラ) / `2`(ボール) へ無停止で切替えられることを、
  各切替後のデコードフレームで確認（画面取得`0`はCAM0設定時のみ有効）。
- **ファンアウト**: 複数ビュアー同時接続で各自が同一ストリームを受信（Pi上りは1本のまま）。
- ヘッドレス視聴はブラウザと同一の署名/SDP/ICE/RTP/camChange経路を通るため、
  ブラウザ視聴経路の検証を兼ねる。

## 低遅延のための設計

- `tune=zerolatency` / `speed-preset=ultrafast` / `constrained-baseline` / Bフレーム無し
- `rtph264pay aggregate-mode=zero-latency`、`queue ... leaky=downstream` で滞留を抑制
- SFUは再エンコードせず転送のみ → LANで概ね <100ms
- 新規視聴・camChange時にPLIでキーフレームを即時取得
- ビットレート/解像度/フレームレートは各スクリプトのENV/ENCODERで調整可能
