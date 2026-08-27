#!/usr/bin/env bash
# =============================================================================
# 中継PC (SFU) セットアップスクリプト
#   - Goが未導入/古い場合は公式版を自動導入
#   - リポジトリを取得(clone/pull)し、relayサーバーをビルド
#   - 任意で systemd サービス登録 ( --service )
#
# ワンライナー:
#   curl -fsSL https://raw.githubusercontent.com/sanjofumihiro/ClaudeShareContents/main/webrtc-camera/setup/setup-relay.sh | bash
#   # systemd常駐させる場合:
#   curl -fsSL .../setup-relay.sh | bash -s -- --service
#
# 環境変数で上書き可:
#   INSTALL_DIR (既定 ~/ClaudeShareContents) / REPO_URL / ADDR (既定 :8080)
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sanjofumihiro/ClaudeShareContents.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/ClaudeShareContents}"
ADDR="${ADDR:-:8080}"
GO_MIN="1.21"
INSTALL_SERVICE=0
[ "${1:-}" = "--service" ] && INSTALL_SERVICE=1

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
log() { printf '\033[1;32m[setup-relay]\033[0m %s\n' "$*"; }

# --- 1) 前提パッケージ ---
log "apt: git curl ca-certificates tar"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq git curl ca-certificates tar

# --- 2) Go (未導入/古い場合のみ公式版を導入) ---
need_go() {
  command -v go >/dev/null 2>&1 || return 0
  local v
  v=$(go version | grep -oE 'go[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 | sed 's/go//')
  # GO_MIN が最小でなければ(=現バージョンがGO_MIN未満なら)導入が必要
  [ "$(printf '%s\n%s\n' "$GO_MIN" "$v" | sort -V | head -1)" != "$GO_MIN" ]
}
if need_go; then
  GO_VER=$(curl -fsSL "https://go.dev/VERSION?m=text" | head -1)   # 例: go1.23.5
  ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
  case "$ARCH" in
    amd64|x86_64)            GOARCH=amd64;;
    arm64|aarch64)           GOARCH=arm64;;
    armhf|armv7l|armv6l|arm) GOARCH=armv6l;;
    *)                       GOARCH="$ARCH";;
  esac
  log "install Go ${GO_VER} (${GOARCH}) -> /usr/local/go"
  curl -fsSL "https://go.dev/dl/${GO_VER}.linux-${GOARCH}.tar.gz" -o /tmp/go.tgz
  $SUDO rm -rf /usr/local/go
  $SUDO tar -C /usr/local -xzf /tmp/go.tgz
  export PATH="$PATH:/usr/local/go/bin"
  # 次回ログイン以降もPATHに通す
  if ! grep -qs '/usr/local/go/bin' "$HOME/.profile" 2>/dev/null; then
    echo 'export PATH=$PATH:/usr/local/go/bin' >> "$HOME/.profile"
  fi
else
  log "Go OK: $(go version)"
fi
command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin"

# --- 3) リポジトリ取得/更新 ---
if [ -d "$INSTALL_DIR/.git" ]; then
  log "update repo: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only || log "pull skip (ローカル変更あり)"
else
  log "clone repo -> $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# --- 4) ビルド ---
RELAY_DIR="$INSTALL_DIR/webrtc-camera/relay"
log "build relay (go build)"
( cd "$RELAY_DIR" && go build -o relay . )
log "built: $RELAY_DIR/relay"

# --- 5) systemd サービス (任意) ---
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  WEB_DIR="$INSTALL_DIR/webrtc-camera/web"
  log "install systemd service: webrtc-relay.service"
  $SUDO tee /etc/systemd/system/webrtc-relay.service >/dev/null <<UNIT
[Unit]
Description=WebRTC Camera Relay (SFU)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$RELAY_DIR/relay -addr $ADDR -web $WEB_DIR
WorkingDirectory=$INSTALL_DIR/webrtc-camera
Restart=on-failure
User=$(id -un)

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now webrtc-relay.service
  log "service started (status: systemctl status webrtc-relay)"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
log "=== 中継PC セットアップ完了 ==="
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  echo "  systemdで常駐起動済み (port $ADDR) : systemctl status webrtc-relay"
else
  echo "  手動起動:"
  echo "    cd $INSTALL_DIR/webrtc-camera"
  echo "    relay/relay -addr $ADDR -web web"
fi
echo "  ブラウザ: http://${IP:-<このPCのIP>}:8080/?id=PI01"
echo "  Pi一覧:   http://${IP:-<このPCのIP>}:8080/pis"
