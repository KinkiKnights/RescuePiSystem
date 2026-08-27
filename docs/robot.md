# 号機 (Raspberry Pi) のセットアップと構成

KinkiKnights レスキューロボットの Raspberry Pi 上で動作するプログラム一式。
リポジトリ全体の地図は [../README.md](../README.md)、kkrtx 側は
[operations.md](operations.md) を参照。

- **対象ハード**: Raspberry Pi 5 (aarch64) / **OS**: Ubuntu 24.04 LTS / **ROS**: ROS 2 Jazzy
- 複数台の Pi へ同一手順で展開可能(`PI_ID` はホスト名から自動生成: kk05 → KK05)

## クイックスタート

新しい Pi では次のワンライナーを貼り付けるだけで完了します。**`kk_robot_setup.sh`
自身が自己完結ブートストラップ**になっており、SSH キーの生成・表示・GitHub 登録
待ち → 自分のリポジトリを clone → clone 先から再実行、までを1本で行います
(依存導入・ビルド・自動起動設定まで全自動)。

```bash
curl -fsSL https://raw.githubusercontent.com/KinkiKnights/RescuePiSystem/main/deploy/robot/kk_robot_setup.sh | bash
```

中継サーバ(relay)IP やカメラソースを変える場合は、`bash` 側に環境変数を付けます
(clone 後の再実行まで引き継がれます):

```bash
curl -fsSL https://raw.githubusercontent.com/KinkiKnights/RescuePiSystem/main/deploy/robot/kk_robot_setup.sh | RELAY_HOST=192.168.137.1 bash
# CSI カメラの例: ... | CAM1_SRC=libcamerasrc bash
```

> **リポジトリが非公開の場合**は raw URL でスクリプト本体を取得できません。その場合は
> 下の「手動で clone して実行する場合」の手順(またはキー先行ワンライナー)を使ってください。

実行の流れ:まず GitHub 用 SSH キーが未登録なら公開鍵を表示し、GitHub に登録して
`Enter` を押すまで待ちます(登録済みならスキップ)。続いてリポジトリが手元に無ければ
自動で clone し、clone 先の `kk_robot_setup.sh` を再実行します。その後**実行する処理を
選ぶメニュー**が表示されます(初回は `1) 新規セットアップ` を選択)。最初の `sudo` で
1度だけパスワードを聞かれます(以後は NOPASSWD 設定)。
`PI_ID` はホスト名から自動生成されます(例: `kk06` → `KK06`)。
USB WiFi ドングル(RTL8811AU)のドライバ導入は既定で**無効**です。DKMS ビルドには
稼働カーネルに一致する `linux-headers-$(uname -r)` が必要で、ヘッダーが入手できない
古いカーネルの Pi では導入が失敗してしまうためです。ドングルを使う Pi では、先に
`sudo apt install linux-image-raspi linux-headers-raspi` で最新カーネルへ更新して
再起動し、ワンライナー先頭に `SETUP_WIFI_DONGLE=1` を付けて実行してください
(接続設定は [docs/usb-wifi-dongle.md](usb-wifi-dongle.md) 参照)。

<details><summary>非公開リポジトリの場合 / 手動で clone して実行する場合</summary>

**非公開リポジトリで raw URL が使えない場合**は、キーを先に登録してから clone する
自己完結ワンライナーを使います(キー登録 → SSH で clone → 本体実行):

```bash
bash -c 'set -e; install -d -m700 ~/.ssh; k=$HOME/.ssh/id_ed25519; [ -f "$k" ] || ssh-keygen -t ed25519 -C "$(hostname)-github" -N "" -f "$k" -q; echo "===== この公開鍵を GitHub に登録してください ====="; echo "  アカウント: https://github.com/settings/keys / リポジトリ単位: Settings -> Deploy keys"; cat "$k.pub"; echo "=================================================="; read -rp "登録が完了したら Enter: " < /dev/tty; export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"; mkdir -p ~/kk_ws/src; [ -d ~/kk_ws/src/RescuePiSystem ] || git clone --recursive git@github.com:KinkiKnights/RescuePiSystem.git ~/kk_ws/src/RescuePiSystem; ~/kk_ws/src/RescuePiSystem/deploy/robot/kk_robot_setup.sh'
```

**手動で clone して実行する場合**(SSH キー登録後):

```bash
mkdir -p ~/kk_ws/src && cd ~/kk_ws/src
git clone --recursive git@github.com:KinkiKnights/RescuePiSystem.git   # submodule も取得
./RescuePiSystem/deploy/robot/kk_robot_setup.sh
```

`--recursive` を付け忘れた場合は `git -C RescuePiSystem submodule update --init` で
joy_node_web(submodule)を取得してください(セットアップスクリプトは自動で init します)。

なお `kk_robot_setup.sh` は同じ `setup/` 内の `env_setup.sh` / `app_setup.sh` /
`update.sh` を呼び出します。手元にサブスクリプトが無い状態(raw URL の `curl | bash`
など)で実行した場合は、SSH キー登録後に**自動でリポジトリを clone し、clone 先の
`kk_robot_setup.sh` を再実行**します。

各サブスクリプトは単体でも実行できます(環境変数は既定値を使用):

```bash
./RescuePiSystem/deploy/robot/env_setup.sh   # 基本設定 + ROS 導入のみ
./RescuePiSystem/deploy/robot/app_setup.sh   # RescuePiSystem の環境構築のみ
./RescuePiSystem/deploy/robot/update.sh      # 更新して再ビルド
```
</details>

セットアップ後、`http://<PiのIP>/` の Web UI から camera / joy_node_web / mic を起動できます。

## 構成

Pi 上で動くプログラムを本リポジトリに集約します。joy_node_web は他ロボットでも
使う共有パッケージのため **submodule**(固定コミットへの参照)として含みます。
外部 OSS の ros2_socketcan のみ `deploy/robot/rescue_pi_system.repos` で参照します。

```
RescuePiSystem/
├── master_control/      # プログラム起動管理サーバ (port 80, systemd 自動起動)
│   ├── master_server.py #   Web UI から programs.json のプログラムを起動/停止
│   └── programs.json    #   このPi固有の登録内容 (セットアップスクリプトが生成)
├── camera_publisher/    # USB カメラ → WebRTC 配信 (外部 relay へ)
├── mic_publisher/       # USB マイク → FLAC ロスレス TCP 配信 (:5005)
├── ros2/
│   ├── joy_node_web/    # [submodule] Web ゲームパッド → sensor_msgs/Joy (colcon 対象, :8700/joy)
│   └── kk_can_bringup/  # MCP2515 SocketCAN + ros2_socketcan bringup (colcon 対象)
└── setup/
    ├── kk_robot_setup.sh      # オーケストレーター (環境変数定義 / SSH キー登録待ち / 実行メニュー)
    ├── env_setup.sh           # 基本設定 (sudo/swap/WiFi) と ROS 2 の導入
    ├── app_setup.sh           # RescuePiSystem の環境構築 (依存/clone/build/systemd 生成)
    ├── can_setup.sh           # CAN (MCP2515 HAT) のシステム側 bring-up (SETUP_CAN=1 で有効)
    ├── update.sh              # RescuePiSystem を最新に更新して再ビルド
    └── RescuePiSystem.repos   # 外部依存 (ros2_socketcan) の vcstool 定義

# .repos で ~/kk_ws/src に別途 clone される (colcon ビルド対象):
#   ros2_socketcan/   CAN 通信
```

### システム全体像

```
[Raspberry Pi]                                 [他デバイス]
  master_control (:80) ──起動/停止──┐
  camera_publisher ──WebRTC──────────→ relay SFU (:8080) → web ビューア
  mic_publisher (:5005) ──FLAC/TCP──→ mic_receiver
  joy_node_web (:8700) ← ブラウザ操作 → /joy → ros2_socketcan → CAN
```

## 外部リポジトリとの関係(メンテナンス方針)

**原則: 各コンポーネントの実体はただ1つの場所にのみ置く。** 2026-08 に号機・kkrtx・
操作画面・relay を本リポジトリへ統合したので、以下だけが外部に残ります。

| コンポーネント | 正式な置き場所 (single source of truth) |
|---|---|
| master_control / camera_publisher / mic_publisher / mic_hub / control_ui / webrtc_relay | **このリポジトリ** |
| joy_node_web | [KinkiKnights/joy_node_web](https://github.com/KinkiKnights/joy_node_web)(他ロボットでも使う共有パッケージ。**submodule** として固定コミットで参照) |
| ros2_socketcan | [autowarefoundation/ros2_socketcan](https://github.com/autowarefoundation/ros2_socketcan)(上流 OSS。取り込まず `.repos` で参照) |
| damiyan-signal-processing | 別リポジトリ(音声解析。契約は [protocols/damiyan.md](protocols/damiyan.md) に明文化) |

### joy_node_web(submodule)の運用

joy_node_web は他ロボットでも使う共有パッケージのため、単一の真実は
[KinkiKnights/joy_node_web](https://github.com/KinkiKnights/joy_node_web) に置き、本リポジトリは
`robot/ros2/joy_node_web` に **submodule(固定コミットへの参照)** として含みます。コードは複製されず、
どの版を積んでいるかは submodule のコミット SHA で明示されます(フリートでの版管理が明確)。

```bash
# 取得(clone 時に付け忘れた場合)
git submodule update --init robot/ros2/joy_node_web

# 上流 (joy_node_web) の最新を取り込み、親リポジトリのポインタを更新
git submodule update --remote robot/ros2/joy_node_web
git add robot/ros2/joy_node_web && git commit -m "Bump joy_node_web submodule"

# joy_node_web 自体を修正する場合は submodule 内で作業してから push し、
# 親リポジトリでポインタ更新をコミットする
cd robot/ros2/joy_node_web && git checkout main && git pull
#   … 編集 … → git commit → git push
cd ../.. && git add robot/ros2/joy_node_web && git commit -m "Bump joy_node_web submodule"
```

### プロトコル契約(乖離防止)

ネットワークで結合する相手(relay / receiver)とはプロトコル契約で結合しています。
**片側を変更したら、必ず対向リポジトリも同時に更新すること。**

- `camera_publisher` ⇔ relay: WebSocket シグナリング (`ws://<relay>:8080/ws`)
- `mic_publisher` ⇔ receiver: FLAC over TCP (`:5005`)

詳細は各ディレクトリの README を参照。

### 外部依存の更新

```bash
vcs pull ~/kk_ws/src          # ros2_socketcan を上流に追従
cd ~/kk_ws && colcon build
```

## 運用メモ

- master control は systemd (`master-control.service`) で自動起動。ユニットファイルは
  `deploy/robot/app_setup.sh` だけが生成します(リポジトリ内に .service ファイルの複製を置かない)。
- `robot/master_control/programs.json` はセットアップスクリプトが Pi ごとに生成する運用ファイルです。
  リポジトリには KK05 の実例をコミットしてあります。
- サービス操作: `sudo systemctl restart master-control.service` / `journalctl -u master-control -f`

## CAN (MCP2515 SPI HAT) 接続

モータ/サーボ等を CAN で制御する号機向けに、MCP2515 CAN HAT の SocketCAN と
[ros2_socketcan](https://github.com/autowarefoundation/ros2_socketcan) ブリッジを構成します。
**この設定は既定で無効**で、**MCP2515 HAT を物理的に装着した号機でのみ**有効化します
(HAT が無いと `can0` が出ず、`can0-setup.service` が待機後に失敗します)。

- **ROS パッケージ** `robot/ros2/kk_can_bringup`(colcon 対象)が ros2_socketcan の
  `socket_can_bridge.launch.xml`(`enable_can_fd=false`)を include します。
  `ros2_socketcan` 本体は `deploy/robot/rescue_pi_system.repos` 経由で取得・ビルドされます
  (取り込まず上流参照)。
- **システム側 bring-up** は `deploy/robot/can_setup.sh` が担当:Device Tree overlay の追記、
  `/etc/default/kk-can` の生成、`/usr/local/sbin/can0-up.sh`(SocketCAN link up)と
  `/usr/local/sbin/kk-can-ros-launch.sh`(ブリッジ起動)の設置、systemd ユニット
  (`can0-setup.service` → `kk-can-ros.service`)の生成・有効化を行います(冪等)。

### 有効化のしかた

新規セットアップのワンライナー先頭に `SETUP_CAN=1` を付けると、`app_setup.sh` の
colcon ビルド後に `can_setup.sh` が実行されます:

```bash
curl -fsSL https://raw.githubusercontent.com/KinkiKnights/RescuePiSystem/main/deploy/robot/kk_robot_setup.sh | SETUP_CAN=1 bash
```

既にセットアップ済みの号機に後から追加する場合は単体実行できます
(`app_setup.sh` の colcon ビルドが済んでいる前提):

```bash
SETUP_CAN=1 ./deploy/robot/can_setup.sh
```

**Device Tree overlay の反映には一度再起動が必要**です。再起動後は
`can0-setup.service` / `kk-can-ros.service` が自動起動します。

### パラメータ(既定値・環境変数で上書き可)

| 変数 | 既定 | 説明 |
|------|------|------|
| `CAN_BITRATE` | `1000000` | CAN ビットレート(1 Mbit/s) |
| `CAN_OSCILLATOR_HZ` | `16000000` | MCP2515 水晶振動子 (Hz) |
| `CAN_INTERRUPT_GPIO` | `24` | MCP2515 INT ピン (BCM) |
| `DT_OVERLAY` | `mcp2515-can0` | Device Tree overlay 名 |
| `ROS_DOMAIN_ID` | `0` | ROS 2 ドメイン ID。**号機内の全 ROS ノードで揃える**こと |

`ROS_DOMAIN_ID` は号機ごとに固有ではなく、**その号機の他 ROS ノード(joy_node_web 等)と
同じ値**にする必要があります(異なると CAN トピックを相互に読めません)。既定は 0
(RescuePiSystem の他ノードの既定と一致)。号機で ROS グラフを分離している場合のみ、
セットアップ時に上書きします(例:`ROS_DOMAIN_ID=5 SETUP_CAN=1 ./deploy/robot/can_setup.sh`)。
上書き後は `/etc/default/kk-can` に保存され、両サービスがこれを参照します。

### 動作確認

```bash
ip link show can0                                  # <UP> と bitrate を確認
systemctl status can0-setup kk-can-ros
ros2 topic list                                    # /from_can_bus /to_can_bus
candump can0                                        # フレーム受信を確認 (can-utils)
```
