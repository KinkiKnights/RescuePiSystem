#!/usr/bin/env bash
# =============================================================================
#  server_ctl.sh  —  運用サーバ (kkrtx) の 4 プロセスをまとめて起動・停止する
# -----------------------------------------------------------------------------
#  対象:
#    - control_ui    : 操作画面サーバ (FastAPI)
#    - webrtc_relay  : WebRTC 中継 SFU (Go)
#    - voice_comm    : PTT 音声中継
#    - mic_hub       : 号機マイクの集約ハブ
#
#  systemd で常駐させず、必要なときだけ手で起動して終わったら止める運用のための
#  制御スクリプト。systemd で常駐させる運用は deploy/server/kkrtx_setup.sh が
#  今までどおり面倒を見る。**両方を同時に使わないこと** (ポートが衝突する)。
#
#  使い方:
#    ./deploy/server/server_ctl.sh start              # 4 つ全部
#    ./deploy/server/server_ctl.sh start mic_hub      # 個別指定 (複数可)
#    ./deploy/server/server_ctl.sh status
#    ./deploy/server/server_ctl.sh restart webrtc_relay
#    ./deploy/server/server_ctl.sh stop
#    ./deploy/server/server_ctl.sh logs -f control_ui # 末尾を表示 (-f で追従)
#
#  PID とログはリポジトリの外 (既定 ~/.local/state/rescue-pi) に置く。
#  リポジトリを汚さないため (CLAUDE.md 規約 6 と同じ趣旨)。
#  ポートは config/units.json が単一の真実 (CLAUDE.md 規約 5)。下の環境変数で
#  一時的に上書きできる。
#
#  機体ごとの運用値は ~/.config/rescue-pi/server.env に置く (規約 6)。
#  優先順位は **コマンドライン > server.env > スクリプト既定値**。
#
#  環境変数:
#    REPO_DIR               リポジトリの場所 (既定: このスクリプトから自動判定)
#    RESCUE_SERVER_ENV      機体ごとの設定ファイル
#                           (既定 ~/.config/rescue-pi/server.env。無くてもよい)
#    RESCUE_STATE_DIR       PID / ログの置き場所 (既定 ~/.local/state/rescue-pi)
#    CONTROL_UI_PORT        操作画面のポート (既定: units.json の control_ui_port)
#    CONTROL_UI_UNPRIV_PORT 特権ポートを避けるときの代替ポート (既定 8000)
#    CONTROL_UI_SUDO        1 で control_ui だけ sudo 起動し特権ポートに bind
#    RELAY_PORT / VOICE_PORT / MIC_HUB_PORT      各サービスのポート
#    MIC_HUB_OUTDIR / MIC_HUB_SEGMENT
#    MIC_HUB_RETENTION_HOURS / MIC_HUB_MAX_GB    録音の設定
#    STOP_GRACE             SIGTERM から SIGKILL までの猶予秒 (既定 8)
# =============================================================================
set -euo pipefail

# ワークスペース直下などにシンボリックリンクを置いて叩けるよう、リンクを解決して
# から実体の位置を求める。解決しないと dirname がリンクの置き場所を返すので、
# そこから 2 つ上を REPO_DIR にしている下の行が見当違いの場所を指す
# (例: ~/kk_mft_ws/server_ctl.sh から呼ぶと REPO_DIR が /home/kk になる)。
SCRIPT_SRC="${BASH_SOURCE[0]:-$0}"
[ -L "${SCRIPT_SRC}" ] && SCRIPT_SRC="$(readlink -f "${SCRIPT_SRC}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SRC}")" && pwd)"
: "${REPO_DIR:=$(cd "${SCRIPT_DIR}/../.." && pwd)}"

SERVICES=(control_ui webrtc_relay voice_comm mic_hub)

log()  { printf '\033[1;35m[server-ctl]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[server-ctl]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[server-ctl]\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 機体ごとの運用値 (~/.config/rescue-pi/server.env)
# -----------------------------------------------------------------------------
#  ディスク容量やポートの都合は機体ごとに違うので、リポジトリの外に置く
#  (CLAUDE.md 規約 6。リポジトリ内の既定値を書き換えると git pull で巻き戻るし、
#   1 台の事情がフリート共通の既定になってしまう)。
#  中身はただの KEY=value。例:
#      MIC_HUB_MAX_GB=1
#
#  読み込みは下の : "${VAR:=...}" 群より **前** でなければならない。後ろだと
#  既定値の代入が先に効いてしまい、ファイルに書いた値が無視される。
#
#  優先順位は コマンドライン > server.env > スクリプト既定値。
#  source すると単純代入がコマンドラインの値を踏み潰すので、先に退避しておいて
#  後から書き戻す。こうすればファイル側は素の KEY=value のままでよい。
: "${RESCUE_SERVER_ENV:=${XDG_CONFIG_HOME:-$HOME/.config}/rescue-pi/server.env}"

RESCUE_ENV_KEYS=(
  REPO_DIR RESCUE_STATE_DIR
  CONTROL_UI_PORT CONTROL_UI_UNPRIV_PORT CONTROL_UI_SUDO CONTROL_UI_RELOAD
  RELAY_PORT VOICE_PORT MIC_HUB_PORT
  MIC_HUB_OUTDIR MIC_HUB_SEGMENT MIC_HUB_RETENTION_HOURS MIC_HUB_MAX_GB
  STOP_GRACE
)

load_server_env() {
  local f="${RESCUE_SERVER_ENV}" k kv
  [ -f "${f}" ] || return 0                     # 無くてよい。エラーにしない
  if [ ! -r "${f}" ]; then
    warn "設定ファイルを読めません (無視して続けます): ${f}"
    return 0
  fi
  local -a saved=()
  for k in "${RESCUE_ENV_KEYS[@]}"; do
    [ -n "${!k+set}" ] && saved+=("${k}=${!k}")  # コマンドラインで来た値を退避
  done
  # shellcheck disable=SC1090
  set -a; . "${f}" || die "設定ファイルの読み込みに失敗しました: ${f}"; set +a
  for kv in ${saved[@]+"${saved[@]}"}; do
    export "${kv%%=*}=${kv#*=}"                  # コマンドラインを勝たせる
  done
}
load_server_env

: "${RESCUE_STATE_DIR:=${XDG_STATE_HOME:-$HOME/.local/state}/rescue-pi}"
PID_DIR="${RESCUE_STATE_DIR}/run"
LOG_DIR="${RESCUE_STATE_DIR}/log"

# -----------------------------------------------------------------------------
# 設定 (ポートは units.json から。jq を要求しない = 標準ライブラリの python3 だけ)
# -----------------------------------------------------------------------------
cfg_port() {
  python3 - "$1" "${REPO_DIR}" <<'PY'
import json, sys, pathlib
key, repo = sys.argv[1], sys.argv[2]
try:
    data = json.loads((pathlib.Path(repo) / "config" / "units.json").read_text(encoding="utf-8"))
    print((data.get("server") or {}).get(key, ""))
except Exception:
    print("")
PY
}

: "${CONTROL_UI_PORT:=$(cfg_port control_ui_port)}"; : "${CONTROL_UI_PORT:=80}"
: "${RELAY_PORT:=$(cfg_port relay_port)}";           : "${RELAY_PORT:=8080}"
: "${VOICE_PORT:=$(cfg_port voice_port)}";           : "${VOICE_PORT:=8766}"
: "${MIC_HUB_PORT:=$(cfg_port mic_hub_port)}";       : "${MIC_HUB_PORT:=8770}"

: "${CONTROL_UI_UNPRIV_PORT:=8000}"
: "${CONTROL_UI_SUDO:=0}"
: "${CONTROL_UI_RELOAD:=0}"
: "${MIC_HUB_OUTDIR:=${HOME}/kk_ws/logs/mic-recordings}"
: "${MIC_HUB_SEGMENT:=60}"
: "${MIC_HUB_RETENTION_HOURS:=24}"
: "${MIC_HUB_MAX_GB:=8}"
: "${STOP_GRACE:=8}"

# 特権ポート (<1024) は一般ユーザの python では bind できない。systemd ユニットは
# AmbientCapabilities=CAP_NET_BIND_SERVICE で解決していたが、手動起動にそれは無い。
#   - 既定 : 非特権ポートへ自動退避する (操作画面の URL に :ポート が付く)
#   - CONTROL_UI_SUDO=1 : control_ui だけ sudo で起動して従来のポートに bind する
#     (このとき control_ui が起動する子プロセスも root になる点に注意。
#      damiyan 検出器のログが root 所有で作られる)
CONTROL_UI_VIA_SUDO=0
if [ "${CONTROL_UI_PORT}" -lt 1024 ] && [ "$(id -u)" -ne 0 ]; then
  if [ "${CONTROL_UI_SUDO}" = "1" ]; then
    CONTROL_UI_VIA_SUDO=1
  else
    warn "control_ui: ポート ${CONTROL_UI_PORT} は特権ポートのため ${CONTROL_UI_UNPRIV_PORT} を使います"
    warn "            (従来どおり ${CONTROL_UI_PORT} で待受するなら CONTROL_UI_SUDO=1 を付けて実行)"
    CONTROL_UI_PORT="${CONTROL_UI_UNPRIV_PORT}"
  fi
fi

RELAY_BIN="${REPO_DIR}/server/webrtc_relay/webrtc_relay"

# -----------------------------------------------------------------------------
# サービス定義: 作業ディレクトリ / 環境変数 / 起動コマンド / ポート / ps 照合パターン
#   svc_spec <name> を呼ぶと SVC_DIR / SVC_ENV / SVC_CMD / SVC_PORT / SVC_PAT が入る
# -----------------------------------------------------------------------------
svc_spec() {
  SVC_DIR=""; SVC_ENV=(); SVC_CMD=(); SVC_PORT=""; SVC_PAT=""
  case "$1" in
    control_ui)
      SVC_DIR="${REPO_DIR}/server/control_ui"
      SVC_ENV=("HOME=${HOME}" "CONTROL_UI_PORT=${CONTROL_UI_PORT}" "CONTROL_UI_RELOAD=${CONTROL_UI_RELOAD}")
      SVC_CMD=(/usr/bin/python3 "${REPO_DIR}/server/control_ui/main.py")
      SVC_PORT="${CONTROL_UI_PORT}"
      SVC_PAT="control_ui/main.py"
      ;;
    webrtc_relay)
      SVC_DIR="${REPO_DIR}/server/webrtc_relay"
      SVC_ENV=("HOME=${HOME}")
      SVC_CMD=("${RELAY_BIN}" -addr ":${RELAY_PORT}" -web "${REPO_DIR}/server/webrtc_relay/web")
      SVC_PORT="${RELAY_PORT}"
      SVC_PAT="webrtc_relay"
      ;;
    voice_comm)
      # 待受ポートは server.py の PORT 定数 (8766)。引数では変えられないので、
      # units.json と食い違ったまま気づかない事故を防ぐため start 時に照合する。
      SVC_DIR="${REPO_DIR}/server/voice_comm"
      SVC_ENV=("HOME=${HOME}")
      SVC_CMD=(/usr/bin/python3 "${REPO_DIR}/server/voice_comm/server.py")
      SVC_PORT="${VOICE_PORT}"
      SVC_PAT="voice_comm/server.py"
      ;;
    mic_hub)
      SVC_DIR="${REPO_DIR}/server/mic_hub"
      SVC_ENV=("HOME=${HOME}")
      SVC_CMD=(/usr/bin/python3 "${REPO_DIR}/server/mic_hub/mic_hub.py"
               --port "${MIC_HUB_PORT}"
               --outdir "${MIC_HUB_OUTDIR}"
               --segment "${MIC_HUB_SEGMENT}"
               --retention-hours "${MIC_HUB_RETENTION_HOURS}"
               --max-gb "${MIC_HUB_MAX_GB}")
      SVC_PORT="${MIC_HUB_PORT}"
      SVC_PAT="mic_hub.py"
      ;;
    *) return 1;;
  esac
}

pidfile() { echo "${PID_DIR}/$1.pid"; }
logfile() { echo "${LOG_DIR}/$1.log"; }

# 記録した PID が「まだ生きていて、かつ当該サービスである」ことまで確かめる。
# PID は再利用されるので、生存確認だけでは無関係なプロセスを掴む事故が起きる。
svc_pid() {
  local name="$1" pf pid args
  pf="$(pidfile "${name}")"
  [ -f "${pf}" ] || return 1
  pid="$(cat "${pf}" 2>/dev/null || true)"
  case "${pid}" in ''|*[!0-9]*) return 1;; esac
  args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
  [ -n "${args}" ] || return 1
  svc_spec "${name}" || return 1
  case "${args}" in *"${SVC_PAT}"*) echo "${pid}"; return 0;; esac
  return 1
}

# ss の Local Address:Port 欄が指定ポートで終わっているか (0.0.0.0:80 / *:80 / [::]:80)
port_open() {
  ss -tln 2>/dev/null \
    | awk -v p="$1" 'NR>1 { n = split($4, a, ":"); if (a[n] == p) found = 1 } END { exit !found }'
}

# 権限が足りないときだけ sudo に落とす (CONTROL_UI_SUDO=1 で root 起動した場合用)
signal_group() {
  local sig="$1" pgid="$2"
  kill "-${sig}" "-${pgid}" 2>/dev/null && return 0
  sudo -n kill "-${sig}" "-${pgid}" 2>/dev/null || true
}

group_alive() { pgrep -g "$1" >/dev/null 2>&1; }

# -----------------------------------------------------------------------------
# start
# -----------------------------------------------------------------------------
start_one() {
  local name="$1" pf lf pid i
  svc_spec "${name}" || die "未知のサービス: ${name}"
  pf="$(pidfile "${name}")"; lf="$(logfile "${name}")"

  if pid="$(svc_pid "${name}")"; then
    log "${name}: 既に起動しています (PID ${pid}) — 何もしません"
    return 0
  fi
  rm -f "${pf}"          # 死んだ PID ファイルが残っていただけなら片付ける
  svc_spec "${name}"     # svc_pid が上書きしているので取り直す

  if port_open "${SVC_PORT}"; then
    warn "${name}: ポート ${SVC_PORT} は既に使用中です。起動を中止しました"
    warn "        (systemd の ${name//_/-}.service が動いていないか確認してください)"
    return 1
  fi

  if [ "${name}" = "webrtc_relay" ] && [ ! -x "${RELAY_BIN}" ]; then
    warn "${name}: バイナリがありません: ${RELAY_BIN}"
    warn "        先に SETUP_SERVICES=0 ./deploy/server/kkrtx_setup.sh を実行してください"
    return 1
  fi
  if [ "${name}" = "voice_comm" ] && [ "${VOICE_PORT}" != "8766" ]; then
    warn "${name}: units.json は ${VOICE_PORT} ですが server.py は 8766 固定です。合わせてください"
  fi
  if [ "${name}" = "mic_hub" ]; then
    mkdir -p "${MIC_HUB_OUTDIR}" 2>/dev/null || warn "${name}: 録音先を作れません: ${MIC_HUB_OUTDIR}"
  fi

  local -a launcher=()
  if [ "${name}" = "control_ui" ] && [ "${CONTROL_UI_VIA_SUDO}" = "1" ]; then launcher=(sudo -n); fi

  # setsid で新しいセッション (= 新しいプロセスグループ) の先頭にする。
  # こうしておくと stop でグループごと落とせるので、子や孫が孤児にならない。
  # stdin は /dev/null。端末や ssh セッションを掴んだままにしないため。
  #
  # PID は親の $! ではなく、子自身に $$ を書かせてから exec させて記録する。
  # $! はバックグラウンドにした AND-list を包むサブシェルの PID になることがあり
  # (実プロセスと 1 ずれる)、そうなると stop が本体を取り逃がして孤児が残る。
  # exec しているので $$ を書いたプロセスがそのまま本体になり、ズレようがない。
  ( cd "${SVC_DIR}" || exit 1
    setsid "${launcher[@]}" env "${SVC_ENV[@]}" \
      bash -c 'echo $$ >"$1"; shift; exec "$@"' _ "${pf}" "${SVC_CMD[@]}" \
      >>"${lf}" 2>&1 </dev/null & )

  for i in $(seq 1 25); do [ -s "${pf}" ] && break; sleep 0.2; done
  if [ ! -s "${pf}" ]; then
    warn "${name}: PID を記録できませんでした。ログ末尾:"; tail -n 15 "${lf}" >&2; return 1
  fi

  # 起動直後に落ちること (ポート衝突・import エラー) があるので確かめる
  for i in $(seq 1 20); do
    sleep 0.5
    if ! svc_pid "${name}" >/dev/null 2>&1; then
      warn "${name}: 起動に失敗しました。ログ末尾:"; tail -n 15 "${lf}" >&2; rm -f "${pf}"; return 1
    fi
    svc_spec "${name}"
    port_open "${SVC_PORT}" && break
  done

  pid="$(svc_pid "${name}" || true)"
  svc_spec "${name}"
  if port_open "${SVC_PORT}"; then
    log "${name}: 起動しました (PID ${pid}, port ${SVC_PORT}) log=${lf}"
  else
    warn "${name}: 起動しましたが port ${SVC_PORT} が待受になりません (PID ${pid})。ログ末尾:"
    tail -n 15 "${lf}" >&2
    return 1
  fi
}

# -----------------------------------------------------------------------------
# stop — プロセスグループごと SIGTERM → 猶予後 SIGKILL
#   robot/master_control/master_server.py の _terminate_tree() と同じ考え方。
#   追跡している親が先に終わっても孫が生き残ることがあるので、親の wait ではなく
#   「グループに誰も居なくなったか」を見て、残っていれば SIGKILL へ上げる。
# -----------------------------------------------------------------------------
stop_one() {
  local name="$1" pf pid pgid deadline
  svc_spec "${name}" || die "未知のサービス: ${name}"
  pf="$(pidfile "${name}")"

  if ! pid="$(svc_pid "${name}")"; then
    if [ -f "${pf}" ]; then rm -f "${pf}"; log "${name}: 停止済み (古い PID ファイルを削除しました)"
    else log "${name}: 停止しています"; fi
    return 0
  fi

  pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d ' ')"
  if [ -z "${pgid}" ]; then rm -f "${pf}"; log "${name}: 停止しています"; return 0; fi

  signal_group TERM "${pgid}"
  deadline=$(( $(date +%s) + STOP_GRACE ))
  while [ "$(date +%s)" -lt "${deadline}" ] && group_alive "${pgid}"; do sleep 0.3; done

  if group_alive "${pgid}"; then
    warn "${name}: SIGTERM で終わらないので SIGKILL します (pgid ${pgid})"
    signal_group KILL "${pgid}"
    sleep 1
  fi

  if group_alive "${pgid}"; then
    warn "${name}: プロセスグループ ${pgid} がまだ残っています: $(pgrep -g "${pgid}" | tr '\n' ' ')"
    rm -f "${pf}"
    return 1
  fi
  rm -f "${pf}"
  log "${name}: 停止しました (PID ${pid})"
}

# -----------------------------------------------------------------------------
# status / logs
# -----------------------------------------------------------------------------
status_one() {
  local name="$1" pid up note
  if pid="$(svc_pid "${name}")"; then
    svc_spec "${name}"
    up="$(ps -p "${pid}" -o etime= 2>/dev/null | tr -d ' ')"
    if port_open "${SVC_PORT}"; then note="port ${SVC_PORT} listen"
    else note="port ${SVC_PORT} \033[1;31m待受なし\033[0m"; fi
    printf '  \033[1;32m●\033[0m %-14s PID %-8s up %-12s %b\n' "${name}" "${pid}" "${up:-?}" "${note}"
  else
    svc_spec "${name}" || die "未知のサービス: ${name}"
    if port_open "${SVC_PORT}"; then note="port ${SVC_PORT} \033[1;33m別プロセスが使用中\033[0m"
    else note="port ${SVC_PORT}"; fi
    printf '  \033[1;30m○\033[0m %-14s %-27s %b\n' "${name}" "stopped" "${note}"
  fi
}

logs_one() {
  local name="$1" lf
  svc_spec "${name}" || die "未知のサービス: ${name}"
  lf="$(logfile "${name}")"
  if [ ! -f "${lf}" ]; then log "${name}: ログはまだありません (${lf})"; return 0; fi
  echo "===== ${lf} ====="
  if [ "${FOLLOW}" = "1" ]; then tail -n 40 -f "${lf}"; else tail -n 40 "${lf}"; fi
}

# -----------------------------------------------------------------------------
usage() {
  cat <<EOS
使い方: $(basename "$0") {start|stop|restart|status|logs} [サービス名...]

  サービス名: ${SERVICES[*]}  (省略すると全部)

  例:
    $(basename "$0") start
    $(basename "$0") start mic_hub voice_comm
    $(basename "$0") status
    $(basename "$0") logs -f control_ui
    $(basename "$0") stop
EOS
}

FOLLOW=0
ACTION="${1:-}"
[ $# -gt 0 ] && shift
case "${ACTION}" in
  -h|--help) usage; exit 0;;
  '')        usage; exit 2;;
esac

TARGETS=()
for a in "$@"; do
  case "${a}" in
    -f|--follow) FOLLOW=1;;
    *)           TARGETS+=("${a}");;
  esac
done
if [ ${#TARGETS[@]} -eq 0 ]; then TARGETS=("${SERVICES[@]}"); fi
for t in "${TARGETS[@]}"; do
  svc_spec "${t}" >/dev/null 2>&1 || die "未知のサービス: ${t} (使えるのは: ${SERVICES[*]})"
done

mkdir -p "${PID_DIR}" "${LOG_DIR}"

rc=0
case "${ACTION}" in
  start)
    log "REPO_DIR=${REPO_DIR}  state=${RESCUE_STATE_DIR}"
    for t in "${TARGETS[@]}"; do start_one "${t}" || rc=1; done
    ;;
  stop)
    for t in "${TARGETS[@]}"; do stop_one "${t}" || rc=1; done
    ;;
  restart)
    for t in "${TARGETS[@]}"; do stop_one "${t}" || rc=1; done
    for t in "${TARGETS[@]}"; do start_one "${t}" || rc=1; done
    ;;
  status)
    echo "RescuePiSystem サーバ (${REPO_DIR})"
    for t in "${TARGETS[@]}"; do status_one "${t}"; done
    ;;
  logs)
    for t in "${TARGETS[@]}"; do logs_one "${t}"; done
    ;;
  *) usage; exit 2;;
esac
exit "${rc}"
