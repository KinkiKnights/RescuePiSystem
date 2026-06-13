#!/usr/bin/env bash
# =============================================================================
# Raspberry Pi (パブリッシャ) セットアップスクリプト   ※Ubuntu 24.04想定
#   - GStreamer (webrtcbin/HWエンコード) と Python シグナリング依存を導入
#   - リポジトリを取得(clone/pull)し、起動スクリプトに実行権限付与
#   - 導入後に webrtcbin が使えるか自動検証
#   - 任意で systemd サービス登録 ( --service )
#
# ワンライナー:
#   curl -fsSL https://raw.githubusercontent.com/sanjofumihiro/ClaudeShareContents/main/webrtc-camera/setup/setup-pi.sh | bash
#   # 中継PCを指定し systemd常駐:
#   curl -fsSL .../setup-pi.sh | PI_ID=PI01 SERVER=ws://192.168.1.10:8080/ws MODEL=pi4 bash -s -- --service
#
# 環境変数で上書き可:
#   INSTALL_DIR / REPO_URL / PI_ID / SERVER / MODEL(pi4|pi5)
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sanjofumihiro/ClaudeShareContents.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/ClaudeShareContents}"
PI_ID="${PI_ID:-PI01}"
SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
MODEL="${MODEL:-pi4}"          # pi4=HWエンコード / pi5=SWエンコード
INSTALL_SERVICE=0
[ "${1:-}" = "--service" ] && INSTALL_SERVICE=1

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
log() { printf '\033[1;32m[setup-pi]\033[0m %s\n' "$*"; }

# --- 1) 必須パッケージ (これらが無いと動かない) ---
log "apt update"
$SUDO apt-get update -qq
log "install core deps (GStreamer + Python signaling)"
$SUDO apt-get install -y \
  git ca-certificates \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-nice \
  gstreamer1.0-libav \
  python3 python3-gi python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  gir1.2-gst-plugins-bad-1.0 \
  python3-websockets \
  v4l-utils

# --- 2) 任意パッケージ (CSIカメラ等。無い環境では失敗を無視) ---
log "install optional deps (libcamera等。失敗は無視)"
$SUDO apt-get install -y \
  gstreamer1.0-libcamera \
  libcamera-tools \
  gstreamer1.0-plugins-ugly 2>/dev/null || log "optional packages skipped"

# --- 3) リポジトリ取得/更新 ---
if [ -d "$INSTALL_DIR/.git" ]; then
  log "update repo: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only || log "pull skip (ローカル変更あり)"
else
  log "clone repo -> $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
chmod +x "$INSTALL_DIR"/webrtc-camera/publisher/*.sh

# --- 4) 検証 (webrtcbin と Python GI が揃っているか) ---
log "verify webrtcbin / python GI"
gst-inspect-1.0 webrtcbin >/dev/null 2>&1 && log "  webrtcbin: OK" || { log "  webrtcbin: NG (gstreamer1.0-plugins-bad/nice を確認)"; exit 1; }
python3 - <<'PY'
import gi
gi.require_version('Gst','1.0'); gi.require_version('GstWebRTC','1.0'); gi.require_version('GstSdp','1.0')
from gi.repository import Gst
Gst.init(None)
print("  python GstWebRTC: OK -", Gst.version_string())
PY

# --- 5) systemd サービス (任意) ---
SCRIPT="publish-${MODEL}.sh"
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  PUB_DIR="$INSTALL_DIR/webrtc-camera/publisher"
  log "install systemd service: webrtc-publisher.service (id=$PI_ID model=$MODEL)"
  $SUDO tee /etc/systemd/system/webrtc-publisher.service >/dev/null <<UNIT
[Unit]
Description=WebRTC Camera Publisher ($PI_ID)
After=network-online.target
Wants=network-online.target

[Service]
Environment=PI_ID=$PI_ID
Environment=SERVER=$SERVER
WorkingDirectory=$PUB_DIR
ExecStart=$PUB_DIR/$SCRIPT
Restart=on-failure
RestartSec=3
User=$(id -un)

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now webrtc-publisher.service
  log "service started (status: systemctl status webrtc-publisher)"
fi

echo ""
log "=== ラズパイ セットアップ完了 ==="
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  echo "  systemdで常駐起動済み (id=$PI_ID, $SCRIPT, SERVER=$SERVER)"
  echo "  ログ: journalctl -u webrtc-publisher -f"
else
  echo "  手動起動 (Pi4=HW / Pi5=SW):"
  echo "    cd $INSTALL_DIR/webrtc-camera/publisher"
  echo "    PI_ID=$PI_ID SERVER=ws://<中継PCのIP>:8080/ws ./publish-pi4.sh   # Pi4"
  echo "    PI_ID=$PI_ID SERVER=ws://<中継PCのIP>:8080/ws ./publish-pi5.sh   # Pi5"
fi
