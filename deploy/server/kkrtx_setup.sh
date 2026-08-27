#!/usr/bin/env bash
# =============================================================================
#  kkrtx_setup.sh  —  運用サーバ (kkrtx) 側のセットアップ
# -----------------------------------------------------------------------------
#  kkrtx で動かすプロセスをまとめて構成します:
#    - control_ui    : 操作画面サーバ (FastAPI, port 80)
#    - voice_comm    : PTT 音声中継 (port 8766)
#    - mic_hub       : 号機マイクの集約ハブ (port 8770)
#    - webrtc_relay  : WebRTC 中継 SFU (Go, port 8080)
#
#  やること: 依存パッケージ導入 → Go 導入 (未導入時のみ) → relay ビルド →
#            systemd ユニットを deploy/systemd のテンプレートから設置 → 起動。
#  ユニットの内容はテンプレートが単一の真実で、このスクリプトは展開するだけ。
#
#  使い方 (リポジトリを clone した後):
#    ./deploy/server/kkrtx_setup.sh
#    SETUP_SERVICES=0 ./deploy/server/kkrtx_setup.sh   # 依存導入とビルドのみ
#
#  環境変数:
#    REPO_DIR        リポジトリの場所 (既定: このスクリプトから自動判定)
#    RELAY_PORT      relay の待受ポート (既定: config/units.json の server.relay_port)
#    SETUP_SERVICES  1=systemd 設置と起動まで行う (既定 1)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
: "${REPO_DIR:=$(cd "${SCRIPT_DIR}/../.." && pwd)}"
: "${USER_NAME:=$(id -un)}"
: "${SETUP_SERVICES:=1}"
SYSTEMD_DIR="${REPO_DIR}/deploy/systemd"
GO_MIN="1.21"

log() { printf '\033[1;35m[kkrtx-setup]\033[0m %s\n' "$*"; }

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# units.json からポートを読む (jq を要求しない。標準ライブラリの python3 だけ)。
cfg_port() {
  python3 - "$1" <<'PY'
import json, sys, pathlib
key = sys.argv[1]
try:
    data = json.loads(pathlib.Path("config/units.json").read_text(encoding="utf-8"))
    print((data.get("server") or {}).get(key, ""))
except Exception:
    print("")
PY
}
cd "${REPO_DIR}"
: "${RELAY_PORT:=$(cfg_port relay_port)}"
: "${RELAY_PORT:=8080}"

# =============================================================================
# 1. 依存パッケージ
# =============================================================================
log "1. 依存パッケージ (Python / ping / Go ビルド用)"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
#   control_ui / voice_comm は FastAPI + uvicorn。mic_hub は標準ライブラリのみ。
#   ping 監視に iputils-ping、証明書生成に python3-cryptography。
$SUDO apt-get install -y -qq \
  git curl ca-certificates tar \
  python3 python3-fastapi python3-uvicorn python3-websockets \
  python3-cryptography iputils-ping

# =============================================================================
# 2. Go (未導入/古い場合のみ公式版を /usr/local/go へ)
# =============================================================================
need_go() {
  command -v go >/dev/null 2>&1 || return 0
  local v
  v=$(go version | grep -oE 'go[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 | sed 's/go//')
  [ "$(printf '%s\n%s\n' "$GO_MIN" "$v" | sort -V | head -1)" != "$GO_MIN" ]
}
if need_go; then
  GO_VER=$(curl -fsSL "https://go.dev/VERSION?m=text" | head -1)
  ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
  case "$ARCH" in
    amd64|x86_64)            GOARCH=amd64;;
    arm64|aarch64)           GOARCH=arm64;;
    armhf|armv7l|armv6l|arm) GOARCH=armv6l;;
    *)                       GOARCH="$ARCH";;
  esac
  log "2. Go ${GO_VER} (${GOARCH}) を /usr/local/go へ導入"
  curl -fsSL "https://go.dev/dl/${GO_VER}.linux-${GOARCH}.tar.gz" -o /tmp/go.tgz
  $SUDO rm -rf /usr/local/go
  $SUDO tar -C /usr/local -xzf /tmp/go.tgz
  export PATH="$PATH:/usr/local/go/bin"
  grep -qs '/usr/local/go/bin' "$HOME/.profile" 2>/dev/null \
    || echo 'export PATH=$PATH:/usr/local/go/bin' >> "$HOME/.profile"
else
  log "2. Go OK: $(go version)"
fi
command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin"

# =============================================================================
# 3. webrtc relay のビルド
# =============================================================================
log "3. webrtc relay を go build"
( cd "${REPO_DIR}/server/webrtc_relay" && go build -o webrtc_relay . )
log "   生成: ${REPO_DIR}/server/webrtc_relay/webrtc_relay"

# =============================================================================
# 4. systemd ユニットの設置と起動
# =============================================================================
if [ "${SETUP_SERVICES}" != "1" ]; then
  log "4. systemd 設置はスキップ (SETUP_SERVICES=0)"
else
  log "4. systemd ユニットを設置 (テンプレート展開)"
  source "${SYSTEMD_DIR}/install_unit.sh"
  install_unit control-ui
  install_unit voice-comm
  install_unit mic-hub
  install_unit webrtc-relay "RELAY_PORT=${RELAY_PORT}"
  install_default mic-hub
  $SUDO mkdir -p "$(. /etc/default/mic-hub 2>/dev/null; echo "${MIC_HUB_OUTDIR:-$HOME/kk_ws/logs/mic-recordings}")"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now control-ui.service voice-comm.service mic-hub.service webrtc-relay.service
fi

# =============================================================================
# 5. 簡易セルフチェック
# =============================================================================
log "5. セルフチェック"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
for svc in control-ui voice-comm mic-hub webrtc-relay; do
  if systemctl is-active --quiet "${svc}.service" 2>/dev/null; then
    echo "   [OK] ${svc} 稼働中"
  else
    echo "   [--] ${svc} 停止 (SETUP_SERVICES=0 なら正常)"
  fi
done
curl -s -o /dev/null --max-time 5 -w "   [HTTP %{http_code}] 操作画面 http://127.0.0.1/\n" "http://127.0.0.1/" || true
curl -s -o /dev/null --max-time 5 -w "   [HTTP %{http_code}] mic hub  http://127.0.0.1:8770/healthz\n" "http://127.0.0.1:8770/healthz" || true
curl -s -o /dev/null --max-time 5 -w "   [HTTP %{http_code}] relay    http://127.0.0.1:${RELAY_PORT}/pis\n" "http://127.0.0.1:${RELAY_PORT}/pis" || true

log "=== kkrtx のセットアップが完了しました ==="
echo "  - 操作画面:   http://${IP:-<kkrtxのIP>}/            (control / analytics / engineer / reporter / master)"
echo "  - mic hub:    http://${IP:-<kkrtxのIP>}:8770/       (号機の状態・試聴)"
echo "  - relay:      http://${IP:-<kkrtxのIP>}:${RELAY_PORT}/pis   (接続中の号機一覧)"
echo "  - 音声中継:   ws://${IP:-<kkrtxのIP>}:8766/voice"
echo "  - HTTPS 化:   python3 server/control_ui/make_cert.py で certs/ を生成し再起動"
