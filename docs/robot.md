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

セットアップ後、`http://<PiのIP>/` の Web UI から camera / joy_node_web / mic を
起動できます。同じ画面の **DEVICE CONFIGURATION** でカメラとマイクを
（接続されている候補から）選べます → [devices.md](devices.md)。

## 構成

Pi 上で動くプログラムを本リポジトリに集約します。他ロボットや上流 OSS と共有する
ROS 2 パッケージ(`joy_node_web` / `ros2_socketcan` / `gm6020_control`)は
`robot/ros2/` 配下に **submodule**(固定コミットへの参照)として含みます。
ファイルを直接コピーする「ベンダリング」はしません — どの版を積んでいるかが
gitlink の SHA で明示され、上流への追従が切れないためです。

```
RescuePiSystem/
├── master_control/      # プログラム起動管理サーバ (port 80, systemd 自動起動)
│   ├── master_server.py #   Web UI から programs.json のプログラムを起動/停止
│   └── programs.json    #   このPi固有の登録内容 (セットアップスクリプトが生成)
├── camera_publisher/    # USB カメラ → WebRTC 配信 (外部 relay へ)
├── mic_publisher/       # USB マイク → 16kHz PCM を集約ハブへ push
├── ros2/
│   ├── joy_node_web/    # [submodule] Web ゲームパッド → sensor_msgs/Joy (colcon 対象, :8700/joy)
│   ├── kk_can_bringup/  # MCP2515 SocketCAN + ros2_socketcan bringup (colcon 対象)
│   ├── ros2_socketcan/  # [submodule] 上流 OSS。SocketCAN ⇔ ROS 2 ブリッジ (colcon 対象)
│   │                    #   ros2_socketcan / ros2_socketcan_msgs の 2 パッケージ
│   └── gm6020_control/  # [submodule] GM6020 サーボ制御 (colcon 対象)
│                        #   ▲ 実モータを駆動する。下の「GM6020 モータ制御」を読むこと
└── setup/
    ├── kk_robot_setup.sh      # オーケストレーター (環境変数定義 / SSH キー登録待ち / 実行メニュー)
    ├── env_setup.sh           # 基本設定 (sudo/swap/WiFi) と ROS 2 の導入
    ├── app_setup.sh           # RescuePiSystem の環境構築 (依存/clone/build/systemd 生成)
    ├── can_setup.sh           # CAN (MCP2515 HAT) のシステム側 bring-up (SETUP_CAN=1 で有効)
    └── update.sh              # RescuePiSystem を最新に更新して再ビルド

# colcon ビルド対象はすべて RescuePiSystem のツリー内にある。
# ~/kk_ws/src に外部リポジトリを別途 clone しない (同名パッケージが二重になる)。
```

### システム全体像

```
[Raspberry Pi]                                 [他デバイス]
  master_control (:80) ──起動/停止──┐
  camera_publisher ──WebRTC──────────→ relay SFU (:8080) → web ビューア
  mic_publisher (:5005) ──FLAC/TCP──→ mic_receiver
  joy_node_web (:8700) ← ブラウザ操作 → /joy → ros2_socketcan → CAN
  gm6020_control ──────────────────────── raw SocketCAN ──────→ CAN (GM6020)
```

## 外部リポジトリとの関係(メンテナンス方針)

**原則: 各コンポーネントの実体はただ1つの場所にのみ置く。** 2026-08 に号機・kkrtx・
操作画面・relay を本リポジトリへ統合したので、以下だけが外部に残ります。

| コンポーネント | 正式な置き場所 (single source of truth) |
|---|---|
| master_control / camera_publisher / mic_publisher / mic_hub / control_ui / webrtc_relay | **このリポジトリ** |
| joy_node_web | [KinkiKnights/joy_node_web](https://github.com/KinkiKnights/joy_node_web)(他ロボットでも使う共有パッケージ。**submodule** として固定コミットで参照) |
| ros2_socketcan | [autowarefoundation/ros2_socketcan](https://github.com/autowarefoundation/ros2_socketcan)(上流 OSS。**submodule** として固定コミットで参照。KinkiKnights の管理下でないため URL は `https://`) |
| gm6020_control | [KinkiKnights/gm6020_control](https://github.com/KinkiKnights/gm6020_control)(GM6020 サーボ制御。**submodule** として固定コミットで参照。**private** のため URL は `git@`) |
| damiyan-signal-processing | 別リポジトリ(音声解析。契約は [protocols/damiyan.md](protocols/damiyan.md) に明文化) |

### submodule の運用

`robot/ros2/` 配下の 3 件が submodule です。

| パス | URL | 公開 |
|---|---|---|
| `robot/ros2/joy_node_web` | `https://github.com/KinkiKnights/joy_node_web.git` | public |
| `robot/ros2/ros2_socketcan` | `https://github.com/autowarefoundation/ros2_socketcan.git` | public(外部 OSS) |
| `robot/ros2/gm6020_control` | `git@github.com:KinkiKnights/gm6020_control.git` | **private**(SSH キー必須) |

固定しているコミットは `git ls-files -s robot/ros2` で確認できます。
`ros2_socketcan` だけ `https://` なのは、KinkiKnights の管理下にない外部 public
リポジトリなので、SSH キーが無い環境でも `--recursive` clone できるようにするためです。

```bash
# clone 時に --recursive を付け忘れた場合(3 件まとめて取得)
git submodule update --init --recursive

# 上流の最新を取り込み、親リポジトリのポインタを更新する(1 件ずつ意識的に)
git submodule update --remote robot/ros2/ros2_socketcan
cd ~/kk_ws && colcon build --base-paths src --symlink-install   # 先に通ることを確認
cd ~/kk_ws/src/RescuePiSystem
git add robot/ros2/ros2_socketcan && git commit -m "ros2_socketcan submodule を更新"
```

上流 OSS(`ros2_socketcan`)は**意図的に固定コミットへ留め置きます**。号機で動作確認
済みの版を全機で揃えるためで、`main` の最新へ自動追従はしません。

#### joy_node_web の運用

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

外部依存も submodule なので、更新は上の「submodule の運用」と同じ手順です。
以前は `deploy/robot/rescue_pi_system.repos`(vcstool)で `~/kk_ws/src` へ別途
clone していましたが、**同名パッケージが `~/kk_ws/src` に二重に現れて
`colcon build` が壊れる**ため廃止しました。`~/kk_ws/src` 直下に
`ros2_socketcan/` や `gm6020_control/` が残っている号機では、それらを退避してから
ビルドし直してください(`colcon list --base-paths src` で重複を確認できます)。

## 運用メモ

- master control は systemd (`master-control.service`) で自動起動。ユニットファイルは
  `deploy/robot/app_setup.sh` だけが生成します(リポジトリ内に .service ファイルの複製を置かない)。
- `robot/master_control/programs.json` はセットアップスクリプトが Pi ごとに生成する運用ファイルです。
  リポジトリには KK05 の実例をコミットしてあります。
  tracked なので `git pull` で巻き戻ります。`MASTER_CONTROL_PROGRAMS` に
  リポジトリ外のパス（例 `~/.config/rescue-pi/programs.json`）を渡せば、
  master_control はそちらを読み書きします（→ `docs/operations.md`）。
- サービス操作: `sudo systemctl restart master-control.service` / `journalctl -u master-control -f`

## CAN (MCP2515 SPI HAT) 接続

モータ/サーボ等を CAN で制御する号機向けに、MCP2515 CAN HAT の SocketCAN と
[ros2_socketcan](https://github.com/autowarefoundation/ros2_socketcan) ブリッジを構成します。
**この設定は既定で無効**で、**MCP2515 HAT を物理的に装着した号機でのみ**有効化します
(HAT が無いと `can0` が出ず、`can0-setup.service` が待機後に失敗します)。

- **ROS パッケージ** `robot/ros2/kk_can_bringup`(colcon 対象)が ros2_socketcan の
  `socket_can_bridge.launch.xml`(`enable_can_fd=false`)を include します。
  `ros2_socketcan` 本体は submodule `robot/ros2/ros2_socketcan`(固定コミット)として
  同梱され、同じ colcon ビルドで作られます。
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

## GM6020 モータ制御 (gm6020_control)

> **⚠ 実アクチュエータを駆動します。** `gm6020_control` のノードを起動して `~/enable` に
> `true` を publish すると、**その瞬間から実機の GM6020 が回ります**。机上での
> 「とりあえず起動して確認」をしてよいパッケージではありません。周囲の安全を確保し、
> 可動範囲に人・ケーブル・工具が無いことを確認してから通電してください。

`robot/ros2/gm6020_control` は submodule([KinkiKnights/gm6020_control](https://github.com/KinkiKnights/gm6020_control)、
private)で、DJI GM6020 のクローズドループ・サーボ(位置)制御と開ループ電圧制御を行います。

### CAN 経路が kk_can_bringup と違う

`kk_can_bringup` / `ros2_socketcan` の `/to_can_bus`・`/from_can_bus` は**使いません**。
`gm6020_control` は Python 標準ライブラリの raw SocketCAN で `can0` を直接読み書きします。
ros2_socketcan ブリッジでは GM6020 の約 1 kHz フィードバックを取りこぼし、サーボループが
成立しないためです。したがって `kk-can-ros.service`(ブリッジ)とは**同じ `can0` を
共有する独立した利用者**になります。

### 安全柵と起動方法

- **`config/gm6020_dual.yaml` の `start_enabled: false` が安全柵**です。起動直後は出力 0 で、
  `~/enable` に `true` を publish して初めて駆動します。**この既定値を `true` に変えないこと。**
  初回フィードバック受信時に現在角度を目標として保持するため、enable した瞬間に
  飛び出さない設計にはなっていますが、それは安全柵の代わりにはなりません。
- **使うのは `gm6020_dual.launch.py`** です。

  ```bash
  ros2 launch gm6020_control gm6020_dual.launch.py     # config/gm6020_dual.yaml を読む
  ```

- **`gm6020.launch.py`(単体ノード)はそのままでは起動できません。** 既定の `params_file` が
  `config/gm6020.yaml` を指していますが、このファイルは失われており現存しません。
  使う場合は `params_file:=` を必ず明示してください。
- 同一 control id(`0x1FF` / `0x2FF`)を共有するモータは 1 フレームにまとめて送る必要が
  あるため、**単体ノードと dual ノードを同時に走らせないこと**(互いの指令スロットを 0 で
  上書きし合い、指令が破綻します)。
- **PID ゲインは実機で未調整**です。現在の値は仕様/復元バイトコードの既定値で、実際に
  運用されていたゲインは失われています。通電前に
  [`robot/ros2/gm6020_control/README.md`](../robot/ros2/gm6020_control/README.md)
  (プロトコル・トピック・パラメータの詳細)を必ず読んでください。
- MCP2515(SPI CAN)の TX は約 500 Hz が上限です。`CAN send dropped` の warn が続く場合は
  `send_rate_hz` を下げます。

### kk03 の実構成

GM6020 ×2 を **ID3 / ID4** で使用し、両方が control id `0x1FF` に載ります
(フィードバックは `0x207` / `0x208`)。`config/gm6020_dual.yaml` がこの構成です。
実機の DIP スイッチ設定と `motor_ids` が一致しているかは通電前に確認してください。
