#!/usr/bin/env bash
# =============================================================================
#  app_setup.sh  —  RescuePiSystem の各種環境構築
# -----------------------------------------------------------------------------
#  基本設定と ROS の導入 (env_setup.sh) が済んでいる前提で、本リポジトリの
#  プログラムを動かすための環境を構築します:
#    1. 各コンポーネントの依存パッケージ導入
#         master_control / joy_node_web / camera_publisher
#    2. ROS 2 ワークスペース kk_ws の作成とリポジトリのクローン
#         RescuePiSystem (+ submodule joy_node_web) と .repos の外部依存 (ros2_socketcan)
#    3. rosdep 依存解決 と colcon ビルド
#    4. master control の programs.json 生成 と 自動起動 (systemd) 設定
#    5. 簡易セルフチェック
#
#  Pi 上で動くプログラムは RescuePiSystem リポジトリに集約されています:
#    - master_control/     : Web UI つきプログラム起動管理サーバ (port 80)
#    - camera_publisher/   : USB カメラ → WebRTC 配信 (relay へ)
#    - mic_publisher/      : USB マイク → FLAC ロスレス TCP 配信
#    - ros2/joy_node_web/  : Web ゲームパッド → sensor_msgs/Joy (submodule, colcon 対象)
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
# カメラ入力ソース (camChange の番号1)。
#   USBカメラ(MJPEG出力)の例: "v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec"
#   CSIカメラの場合は         : "libcamerasrc"
: "${CAM1_SRC:=v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec}"
# マイク配信 (mic_publisher / FLAC ロスレス TCP)
: "${MIC_ALSA_DEV:=hw:1,0}"   # USBマイク (arecord -l で確認)
: "${MIC_RATE:=48000}"        # 48000 または 44100 (マイクのネイティブ)
: "${MIC_PORT:=5005}"         # 配信TCPポート
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

# =============================================================================
# 2. ROS2 ワークスペース kk_ws の作成とリポジトリのクローン
#    Pi 側プログラムは RescuePiSystem に集約。joy_node_web は submodule
#    (ros2/joy_node_web) として固定コミットで含む → submodule init が必要。
#    外部 OSS (ros2_socketcan) のみ deploy/robot/rescue_pi_system.repos に定義し
#    vcstool で取得します。
# =============================================================================
log "2. ワークスペース ${WS} を作成しリポジトリをクローン"
mkdir -p "${WS}/src"
cd "${WS}/src"

# --recursive で submodule (joy_node_web) も同時に取得。既存 clone の場合に
# 備え submodule update も明示実行(未取得なら空ディレクトリ→ビルド失敗を防ぐ)。
# clone は SSH (SSH キー) を優先し、失敗時のみ HTTPS (公開時のみ有効)。
[ -d "${REPO_DIR}" ] \
  || GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone --recursive "${REPO_SSH}" "${REPO_DIR}" \
  || git clone --recursive "${REPO_URL}" "${REPO_DIR}"
git -C "${REPO_DIR}" submodule update --init --recursive
vcs import "${WS}/src" < "${REPO_DIR}/deploy/robot/rescue_pi_system.repos"
chmod +x "${REPO_DIR}/robot/camera_publisher/"*.sh "${REPO_DIR}/robot/mic_publisher/"*.sh

# =============================================================================
# 3. rosdep 依存解決 と colcon ビルド
#    colcon は package.xml を持つパッケージのみビルド:
#      RescuePiSystem/robot/ros2/joy_node_web / RescuePiSystem/robot/ros2/kk_can_bringup
#      / ros2_socketcan / ros2_socketcan_msgs
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
#    ROS パッケージ (kk_can_bringup / ros2_socketcan) は上の colcon build で
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
log "4-1. programs.json にカメラ / joy_node_web / mic を登録"
cat > "${REPO_DIR}/robot/master_control/programs.json" <<JSON
[
  {"id": 1, "name": "camera",       "type": "bash", "cmd": "PI_ID=${PI_ID} SERVER=${RELAY_URL} CAM1=\"${CAM1_SRC}\" ${REPO_DIR}/robot/camera_publisher/publish-${PI_MODEL}.sh"},
  {"id": 2, "name": "joy_node_web", "type": "ros2", "cmd": "source ${WS}/install/setup.bash && ros2 run joy_node_web joy_node"},
  {"id": 3, "name": "mic",          "type": "bash", "cmd": "ALSA_DEV=${MIC_ALSA_DEV} RATE=${MIC_RATE} PORT=${MIC_PORT} ${REPO_DIR}/robot/mic_publisher/mic-publish.sh"}
]
JSON

log "4-2. master-control.service を作成(kk ユーザで port 80 を bind)"
sudo tee /etc/systemd/system/master-control.service >/dev/null <<UNIT
[Unit]
Description=RescuePiSystem Master Control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=HOME=${HOME}
WorkingDirectory=${REPO_DIR}/robot/master_control
ExecStart=/usr/bin/python3 ${REPO_DIR}/robot/master_control/master_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now master-control.service

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
ls "${REPO_DIR}/robot/mic_publisher/mic-publish.sh" >/dev/null 2>&1 && echo "   [OK] mic publisher 配置済み" || echo "   [NG] mic publisher なし"

log "=== RescuePiSystem の環境構築が完了しました ==="
echo "  - master control:  http://<このPiのIP>/        (port 80, 自動起動済み)"
echo "  - joy_node_web:    http://<このPiのIP>:8700/joy (master control から起動)"
echo "  - camera:          PI_ID=${PI_ID}  RELAY=${RELAY_URL}"
echo "  - mic:             FLACロスレス配信 tcp://<このPiのIP>:${MIC_PORT} (dev=${MIC_ALSA_DEV})"
echo "  - カメラ/joy/micは master control の Web UI から起動します(自動起動はしません)。"
echo "  - 反映には再ログイン、または 'source ~/.bashrc' を実行してください。"
