#!/usr/bin/env bash
# =============================================================================
#  update.sh  —  RescuePiSystem を最新に更新して再ビルド
# -----------------------------------------------------------------------------
#  既にセットアップ済みの Pi で、リポジトリの更新を取り込みます:
#    1. RescuePiSystem の git pull(+ submodule joy_node_web の更新)
#    2. (外部依存も submodule なので 1. の submodule update で一緒に追従します)
#    3. colcon 再ビルド
#    4. master-control.service の再起動(稼働中の場合)
#
#  ※ programs.json や systemd ユニットなど Pi 固有の設定は上書きしません。
#    それらを作り直したい場合は app_setup.sh を実行してください。
#
#  通常は kk_robot_setup.sh から呼び出されます(環境変数を引き継ぎます)。
#  単体でも実行できます(未設定の環境変数は既定値を使用):
#    ./deploy/robot/update.sh
# =============================================================================
set -euo pipefail

# ---- 環境変数(kk_robot_setup.sh から export。単体実行時は既定値)-----------
: "${ROS_DISTRO:=jazzy}"
: "${WS:=$HOME/kk_ws}"
: "${REPO_DIR:=${WS}/src/RescuePiSystem}"

log() { printf '\033[1;36m[update]\033[0m %s\n' "$*"; }

if [ ! -d "${REPO_DIR}/.git" ]; then
  log "エラー: ${REPO_DIR} が見つかりません。先に app_setup.sh でセットアップしてください。"
  exit 1
fi

# =============================================================================
# 1. RescuePiSystem 本体と submodule (joy_node_web) を更新
# =============================================================================
log "0. カメラ / マイクのデバイス設定を確認(未作成なら自動検出して作成)"
python3 "${REPO_DIR}/robot/device_config.py" --init || log "   (初期化に失敗。Web UI から設定してください)"

log "1. RescuePiSystem を pull(現在のブランチを追従)"
BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD)"
git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"
log "   -> submodule (joy_node_web / ros2_socketcan / gm6020_control) を追従"
git -C "${REPO_DIR}" submodule update --init --recursive

# =============================================================================
# 2. 外部依存について
#    外部の ROS 2 パッケージ (ros2_socketcan / gm6020_control) は robot/ros2/ 配下の
#    submodule なので、上の submodule update で親リポジトリが指す固定コミットに
#    揃います。vcstool による ${WS}/src への別途 clone は廃止しました
#    (同名パッケージが二重になり colcon build が壊れるため)。
# =============================================================================

# 配信スクリプトの実行権限を復旧(pull で失われることがある)
chmod +x "${REPO_DIR}/robot/camera_publisher/"*.sh "${REPO_DIR}/robot/mic_publisher/"*.sh 2>/dev/null || true

# =============================================================================
# 3. colcon 再ビルド
# =============================================================================
log "3. colcon 再ビルド"
set +u; source "/opt/ros/${ROS_DISTRO}/setup.bash"; set -u
cd "${WS}"
rosdep install --from-paths src --ignore-src -r -y 2>/dev/null || log "   (rosdep 一部スキップ)"
colcon build --symlink-install

# =============================================================================
# 4. master-control.service の再起動(稼働中の場合のみ)
# =============================================================================
log "4. master-control.service を再起動"
if systemctl list-unit-files 2>/dev/null | grep -q '^master-control.service'; then
  sudo systemctl restart master-control.service
  sleep 2
  systemctl is-active --quiet master-control.service && echo "   [OK] master-control 稼働中" || echo "   [NG] master-control 停止"
else
  log "   (master-control.service 未登録のためスキップ: app_setup.sh を実行してください)"
fi

log "=== 更新が完了しました ==="
echo "  - 反映には再ログイン、または 'source ~/.bashrc' を実行してください。"
