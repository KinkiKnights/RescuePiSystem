#!/usr/bin/env bash
# =============================================================================
#  app_setup.sh  —  RescuePiSystem の各種環境構築
# -----------------------------------------------------------------------------
#  基本設定と ROS の導入 (env_setup.sh) が済んでいる前提で、本リポジトリの
#  プログラムを動かすための環境を構築します:
#    1. 各コンポーネントの依存パッケージ導入
#         master_control / joy_node_web / camera_publisher
#    2. ROS 2 ワークスペース kk_ws の作成とリポジトリのクローン
#         RescuePiSystem と submodule (joy_node_web / ros2_socketcan / gm6020_control)
#    3. rosdep 依存解決 と colcon ビルド
#    4. master control の programs.json 生成 と 自動起動 (systemd) 設定
#         ユニットは deploy/systemd/*.service.in を展開して設置する
#    5. 簡易セルフチェック
#
#  Pi 上で動くプログラムは RescuePiSystem リポジトリに集約されています:
#    - master_control/     : Web UI つきプログラム起動管理サーバ (port 80)
#    - camera_publisher/   : USB カメラ → WebRTC 配信 (relay へ)
#         カメラ/マイクのデバイス指定は master_control の「デバイス設定」
#         (~/.config/rescue-pi/devices.json) が持つ
#    - mic_publisher/      : USB マイク → 16kHz PCM を集約ハブへ HTTP push
#    - ros2/joy_node_web/  : Web ゲームパッド → sensor_msgs/Joy (submodule, colcon 対象)
#    - ros2/ros2_socketcan/: SocketCAN <-> ROS 2 ブリッジ (submodule/上流 OSS, colcon 対象)
#    - ros2/gm6020_control/: GM6020 サーボ制御 (submodule, colcon 対象)
#         ▲ 実モータを駆動する。docs/robot.md の「GM6020 モータ制御」を読むこと
#
#  通常は kk_robot_setup.sh から呼び出されます(環境変数を引き継ぎます)。
#  単体でも実行できます(未設定の環境変数は既定値を使用):
#    ./deploy/robot/app_setup.sh
# =============================================================================
set -euo pipefail

# ---- 環境変数(kk_robot_setup.sh から export。単体実行時は既定値)-----------
: "${ROS_DISTRO:=jazzy}"
: "${WS:=$HOME/kk_ws}"
: "${REPO_SSH:=git@github.com:KinkiKnights/RescuePiSystem.git}"       # 優先 (SSH キーで認証)
: "${REPO_URL:=https://github.com/KinkiKnights/RescuePiSystem.git}"   # 公開時のフォールバック
: "${REPO_DIR:=${WS}/src/RescuePiSystem}"
: "${PI_MODEL:=pi5}"                                       # publish-${PI_MODEL}.sh を使用 (pi4=HW / pi5=SW)
: "${RELAY_HOST:=192.168.137.1}"                           # webrtc 中継(SFU)サーバのIP
: "${RELAY_URL:=ws://${RELAY_HOST}:8080/ws}"
: "${PI_ID:=$(hostname | tr '[:lower:]' '[:upper:]')}"     # 配信ID(ホスト名から自動生成)
# カメラ/マイクのデバイスは devices.json (master_control の「デバイス設定」)が持つ。
#   ここでは初期値を自動検出するだけで、programs.json には埋め込まない。
#   手動で固定したい場合は setup 後に UI から選ぶか devices.json を編集する。
# マイク配信 (mic_publisher: 16kHz/mono/S16LE を集約ハブへ HTTP push)
: "${HUB_HOST:=192.168.10.3}"                  # 集約ハブ(kkrtx)のIP
: "${MIC_HUB:=http://${HUB_HOST}:8770}"
: "${MIC_UNIT:=$(hostname | grep -oE '[0-9]+$' | sed 's/^0*//' || true)}"   # kk05 -> 5
: "${MIC_UNIT:=$(hostname)}"
: "${USER_NAME:=$(id -un)}"
# CAN (MCP2515 SPI HAT) bring-up。HAT を装着した号機のみ 1(既定は無効)。
: "${SETUP_CAN:=0}"
# ROS 2 ドメイン ID。号機内の全 ROS ノードで揃える(既定 0)。CAN ブリッジもこれを使う。
: "${ROS_DOMAIN_ID:=0}"

log() { printf '\033[1;36m[app-setup]\033[0m %s\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # サービス再起動の確認ダイアログを抑制

# =============================================================================
# 1. 各コンポーネントの依存パッケージ
# =============================================================================
log "1-1. master_control の依存 (psutil)"
sudo apt-get install -y python3-psutil

log "1-2. joy_node_web の依存 (FastAPI / uvicorn / websockets)"
sudo apt-get install -y python3-fastapi python3-uvicorn python3-websockets

log "1-3. camera_publisher の依存 (GStreamer / Python GI / libcamera)"
sudo apt-get install -y \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-libav \
  python3-gi python3-gi-cairo gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
  python3-websockets v4l-utils
# CSIカメラ等の任意パッケージ(無い環境では無視)
sudo apt-get install -y gstreamer1.0-libcamera libcamera-tools gstreamer1.0-plugins-ugly 2>/dev/null \
  || log "   (任意パッケージはスキップ)"

log "1-4. mic_publisher の依存 (arecord / numpy)"
#   GStreamer も FLAC も使わない。arecord で取り込み、numpy の FIR で 16kHz へ
#   落として集約ハブへ push する (詳細は docs/protocols/mic.md)。
sudo apt-get install -y alsa-utils python3-numpy

# =============================================================================
# 2. ROS2 ワークスペース kk_ws の作成とリポジトリのクローン
#    Pi 側プログラムは RescuePiSystem に集約。共有 ROS 2 パッケージは robot/ros2/ 配下の
#    submodule (joy_node_web / ros2_socketcan / gm6020_control) として固定コミットで
#    含む → submodule init が必要。${WS}/src へ外部リポジトリを別途 clone しない
#    (同名パッケージが二重になり colcon build が壊れる)。
# =============================================================================
log "2. ワークスペース ${WS} を作成しリポジトリをクローン"
mkdir -p "${WS}/src"
cd "${WS}/src"

# --recursive で submodule (joy_node_web / ros2_socketcan / gm6020_control) も同時に
# 取得。gm6020_control は private リポジトリなので SSH キーの登録が必要。既存 clone の場合に
# 備え submodule update も明示実行(未取得なら空ディレクトリ→ビルド失敗を防ぐ)。
# clone は SSH (SSH キー) を優先し、失敗時のみ HTTPS (公開時のみ有効)。
[ -d "${REPO_DIR}" ] \
  || GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone --recursive "${REPO_SSH}" "${REPO_DIR}" \
  || git clone --recursive "${REPO_URL}" "${REPO_DIR}"
git -C "${REPO_DIR}" submodule update --init --recursive
chmod +x "${REPO_DIR}/robot/camera_publisher/"*.sh

# =============================================================================
# 3. rosdep 依存解決 と colcon ビルド
#    colcon は package.xml を持つパッケージのみビルド (すべて RescuePiSystem/robot/ros2/):
#      joy_node_web / kk_can_bringup / ros2_socketcan / ros2_socketcan_msgs / gm6020_control
#    (master_control / camera_publisher / mic_publisher は ROS パッケージではない)
#    rosdep が ros2_socketcan の依存 (ros-jazzy-can-msgs 等) を自動導入します。
#    ※ rosdep の初期化・更新 (rosdep init / update) は env_setup.sh で実施済み。
# =============================================================================
log "3. rosdep 解決と colcon ビルド"
# ROS の setup.bash は AMENT_TRACE_SETUP_FILES 等の未定義変数を参照するため、
# nounset (set -u) 下ではそのまま source すると失敗する。source の間だけ無効化する。
set +u; source "/opt/ros/${ROS_DISTRO}/setup.bash"; set -u
cd "${WS}"
rosdep install --from-paths src --ignore-src -r -y || log "   (rosdep 一部スキップ)"
colcon build --symlink-install

# =============================================================================
# 3.5 CAN (MCP2515 HAT) のシステム側 bring-up — 既定で無効(HAT 装着機のみ)
#    ROS パッケージ (kk_can_bringup / ros2_socketcan、いずれも robot/ros2/) は上の colcon build で
#    ビルド済み。ここでは overlay / /etc/default/kk-can / systemd 等のシステム側を
#    can_setup.sh で構成する。HAT 未装着機で有効化するとサービスが失敗するため既定無効。
#    有効化: SETUP_CAN=1 を付けて実行。
# =============================================================================
CAN_SETUP="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/can_setup.sh"
if [ "${SETUP_CAN}" = "1" ]; then
  log "3.5. CAN bring-up (SETUP_CAN=1)"
  bash "${CAN_SETUP}"
else
  log "3.5. CAN bring-up は既定で無効 (MCP2515 HAT 装着機は SETUP_CAN=1 を付けて実行)"
fi

# =============================================================================
# 4. master control: カメラ/joy_node_web/mic を登録 + 自動起動(systemd)
#    systemd ユニットはこのスクリプトだけが生成します(重複定義を持たない)。
# =============================================================================
# カメラ/マイクのデバイス指定は cmd に埋め込まない。master_control が
# devices.json (robot/device_config.py) から解決して環境変数で渡す。
# 号機ごとの運用設定なのでリポジトリ外 (~/.config/rescue-pi/devices.json) に置く。
log "4-0. カメラ / マイクのデバイス設定を初期化 (未作成なら自動検出)"
python3 "${REPO_DIR}/robot/device_config.py" --init || log "   (デバイス設定の初期化に失敗。UI から設定してください)"
python3 "${REPO_DIR}/robot/device_config.py" || true

log "4-1. programs.json にカメラ / joy_node_web / mic を登録"
cat > "${REPO_DIR}/robot/master_control/programs.json" <<JSON
[
  {"id": 1, "name": "camera",       "type": "bash", "cmd": "PI_ID=${PI_ID} SERVER=${RELAY_URL} ${REPO_DIR}/robot/camera_publisher/publish-${PI_MODEL}.sh"},
  {"id": 2, "name": "joy_node_web", "type": "ros2", "cmd": "source ${WS}/install/setup.bash && ros2 run joy_node_web joy_node"},
  {"id": 3, "name": "mic",          "type": "bash", "cmd": "/usr/bin/python3 ${REPO_DIR}/robot/mic_publisher/mic_publisher.py --hub ${MIC_HUB} --unit ${MIC_UNIT}"}
]
JSON

log "4-2. master-control.service を設置(kk ユーザで port 80 を bind)"
#   ユニットの内容は deploy/systemd/master-control.service.in が単一の真実。
#   ここではテンプレートを展開して設置するだけ(内容の重複を持たない)。
SYSTEMD_DIR="${REPO_DIR}/deploy/systemd"
source "${SYSTEMD_DIR}/install_unit.sh"
install_unit master-control
sudo systemctl daemon-reload
sudo systemctl enable --now master-control.service

# --- 4-3. mic publisher を常駐させる場合のみ (既定は master_control から起動) ---
#   ハブは 1 号機 1 publisher しか受け付けないため、常駐と Web UI 起動を
#   同時に使わないこと(二重起動は 409)。
if [ "${MIC_SERVICE:-0}" = "1" ]; then
  log "4-3. mic-publisher.service を設置 (MIC_SERVICE=1)"
  if [ -f /etc/default/mic-publisher ]; then
    log "   /etc/default/mic-publisher は既存を尊重します(上書きなし)"
  else
    #   雛形は deploy/systemd/mic-publisher.default。ここでは号機の実値で生成する。
    sudo tee /etc/default/mic-publisher >/dev/null <<DEF
# /etc/default/mic-publisher — 号機ごとの設定 (app_setup.sh が初回生成)
MIC_HUB=${MIC_HUB}
MIC_UNIT=${MIC_UNIT}
# 録音デバイスと取り込みレートは master_control の「デバイス設定」
# (~/.config/rescue-pi/devices.json) が持つ。ここで MIC_DEVICE /
# MIC_CAPTURE_RATE を設定すると UI の設定より優先されるので、
# 意図的に固定したいときだけコメントを外す。
#MIC_DEVICE=hw:CARD=Device,DEV=0
#MIC_CAPTURE_RATE=48000
DEF
  fi
  install_unit mic-publisher
  sudo systemctl daemon-reload
  sudo systemctl enable --now mic-publisher.service
  log "   /etc/default/mic-publisher の MIC_UNIT / MIC_DEVICE を確認してください"
else
  log "4-3. mic は master_control の Web UI から起動します (常駐させるなら MIC_SERVICE=1)"
fi

# =============================================================================
# 5. 簡易セルフチェック (失敗してもスクリプトは止めない)
# =============================================================================
log "5. セルフチェック"
sleep 2
systemctl is-active --quiet master-control.service && echo "   [OK] master-control 稼働中" || echo "   [NG] master-control 停止"
curl -s -o /dev/null --max-time 5 -w "   [HTTP %{http_code}] master control UI\n" "http://127.0.0.1:80/" || echo "   [NG] UI 応答なし"
set +u; source "/opt/ros/${ROS_DISTRO}/setup.bash" 2>/dev/null || true; source "${WS}/install/setup.bash" 2>/dev/null || true; set -u
ros2 pkg executables joy_node_web 2>/dev/null | grep -q joy_node && echo "   [OK] joy_node_web ビルド済み" || echo "   [NG] joy_node_web 未ビルド"
ls "${REPO_DIR}/robot/camera_publisher/publish-${PI_MODEL}.sh" >/dev/null 2>&1 && echo "   [OK] camera publisher 配置済み" || echo "   [NG] camera publisher なし"
ls "${REPO_DIR}/robot/mic_publisher/mic_publisher.py" >/dev/null 2>&1 && echo "   [OK] mic publisher 配置済み" || echo "   [NG] mic publisher なし"
python3 "${REPO_DIR}/robot/device_config.py" >/dev/null 2>&1 && echo "   [OK] デバイス設定を解決できる" || echo "   [NG] デバイス設定の解決に失敗"

log "=== RescuePiSystem の環境構築が完了しました ==="
echo "  - master control:  http://<このPiのIP>/        (port 80, 自動起動済み)"
echo "  - joy_node_web:    http://<このPiのIP>:8700/joy (master control から起動)"
echo "  - camera:          PI_ID=${PI_ID}  RELAY=${RELAY_URL}"
echo "  - mic:             ${MIC_HUB}/ingest/${MIC_UNIT} へ push"
echo "                     購読は ${MIC_HUB}/${MIC_UNIT}"
echo "  - カメラ/joy/micは master control の Web UI から起動します(自動起動はしません)。"
echo "  - 反映には再ログイン、または 'source ~/.bashrc' を実行してください。"
