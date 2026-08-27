#!/usr/bin/env bash
# =============================================================================
#  can_setup.sh  —  MCP2515 (SPI HAT) SocketCAN + ros2_socketcan の bring-up
# -----------------------------------------------------------------------------
#  MCP2515 CAN HAT を装着した Pi 向けの「システム側」bring-up を行います:
#    1. SPI/GPIO グループ追加 と can-utils 導入
#    2. Device Tree overlay (dtparam=spi=on / dtoverlay=mcp2515-can0 …) を
#       /boot/firmware/config.txt に冪等に追記 (反映には再起動が必要)
#    3. /etc/default/kk-can (CAN パラメータ / ROS_DOMAIN_ID) を生成
#    4. /usr/local/sbin/can0-up.sh (SocketCAN link up) を生成
#    5. /usr/local/sbin/kk-can-ros-launch.sh (ros2_socketcan bridge 起動) を生成
#    6. systemd ユニット can0-setup.service / kk-can-ros.service を生成・有効化
#
#  ※ ROS パッケージ (kk_can_bringup / ros2_socketcan) の取得・ビルドは
#    app_setup.sh の vcs import + colcon build が担当します。本スクリプトは
#    システム側(overlay / systemd / 起動スクリプト)のみを担当します。したがって
#    app_setup.sh の colcon build 後、または新規セットアップ完了後に実行してください。
#
#  通常は SETUP_CAN=1 のとき app_setup.sh から呼ばれます。単体でも実行できます:
#    SETUP_CAN=1 ./setup/can_setup.sh
#    CAN_BITRATE=250000 ROS_DOMAIN_ID=5 ./setup/can_setup.sh
#
#  ※ MCP2515 HAT が物理的に装着されていないと can0 は現れず、can0-setup.service は
#    待機後に失敗します(起動全体は継続)。HAT 装着機でのみ実行してください。
#  ※ 何度実行しても安全(冪等)です。
# =============================================================================
set -euo pipefail

# ---- 環境変数(kk_robot_setup.sh から export。単体実行時は既定値)-----------
: "${ROS_DISTRO:=jazzy}"
: "${WS:=$HOME/kk_ws}"
: "${USER_NAME:=$(id -un)}"
: "${CAN_INTERFACE:=can0}"
: "${CAN_BITRATE:=1000000}"          # CAN ビットレート (bps)
: "${CAN_OSCILLATOR_HZ:=16000000}"   # MCP2515 水晶振動子 (Hz)
: "${CAN_INTERRUPT_GPIO:=24}"        # MCP2515 INT ピン (BCM)
: "${DT_OVERLAY:=mcp2515-can0}"
# ROS 2 ドメイン。号機内の他 ROS ノード(joy_node_web 等)と揃える必要があります。
# 既定 0(kk_rescue26_pi の他ノードの既定と一致)。号機で分離する場合のみ変更。
: "${ROS_DOMAIN_ID:=0}"

log() { printf '\033[1;36m[can-setup]\033[0m %s\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # サービス再起動の確認ダイアログを抑制

log "CAN bring-up 開始: iface=${CAN_INTERFACE} bitrate=${CAN_BITRATE} osc=${CAN_OSCILLATOR_HZ} int=${CAN_INTERRUPT_GPIO} domain=${ROS_DOMAIN_ID}"

# =============================================================================
# 1. SPI/GPIO アクセス権 と can-utils
# =============================================================================
log "1. SPI/GPIO グループ追加 と can-utils 導入"
sudo usermod -aG dialout,spi,gpio "${USER_NAME}" 2>/dev/null \
  || sudo usermod -aG dialout "${USER_NAME}" 2>/dev/null || true
sudo apt-get install -y can-utils 2>/dev/null || log "   (can-utils はスキップ)"

# =============================================================================
# 2. Device Tree overlay (/boot/firmware/config.txt)
#    overlay 行の有無で冪等判定(別マーカーで導入済みでも二重追記しない)。
#    dtparam=spi=on は overlay とは独立に、常に 1 つだけ存在させる。
# =============================================================================
log "2. Device Tree overlay を config.txt に追記"
CONFIG_TXT=""
if [ -f /boot/firmware/config.txt ]; then CONFIG_TXT=/boot/firmware/config.txt
elif [ -f /boot/config.txt ]; then CONFIG_TXT=/boot/config.txt; fi
OVERLAY_LINE="dtoverlay=${DT_OVERLAY},oscillator=${CAN_OSCILLATOR_HZ},interrupt=${CAN_INTERRUPT_GPIO}"
MARKER="# kk-can-mcp2515"
if [ -z "${CONFIG_TXT}" ]; then
  log "   -> config.txt が見つからないためスキップ(overlay は手動設定が必要)"
else
  if grep -qF "${OVERLAY_LINE}" "${CONFIG_TXT}"; then
    log "   -> overlay は既に設定済み (${CONFIG_TXT})"
  elif grep -qE '^[[:space:]]*dtoverlay=mcp2515' "${CONFIG_TXT}"; then
    log "   -> 警告: 別設定の mcp2515 overlay が存在します。手動確認: ${OVERLAY_LINE}"
  else
    { echo ""; echo "${MARKER}"; echo "${OVERLAY_LINE}"; } | sudo tee -a "${CONFIG_TXT}" >/dev/null
    log "   -> overlay を追記 (反映には再起動が必要)"
  fi
  if ! grep -qE '^[[:space:]]*dtparam=spi=on' "${CONFIG_TXT}"; then
    echo "dtparam=spi=on" | sudo tee -a "${CONFIG_TXT}" >/dev/null
    log "   -> dtparam=spi=on を追記"
  fi
fi

# =============================================================================
# 3. /etc/default/kk-can
# =============================================================================
log "3. /etc/default/kk-can を生成"
sudo tee /etc/default/kk-can >/dev/null <<EOF
# kk-can 設定(can_setup.sh が生成)。号機ごとに変更する場合はここを編集し
# 'sudo systemctl restart can0-setup kk-can-ros' で反映。
CAN_INTERFACE=${CAN_INTERFACE}
CAN_BITRATE=${CAN_BITRATE}
CAN_OSCILLATOR_HZ=${CAN_OSCILLATOR_HZ}
CAN_INTERRUPT_GPIO=${CAN_INTERRUPT_GPIO}
DT_OVERLAY=${DT_OVERLAY}
ROS_DISTRO=${ROS_DISTRO}
KK_WS=${WS}
# ROS 2 ドメイン。号機内の他ノード(joy_node_web 等)と揃える(既定 0)。
ROS_DOMAIN_ID=${ROS_DOMAIN_ID}
EOF

# =============================================================================
# 4. /usr/local/sbin/can0-up.sh — SocketCAN link を up にする(can0 出現を待機)
# =============================================================================
log "4. /usr/local/sbin/can0-up.sh を生成"
sudo tee /usr/local/sbin/can0-up.sh >/dev/null <<'EOF'
#!/bin/bash
# SocketCAN インターフェースを up にする(カーネルドライバ / can0 netdev を待機)。
set -euo pipefail
# shellcheck disable=SC1091
[ -f /etc/default/kk-can ] && source /etc/default/kk-can
INTERFACE="${CAN_INTERFACE:-can0}"
BITRATE="${CAN_BITRATE:-1000000}"
MAX_WAIT_SEC="${CAN_MAX_WAIT_SEC:-60}"

for ((i = 1; i <= MAX_WAIT_SEC; i++)); do
  if ip link show "${INTERFACE}" &>/dev/null; then
    if ip link show "${INTERFACE}" | grep -q "UP"; then
      exit 0
    fi
    /sbin/ip link set "${INTERFACE}" down 2>/dev/null || true
    /sbin/ip link set "${INTERFACE}" up type can bitrate "${BITRATE}"
    echo "CAN interface ${INTERFACE} is UP (bitrate=${BITRATE})"
    exit 0
  fi
  sleep 1
done

echo "ERROR: ${INTERFACE} not found after ${MAX_WAIT_SEC}s (Device Tree overlay / 配線を確認)" >&2
exit 1
EOF
sudo chmod 0755 /usr/local/sbin/can0-up.sh

# =============================================================================
# 5. /usr/local/sbin/kk-can-ros-launch.sh — ros2_socketcan bridge を起動
#    ROS の setup.bash は未定義変数を参照するため nounset(-u)は使わない。
# =============================================================================
log "5. /usr/local/sbin/kk-can-ros-launch.sh を生成"
sudo tee /usr/local/sbin/kk-can-ros-launch.sh >/dev/null <<'EOF'
#!/bin/bash
set -eo pipefail
# shellcheck disable=SC1091
[ -f /etc/default/kk-can ] && source /etc/default/kk-can
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
CAN_INTERFACE="${CAN_INTERFACE:-can0}"
WORKSPACE="${KK_WS:-/home/kk/kk_ws}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
# shellcheck source=/dev/null
source "/opt/ros/${ROS_DISTRO}/setup.bash"
# shellcheck source=/dev/null
source "${WORKSPACE}/install/setup.bash"
exec ros2 launch kk_can_bringup can_bridge.launch.xml "interface:=${CAN_INTERFACE}"
EOF
sudo chmod 0755 /usr/local/sbin/kk-can-ros-launch.sh

# =============================================================================
# 6. systemd ユニット(can0 の link up → ros2_socketcan bridge)
#    README 方針に従い .service ファイルはリポジトリに置かず本スクリプトが生成。
# =============================================================================
log "6. systemd ユニットを生成・有効化"
sudo tee /etc/systemd/system/can0-setup.service >/dev/null <<'EOF'
[Unit]
Description=Bring up MCP2515 SocketCAN interface
After=local-fs.target
Before=kk-can-ros.service
DefaultDependencies=yes

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-/etc/default/kk-can
ExecStart=/usr/local/sbin/can0-up.sh
ExecStop=/sbin/ip link set can0 down
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/kk-can-ros.service >/dev/null <<EOF
[Unit]
Description=ROS 2 SocketCAN bridge (ros2_socketcan)
After=network-online.target can0-setup.service
Wants=network-online.target
Requires=can0-setup.service

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
Environment=HOME=/home/${USER_NAME}
EnvironmentFile=-/etc/default/kk-can
WorkingDirectory=${WS}
ExecStart=/usr/local/sbin/kk-can-ros-launch.sh
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable can0-setup.service kk-can-ros.service
sudo modprobe mcp251x 2>/dev/null || true
sudo systemctl start can0-setup.service 2>/dev/null || true
sudo systemctl start kk-can-ros.service 2>/dev/null || true

# =============================================================================
# 7. 案内
# =============================================================================
log "=== CAN bring-up 設定が完了しました ==="
echo "  - 設定ファイル:  /etc/default/kk-can (CAN_BITRATE=${CAN_BITRATE}, ROS_DOMAIN_ID=${ROS_DOMAIN_ID})"
echo "  - サービス:      can0-setup.service, kk-can-ros.service (自動起動有効)"
echo "  - 確認:          ip link show ${CAN_INTERFACE} / systemctl status can0-setup kk-can-ros"
echo "  - ROS トピック:  ros2 topic list   (期待: /from_can_bus /to_can_bus)"
if ! ip link show "${CAN_INTERFACE}" >/dev/null 2>&1; then
  echo "  - can0 未検出: Device Tree overlay 反映のため一度再起動してください: sudo reboot"
  echo "    (MCP2515 HAT が物理的に装着されていることも確認してください)"
fi
