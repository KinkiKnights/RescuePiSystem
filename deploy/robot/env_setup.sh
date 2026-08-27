#!/usr/bin/env bash
# =============================================================================
#  env_setup.sh  —  基本的な設定と ROS 2 の導入  (Ubuntu 24.04 / ROS 2 Jazzy)
# -----------------------------------------------------------------------------
#  kk_rescue26_pi リポジトリに依存しない「土台」を構築します:
#    1. パスワード無し sudo の設定
#    2. USB 最大電流の有効化 (config.txt: usb_max_current_enable=1)
#    3. 2GB スワップ領域の作成
#    4. USB WiFi ドングルドライバ (RTL8811AU) の導入(既定で無効)
#    5. ubuntu-desktop / ros-jazzy-desktop と ROS 開発ツールの導入
#    6. rosdep の初期化・更新
#    7. ~/.bashrc への ROS 2 source 追記
#
#  通常は kk_robot_setup.sh から呼び出されます(環境変数を引き継ぎます)。
#  単体でも実行できます(未設定の環境変数は既定値を使用):
#    ./setup/env_setup.sh
# =============================================================================
set -euo pipefail

# ---- 環境変数(kk_robot_setup.sh から export。単体実行時は既定値)-----------
: "${ROS_DISTRO:=jazzy}"
: "${USER_NAME:=$(id -un)}"
: "${SETUP_WIFI_DONGLE:=0}"

log() { printf '\033[1;36m[env-setup]\033[0m %s\n' "$*"; }

# =============================================================================
# 1. パスワード無し sudo の設定
#    /etc/sudoers.d/ に NOPASSWD 設定を作成。最初の sudo で1度だけパスワードを聞かれます。
# =============================================================================
log "1. パスワード無し sudo を設定"
if ! sudo -n true 2>/dev/null; then
  echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" | sudo tee "/etc/sudoers.d/${USER_NAME}-nopasswd" >/dev/null
  sudo chmod 440 "/etc/sudoers.d/${USER_NAME}-nopasswd"
  sudo visudo -c -f "/etc/sudoers.d/${USER_NAME}-nopasswd"   # 文法チェック
fi
log "   -> 以後 sudo はパスワード不要"

# =============================================================================
# 2. USB 最大電流の有効化 (/boot/firmware/config.txt)
#    Pi 5 で USB 周辺機器(カメラ/マイク等)に十分な電流を供給する。
#    反映には再起動が必要。既に設定済みならスキップ。
# =============================================================================
log "2. USB 最大電流を有効化 (usb_max_current_enable=1)"
CONFIG_TXT=""
if [ -f /boot/firmware/config.txt ]; then
  CONFIG_TXT=/boot/firmware/config.txt
elif [ -f /boot/config.txt ]; then
  CONFIG_TXT=/boot/config.txt
fi
if [ -n "${CONFIG_TXT}" ]; then
  if grep -qE '^[[:space:]]*usb_max_current_enable[[:space:]]*=' "${CONFIG_TXT}"; then
    log "   -> 既に設定済み (${CONFIG_TXT})"
  else
    printf '\nusb_max_current_enable=1\n' | sudo tee -a "${CONFIG_TXT}" >/dev/null
    log "   -> ${CONFIG_TXT} に追記 (再起動後に有効)"
  fi
else
  log "   -> config.txt が見つからないためスキップ"
fi

# =============================================================================
# 3. 2GB スワップ領域の作成 (/swapfile) と /etc/fstab への永続化
# =============================================================================
log "3. 2GB スワップを作成"
if ! swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
fi
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
log "   -> $(swapon --show | grep /swapfile || echo 'swap 有効')"

# =============================================================================
# 4. apt リポジトリ準備 と 大型パッケージの導入
# =============================================================================
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # サービス再起動の確認ダイアログを抑制

log "4-1. universe リポジトリと基本ツール"
sudo add-apt-repository -y universe
sudo apt-get update -qq
sudo apt-get install -y curl gnupg lsb-release ca-certificates git

log "4-2. ROS 2 apt リポジトリを追加"
if ! dpkg -s ros2-apt-source >/dev/null 2>&1; then
  RAS_VER=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest \
            | grep -F '"tag_name"' | awk -F\" '{print $4}')
  CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
  curl -L -o /tmp/ros2-apt-source.deb \
    "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${RAS_VER}/ros2-apt-source_${RAS_VER}.${CODENAME}_all.deb"
  sudo apt-get install -y /tmp/ros2-apt-source.deb
  sudo apt-get update -qq
fi

log "4-3. ROS 2 開発ツール / ros-${ROS_DISTRO}-desktop / ubuntu-desktop を導入(時間がかかります)"
sudo -E apt-get install -y python3-colcon-common-extensions python3-rosdep python3-vcstool ros-dev-tools
sudo -E apt-get install -y "ros-${ROS_DISTRO}-desktop"
sudo -E apt-get install -y ubuntu-desktop

# =============================================================================
# 5. USB WiFi ドングルドライバ (RTL8811AU) — 既定で無効
#    DKMS ビルドには稼働カーネルに一致する linux-headers-$(uname -r) が必須だが、
#    アーカイブから当該ヘッダーが削除された古いカーネルで動作中の Pi では入手できず、
#    set -e によりセットアップ全体が停止してしまうため。
#    ドングルを使う Pi では SETUP_WIFI_DONGLE=1 を付けて実行する。
#      前提: sudo apt install linux-image-raspi linux-headers-raspi で最新カーネルに
#            更新して再起動し、uname -r と一致するヘッダーが入手可能な状態にしておく。
# =============================================================================
if [ "${SETUP_WIFI_DONGLE}" = "1" ]; then
  log "5. USB WiFi ドングルドライバ (RTL8811AU: BUFFALO WI-U2-433 等)"
  # 詳細は docs/usb-wifi-dongle.md を参照。DKMS 導入によりカーネル更新後も自動再ビルド。
  KHDR="linux-headers-$(uname -r)"
  if sudo apt-get install -y --dry-run "${KHDR}" >/dev/null 2>&1; then
    sudo apt-get install -y "${KHDR}" dkms build-essential iw
    # Ubuntu の rtl8812au-dkms (2014年版) は RTL8811AU 非対応で強制バインド時に
    # カーネル oops を起こすため、誤ロードを封じる
    echo "blacklist 8812au" | sudo tee /etc/modprobe.d/blacklist-rtl8812au.conf >/dev/null
    if ! dkms status 2>/dev/null | grep -q '^rtl8821au/'; then
      DRV_SRC=/tmp/8821au-20210708
      rm -rf "${DRV_SRC}"
      git clone --depth 1 https://github.com/morrownr/8821au-20210708.git "${DRV_SRC}"
      DRV_VER="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${DRV_SRC}/dkms.conf")"
      sudo dkms add "${DRV_SRC}"
      sudo dkms build  "rtl8821au/${DRV_VER}"
      sudo dkms install "rtl8821au/${DRV_VER}"
      rm -rf "${DRV_SRC}"
    fi
    sudo modprobe 8821au 2>/dev/null || true   # ドングル未挿入でもロード自体は可
    log "   -> $(dkms status | grep '^rtl8821au/' || echo 'rtl8821au 未導入(要確認)')"
  else
    log "5. WiFi ドングルドライバをスキップ: ${KHDR} が入手不可(カーネル更新+再起動後に SETUP_WIFI_DONGLE=1 で再実行)"
  fi
else
  log "5. USB WiFi ドングルドライバは既定で無効 (有効化するには SETUP_WIFI_DONGLE=1 を付けて実行)"
fi

# =============================================================================
# 6. rosdep の初期化・更新
#    ワークスペースの依存解決 (rosdep install) は app_setup.sh が行います。
# =============================================================================
log "6. rosdep 初期化・更新"
sudo rosdep init 2>/dev/null || true
rosdep update

# =============================================================================
# 7. ~/.bashrc に ROS 2 の source を追記
# =============================================================================
log "7. ~/.bashrc に ROS 2 source を追記"
if ! grep -q "kk robot setup" "$HOME/.bashrc"; then
  cat >> "$HOME/.bashrc" <<EOF

# ===== ROS 2 (kk robot setup) =====
[ -f /opt/ros/${ROS_DISTRO}/setup.bash ] && source /opt/ros/${ROS_DISTRO}/setup.bash
[ -f "\$HOME/kk_ws/install/setup.bash" ] && source "\$HOME/kk_ws/install/setup.bash"
EOF
fi

log "=== 基本設定と ROS 導入が完了しました ==="
