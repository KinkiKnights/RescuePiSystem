#!/usr/bin/env bash
# =============================================================================
#  kk ロボット用 Raspberry Pi セットアップ(オーケストレーター)
#  (Ubuntu 24.04 / ROS 2 Jazzy)
# -----------------------------------------------------------------------------
#  対象: Raspberry Pi 5 (aarch64) + Ubuntu 24.04 LTS
#
#  このスクリプトの役割:
#    - 各種環境変数の定義(以下のサブスクリプトへ export で引き継ぐ)
#    - GitHub 用 SSH キーの生成・表示(GitHub に登録できるまで待機)
#    - どの処理を実行するかのユーザー選択(メニュー)
#
#  実処理は deploy/robot/ 内のサブスクリプトが担当します:
#    - env_setup.sh : 基本設定 (sudo/swap/WiFi) と ROS 2 の導入
#    - app_setup.sh : RescuePiSystem の各種環境構築 (依存/clone/build/systemd)
#    - update.sh    : RescuePiSystem を最新に更新して再ビルド
#
#  Pi 上で動くプログラムは RescuePiSystem リポジトリに集約されています:
#    - master_control/     : Web UI つきプログラム起動管理サーバ (port 80)
#    - camera_publisher/   : USB カメラ → WebRTC 配信 (relay へ)
#    - mic_publisher/      : USB マイク → FLAC ロスレス TCP 配信
#    - ros2/joy_node_web/  : Web ゲームパッド → sensor_msgs/Joy (submodule, colcon 対象)
#    - ros2/kk_can_bringup/: MCP2515 SocketCAN + ros2_socketcan bringup (colcon 対象)
#  外部 OSS は deploy/robot/rescue_pi_system.repos で参照(vcs import):
#    - ros2_socketcan      : CAN 通信 (上流 OSS)
#  CAN(MCP2515 HAT)のシステム側 bring-up は setup/can_setup.sh が担当
#  (HAT 装着機のみ。SETUP_CAN=1 で app_setup.sh から実行、または単体実行)。
#
#  ※ webrtc の中継(SFU=relay)サーバとビューアは「別マシン」で動かします
#    (本リポジトリ server/webrtc_relay の relay/web を参照)。relay は
#    RELAY_HOST:8080。publisher は relay が落ちても自動再接続します。
#
#  使い方(公開リポジトリ時に推奨のワンライナー):
#    curl -fsSL https://raw.githubusercontent.com/KinkiKnights/RescuePiSystem/main/deploy/robot/kk_robot_setup.sh | bash
#    本スクリプトは自己完結ブートストラップ:サブスクリプトが手元に無い場合
#    (raw URL の curl | bash 等)は、SSH キー生成→登録待ち→リポジトリ clone→
#    clone 先の自分自身を exec で再実行、まで自動で行う。
#  手動 clone の場合(SSH キー登録後):
#    git clone --recursive git@github.com:KinkiKnights/RescuePiSystem.git
#    ./RescuePiSystem/deploy/robot/kk_robot_setup.sh
#
#  ※ 別のラズパイでもそのまま実行できます。PI_ID はホスト名から自動生成します
#    (例: hostname=kk06 → PI_ID=KK06)。
# =============================================================================
set -euo pipefail

# ---- 各種環境変数の定義(サブスクリプトへ export で引き継ぐ)-----------------
export ROS_DISTRO="jazzy"
export WS="$HOME/kk_ws"                                              # ワークスペース
export REPO_SSH="git@github.com:KinkiKnights/RescuePiSystem.git"     # 優先 (SSH キーで認証)
export REPO_URL="https://github.com/KinkiKnights/RescuePiSystem.git" # 公開リポジトリ時のフォールバック
export REPO_DIR="${WS}/src/RescuePiSystem"
export PI_MODEL="pi5"                                     # publish-${PI_MODEL}.sh を使用 (pi4=HW / pi5=SW)
export RELAY_HOST="${RELAY_HOST:-192.168.137.1}"          # webrtc 中継(SFU)サーバのIP
export RELAY_URL="ws://${RELAY_HOST}:8080/ws"
export PI_ID="${PI_ID:-$(hostname | tr '[:lower:]' '[:upper:]')}"    # 配信ID(ホスト名から自動生成)
# カメラ入力ソース (camChange の番号1)。
#   USBカメラ(MJPEG出力)の例: "v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec"
#   CSIカメラの場合は         : "libcamerasrc"
export CAM1_SRC="${CAM1_SRC:-v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec}"
# マイク配信 (mic_publisher / FLAC ロスレス TCP)
export MIC_ALSA_DEV="${MIC_ALSA_DEV:-hw:1,0}"   # USBマイク (arecord -l で確認)
export MIC_RATE="${MIC_RATE:-48000}"            # 48000 または 44100 (マイクのネイティブ)
export MIC_PORT="${MIC_PORT:-5005}"             # 配信TCPポート
export USER_NAME="$(id -un)"
# USB WiFi ドングルドライバ (RTL8811AU) は既定で無効(詳細は env_setup.sh / docs)。
export SETUP_WIFI_DONGLE="${SETUP_WIFI_DONGLE:-0}"
# CAN (MCP2515 SPI HAT) の bring-up。HAT を装着した号機のみ 1(詳細は setup/can_setup.sh)。
export SETUP_CAN="${SETUP_CAN:-0}"
# ROS 2 ドメイン ID。号機内の全 ROS ノードで揃える(既定 0)。CAN ブリッジもこれを使う。
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
# Git ユーザー識別情報(commit 用。未設定なら以下を --global に設定する)。
export GIT_USER_NAME="${GIT_USER_NAME:-sanjo}"
export GIT_USER_EMAIL="${GIT_USER_EMAIL:-sanjo@kinkiknights.com}"

# サブスクリプトの場所(本スクリプトと同じ deploy/robot/ ディレクトリ)。
#   curl | bash のようにパイプで実行された場合は BASH_SOURCE が実ファイルを指さない
#   ため空になり得る(set -u 対策で :- を付ける)。その場合は下のブートストラップで
#   リポジトリを clone し、clone 先から自分自身を再実行する。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

log() { printf '\033[1;36m[kk-setup]\033[0m %s\n' "$*"; }

# 制御端末(/dev/tty)が使えるか。curl | bash 実行時は stdin がスクリプト本文の
# ため、対話入力はすべて /dev/tty から読む。ノードが存在しても制御端末が無いと
# 開けないため、実 open で判定する。
has_tty() { ( : < /dev/tty ) 2>/dev/null; }

# =============================================================================
#  GitHub 用 SSH キーの生成と登録待ち
#    push や非公開リポジトリのアクセスに使う ed25519 キーを用意し、公開鍵を表示。
#    ユーザーが GitHub に登録して Enter を押すまで待つ(既に認証できる場合はスキップ)。
# =============================================================================
# GitHub に SSH 認証できるか判定する。
#   ※ ssh -T git@github.com は認証成功でも「シェルを提供しない」ため終了コード 1 を返す。
#     set -o pipefail 下では `ssh ... | grep` の終了コードが ssh 側の 1 に引きずられ、
#     grep が一致(認証成功)しても関数が false になってしまう。これが
#     「登録済みでも『まだ GitHub に認証できません』と出る」バグの原因だった。
#     そのため ssh の出力を一旦変数に受けてから grep で判定する(パイプにしない)。
gh_auth_ok() {
  local out
  out="$(ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
             git@github.com 2>&1 || true)"
  printf '%s\n' "$out" | grep -q "successfully authenticated"
}

# Git のユーザー識別情報(commit に必須)を設定する。
#   既に設定済みなら尊重して上書きしない(GIT_USER_NAME / GIT_USER_EMAIL で変更可)。
#   git 未導入(基本設定前)の環境では何もしない(env_setup.sh 実行後に再設定される)。
setup_git_identity() {
  command -v git >/dev/null 2>&1 || return 0
  log "Git ユーザー識別情報を確認"
  git config --global user.name  >/dev/null 2>&1 || git config --global user.name  "${GIT_USER_NAME}"
  git config --global user.email >/dev/null 2>&1 || git config --global user.email "${GIT_USER_EMAIL}"
  log "   -> $(git config --global user.name) <$(git config --global user.email)>"
}

setup_github_key() {
  log "GitHub 用 SSH キーを確認"
  if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keygen -t ed25519 -C "$(hostname)-github" -f "$HOME/.ssh/id_ed25519" -N "" -q
    log "   -> 新しいキーを生成しました"
  fi
  if gh_auth_ok; then
    log "   -> GitHub に認証済み(登録作業は不要)"
    return 0
  fi
  echo "=================================================================="
  echo " 以下の公開鍵をコピーして GitHub に登録してください:"
  echo "   - アカウント全体で使う場合: https://github.com/settings/keys"
  echo "   - リポジトリ単位の場合   : 各リポジトリ Settings -> Deploy keys"
  echo "------------------------------------------------------------------"
  cat "$HOME/.ssh/id_ed25519.pub"
  echo "=================================================================="
  if has_tty; then
    while true; do
      printf '登録が完了したら Enter を押してください (登録せず続行する場合は s + Enter): '
      read -r ans < /dev/tty
      if [ "${ans:-}" = "s" ]; then
        log "   -> キー登録をスキップして続行します"
        break
      fi
      if gh_auth_ok; then
        log "   -> GitHub 認証を確認できました"
        break
      fi
      echo "   まだ GitHub に認証できません。登録内容を確認してください。"
    done
  else
    log "   (対話端末が無いため登録待ちをスキップします。上記キーは ~/.ssh/id_ed25519.pub)"
  fi
}

# =============================================================================
#  ブートストラップ(raw URL の curl | bash 実行に対応)
#    サブスクリプト(env_setup.sh 等)が手元に無い＝リポジトリを clone せずに
#    本スクリプト単体が流し込まれた状態。SSH キー登録後にリポジトリを clone し、
#    clone 先の kk_robot_setup.sh を exec で再実行する(以降は通常フロー)。
# =============================================================================
siblings_present() {
  [ -n "${SCRIPT_DIR}" ] \
    && [ -f "${SCRIPT_DIR}/env_setup.sh" ] \
    && [ -f "${SCRIPT_DIR}/app_setup.sh" ] \
    && [ -f "${SCRIPT_DIR}/update.sh" ]
}

bootstrap_clone_and_reexec() {
  # 二重ブートストラップ(clone 失敗時の無限ループ)を防ぐガード。
  if [ "${KK_BOOTSTRAPPED:-0}" = "1" ]; then
    log "エラー: clone 後もサブスクリプトが見つかりません(clone 失敗の可能性)。"
    log "        手動で clone してから deploy/robot/kk_robot_setup.sh を実行してください。"
    exit 1
  fi
  log "サブスクリプトが手元に無いため、リポジトリを clone してから続行します(ブートストラップ)"
  mkdir -p "${WS}/src"
  # SSH (登録済みキー) を優先し、失敗時のみ HTTPS (公開リポジトリのみ有効)。
  [ -d "${REPO_DIR}" ] \
    || GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone --recursive "${REPO_SSH}" "${REPO_DIR}" \
    || git clone --recursive "${REPO_URL}" "${REPO_DIR}"
  log "   -> clone 完了。clone 先の kk_robot_setup.sh を再実行します"
  export KK_BOOTSTRAPPED=1   # 環境変数(定義済みの設定含む)は exec に引き継がれる
  exec bash "${REPO_DIR}/deploy/robot/kk_robot_setup.sh"
}

# =============================================================================
#  サブスクリプトの実行
# =============================================================================
run_sub() {
  local name="$1"
  local path="${SCRIPT_DIR}/${name}"
  if [ ! -f "${path}" ]; then
    log "エラー: ${path} が見つかりません(リポジトリを clone した状態で実行してください)"
    exit 1
  fi
  log ">>> ${name} を実行"
  bash "${path}"
}

# =============================================================================
#  メイン: Git 識別情報 → SSH キー設定 →(必要なら)ブートストラップ clone → 実行内容の選択
# =============================================================================
setup_git_identity
setup_github_key

# raw URL の curl | bash 実行など、サブスクリプトが手元に無い場合は
# リポジトリを clone して clone 先から自分を再実行する(戻ってこない=exec)。
if ! siblings_present; then
  bootstrap_clone_and_reexec
fi

echo ""
echo "=================================================================="
echo " 実行する処理を選択してください:"
echo "   1) 新規セットアップ (env_setup + app_setup) [推奨: 初回]"
echo "   2) 基本設定 + ROS 導入のみ (env_setup)"
echo "   3) RescuePiSystem 環境構築のみ (app_setup)"
echo "   4) 更新して再ビルド (update)"
echo "   q) 何もせず終了"
echo "=================================================================="

# 選択肢は環境変数 CHOICE でも指定可能(非対話実行向け。例: CHOICE=1)。
choice="${CHOICE:-}"
if [ -z "${choice}" ]; then
  if has_tty; then
    printf '番号を入力してください [1-4/q]: '
    read -r choice < /dev/tty
  else
    log "対話端末が無いため既定(1: 新規セットアップ)で実行します。CHOICE=N で指定できます。"
    choice="1"
  fi
fi

case "${choice}" in
  1)
    run_sub env_setup.sh
    run_sub app_setup.sh
    ;;
  2)
    run_sub env_setup.sh
    ;;
  3)
    run_sub app_setup.sh
    ;;
  4)
    run_sub update.sh
    ;;
  q|Q|"")
    log "何もせず終了します。"
    exit 0
    ;;
  *)
    log "不明な選択: '${choice}'。終了します。"
    exit 1
    ;;
esac

log "=== 完了 ==="
