# 免責事項
本リポジトリのコード・設計・その他成果物を利用したことによる、不具合・トラブルは責任を負いかねます。
自身の製作している環境・ロボットで問題がないか充分に確認したうえで活用してください。
KINKI KNIGHTSとして、本番環境で使用しているシステムのためPRの内容によってはマージしない場合もあります。

# RescuePiSystem

レスキューロボコンでRaspberryPiをロボット側コンピュータとして使用するために開発したシステムです。
次の内容が含まれます。

- RaspberryPiの初期セットアップスクリプト
- RaspberryPi管理・運用画面(Webアクセス)
- 映像伝送
- 音声伝送

# 設計と構成
ロボット複数台に対し1台のサーバーが対応します。
映像や音声データについては、各ロボットからのデータを一度サーバ役のPCで受けてから、解析用PCや操作用PCに配信します。
これにより、機体を接続しているWiFiの転送帯域を削減しています。

サーバー役のPCのIPは固定として、各ロボットがクライアントとなりサーバーに接続します。
有線・無線を切り替えてロボットのIPが変わっても、通信設定を変える必要はありません。

※JoyNodeWebのシステムのみ、コントローラ役のPCからロボットに通信するためロボットIP変更に伴って通信先の変更が必要です。

# 各ロボットの構成
## MasterControl
メインのコントロール画面は、HTTPで各ロボットのIPアドレスにアクセスすることで表示できます。
コントロール画面では、プログラムの起動・停止、カメラやマイクの選択ができます。

## MicPublisher
サーバー上で動作するマイクハブにUSBマイクの音声を送信します。
USBマイクのデバイスはマスターコントロールから設定可能です。

## CameraPublisher
USB接続されたカメラデバイスの映像をサーバー上のWebRTCリレーに送信します。
カメラデバイスはマスターコントロールから設定可能です。

## ROS2ワークスペース
各機体共通で使用するROS2パッケージを `robot/ros2/` に含みます（すべて colcon ビルド対象）。

| パッケージ | 取り込み方 | 役割 |
|---|---|---|
| `joy_node_web` | submodule | Web ゲームパッド → `sensor_msgs/Joy`（:8700 `/joys`） |
| `kk_can_bringup` | このリポジトリの実体 | MCP2515 SocketCAN + `ros2_socketcan` ブリッジの bringup |
| `ros2_socketcan`（+`ros2_socketcan_msgs`） | submodule（上流 OSS） | SocketCAN ⇔ ROS 2 ブリッジ |
| `gm6020_control` | submodule | DJI GM6020 モータのサーボ（位置）制御。**実アクチュエータを駆動する** → [安全上の注意](#gm6020_control-の安全上の注意) |

### gm6020_control の安全上の注意

`gm6020_control` は **実際の GM6020 モータへ CAN 指令を送る**パッケージです。
`ros2_socketcan` ブリッジ（`/to_can_bus`）を経由せず、raw SocketCAN で `can0` を
直接読み書きします（GM6020 の約 1 kHz フィードバックを取りこぼさないため）。
動作確認のつもりで気軽に起動してよいノードではありません。

- **`config/gm6020_dual.yaml` の `start_enabled: false` が安全柵**です。起動直後は
  出力 0 のままで、`~/enable` に `true` を publish した**瞬間からモータが回ります**。
  この既定値を `true` に変えないこと。
- **使うのは `gm6020_dual.launch.py`** です。`gm6020.launch.py` は既定の params_file が
  現存しない `config/gm6020.yaml` を指しているため、**そのままでは起動できません**
  （使う場合は `params_file:=` を必ず明示する）。
- 同一 control id（`0x1FF` / `0x2FF`）を共有するモータは 1 フレームにまとめて送る必要が
  あるため、単体ノード（`gm6020_node`）と dual ノード（`gm6020_dual_node`）を
  **同時に走らせないこと**（互いの指令を 0 で上書きし合う）。
- PID ゲインは復元時の既定値で **実機では未調整**です。通電前に
  [`robot/ros2/gm6020_control/README.md`](robot/ros2/gm6020_control/README.md) を読むこと。


# 変更履歴
KinkiKnights レスキューロボットの **号機 (Raspberry Pi) と運用サーバ (kkrtx) と
操作画面** を一本にまとめたリポジトリ。2026-08 に以下 4 つを履歴つきで統合した。

| 統合元 | 引き継いだもの |
|---|---|
| `kk_rescue26_pi` | 号機側プログラム一式・セットアップスクリプト |
| `MicStreamRes2026` | マイク集約ハブ・号機 publisher |
| `VideoControlSystemRes2026` | 操作画面サーバ・PTT 音声中継 |
| `ClaudeShareContents` の `webrtc-camera/` | WebRTC 中継 (SFU) と視聴クライアント |

分割していた頃は、送信側と受信側が別リポジトリにあるせいでプロトコルが片側だけ
更新される事故が実際に起きていた。**対向どうしを同じコミットで直せる**ことが
統合の目的なので、コンポーネントを勝手に外へ出さないこと（→ [CLAUDE.md](CLAUDE.md)）。

## 全体像

```
[号機 Pi × 5]                          [kkrtx = 192.168.10.3]         [操作端末]
 master_control    :80  ◀── reboot/shutdown ── control_ui   :80         ブラウザ 5 モード
 camera_publisher  ──WebRTC push──▶          webrtc_relay  :8080  ──▶  control / analytics
 mic_publisher     ──PCM push─────▶          mic_hub       :8770        engineer / reporter
 joy_node_web      :8700 ◀── /joys ──        voice_comm    :8766        master
 kk_can_bringup    ──▶ CAN                    └▶ damiyan-detector :8771-3 (外部リポジトリ)
 gm6020_control    ──▶ CAN (raw SocketCAN。実モータを駆動する)
```

| ポート | プロセス | ホスト |
|---|---|---|
| 80 | `robot/master_control` | 号機 Pi |
| 8700 | `robot/ros2/joy_node_web` (`/joys`) | 号機 Pi |
| 80 | `server/control_ui` | kkrtx |
| 8080 | `server/webrtc_relay` (`/ws`, `/pis`) | kkrtx |
| 8766 | `server/voice_comm` (`/voice`) | kkrtx |
| 8770 | `server/mic_hub` (`/ingest/<号機>`, `/<号機>`) | kkrtx |
| 8771-8773 | damiyan-detector (外部・mic_hub の購読者) | kkrtx |

## ディレクトリ構成

```
config/     号機・機器アドレスの単一の真実 (units.json)
robot/      号機 Pi に載るもの  … master_control / camera_publisher / mic_publisher / ros2
            device_config.py = カメラ/マイクのデバイス設定 (UI から候補を選ぶ)
server/     kkrtx に載るもの    … control_ui(汎用ツール) / voice_comm / mic_hub / webrtc_relay
            ※競技用の操作画面 5 モードは別リポジトリ res26_control_ui(:8001)へ分離
deploy/     セットアップ        … robot/*.sh, server/kkrtx_setup.sh, systemd/*.service.in
tools/      検証用              … mic_selftest.py, headless_viewer
docs/       ドキュメント        … architecture / protocols / spec / operations
archive/    現役でない参考資料  … 旧 mic relay, 旧プロトタイプ画面, 原典仕様
```

`robot/ros2/` の 3 つは他ロボット・上流 OSS と共有するパッケージなので
**submodule**（固定コミット参照）。実体をコピーせず、どの版を積んでいるかを
gitlink の SHA で示す。clone には必ず `--recursive` を付ける。

| パス | URL | 公開 | 備考 |
|---|---|---|---|
| `robot/ros2/joy_node_web` | `https://github.com/KinkiKnights/joy_node_web.git` | public | 他ロボットでも使う共有パッケージ |
| `robot/ros2/ros2_socketcan` | `https://github.com/autowarefoundation/ros2_socketcan.git` | public（外部 OSS） | KinkiKnights の管理下でないため **`https://`** で参照する（SSH キーが無い環境でも `--recursive` clone できる） |
| `robot/ros2/gm6020_control` | `git@github.com:KinkiKnights/gm6020_control.git` | **private** | private なので **SSH 形式**。clone には GitHub の SSH キー登録が必要 |

固定しているコミットは `git ls-files -s robot/ros2` で確認できる（gitlink の SHA）。
`--recursive` を付け忘れたら `git submodule update --init --recursive`。

## セットアップ

**号機 (Raspberry Pi 5 / Ubuntu 24.04 / ROS 2 Jazzy)** — ワンライナーで完結する。

```bash
curl -fsSL https://raw.githubusercontent.com/KinkiKnights/RescuePiSystem/main/deploy/robot/kk_robot_setup.sh | bash
```

**kkrtx (運用サーバ)** — clone してから実行する。

```bash
git clone --recursive git@github.com:KinkiKnights/RescuePiSystem.git ~/kk_ws/src/RescuePiSystem
~/kk_ws/src/RescuePiSystem/deploy/server/kkrtx_setup.sh
```

`--recursive` で `robot/ros2/` の submodule 3 件も同時に展開される。うち
`gm6020_control` は **private リポジトリ**なので、GitHub の SSH キーが登録済みで
ある必要がある（号機セットアップはキー登録から始まるので通常は問題にならない）。

詳細は [docs/robot.md](docs/robot.md)（号機）と [docs/operations.md](docs/operations.md)（kkrtx）。

kkrtx を**常駐させずに**使う場合（競技や試験のときだけ立ち上げる運用）は、
`kkrtx_setup.sh` を `SETUP_SERVICES=0` で実行して依存導入と relay のビルドだけ
済ませ、4 サービスの起動・停止は `deploy/server/server_ctl.sh` で行う。

```bash
SETUP_SERVICES=0 ~/kk_ws/src/RescuePiSystem/deploy/server/kkrtx_setup.sh
~/kk_ws/src/RescuePiSystem/deploy/server/server_ctl.sh start    # stop / restart / status / logs
```

systemd 常駐と `server_ctl.sh` を**同時に使わないこと**（ポートが衝突する）。

ディスク容量に応じた `MIC_HUB_MAX_GB` など**機体ごとの運用値**は
`~/.config/rescue-pi/server.env` に置く（リポジトリ外・git 管理外）。優先順位は
コマンドライン > `server.env` > スクリプト既定値。
詳細は [docs/operations.md](docs/operations.md)。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 全体構成・データフロー・起動順・デプロイ先マップ |
| [docs/robot.md](docs/robot.md) | 号機のセットアップ、CAN、submodule 運用 |
| [docs/operations.md](docs/operations.md) | kkrtx の配備・運用・トラブルシュート |
| [docs/control-ui.md](docs/control-ui.md) | 操作画面 5 モードの使い方と構成 |
| [docs/spec.md](docs/spec.md) | 操作画面の要求仕様 |
| [docs/mic-system.md](docs/mic-system.md) | マイク集約の設計と使い方 |
| [docs/webrtc-camera.md](docs/webrtc-camera.md) | カメラ映像経路の設計と使い方 |
| [docs/usb-wifi-dongle.md](docs/usb-wifi-dongle.md) | USB WiFi ドングル (RTL8811AU) の手順 |
| [docs/devices.md](docs/devices.md) | カメラ / マイクのデバイス設定（候補選択・即反映・優先順） |
| [docs/protocols/](docs/protocols/) | ワイヤ契約（mic / joy / webrtc / state / damiyan） |

## 設定

号機の IP・機器一覧・ポートは [`config/units.json`](config/units.json) が唯一の真実。
`units[n].addrs` は**優先順の候補配列**（無線 → 調整無線 → 有線 → mDNS）で、
control_ui が live な経路を自動採用する。ping 監視の対象もここから導出される。

**号機 ID は `KK01`〜`KK05` で統一**している（`units[n].pi_id`）。カメラ配信の
`PI_ID` は既定でホスト名を大文字化するので、ホスト名を `kk0N` にしておけば
操作画面が購読する ID と自動で一致する。

**カメラとマイクのデバイスは master_control の Web UI から選ぶ**
（`http://<号機IP>/` の DEVICE CONFIGURATION）。接続されている候補が列挙され、
選んだ内容は `~/.config/rescue-pi/devices.json` に記録される。指定は
`/dev/v4l/by-id/...` と `hw:CARD=<名前>,DEV=0` の**安定した識別子**なので、
USB を抜き差ししても、カメラやマイクを交換しても追従できる。「保存して即反映」で
その場で反映される。詳細は [docs/devices.md](docs/devices.md)。

号機ごとの運用値（ハブ URL・号機番号など）は `/etc/default/*` と
`devices.json` に置く。リポジトリ内の設定ファイルに書くと `git pull` で
号機固有の差分が巻き戻るため。
