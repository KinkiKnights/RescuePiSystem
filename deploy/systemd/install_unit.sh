#!/usr/bin/env bash
# =============================================================================
#  install_unit.sh — systemd ユニットテンプレートを展開して設置する共通処理
# -----------------------------------------------------------------------------
#  リポジトリには *.service.in (テンプレート) だけを置き、実ファイルの生成は
#  セットアップスクリプトだけが行う。これにより「リポジトリ内の .service と
#  スクリプトが生成する .service が二重に存在してズレる」事故を防ぐ。
#
#  使い方 (呼び出し側で REPO_DIR / USER_NAME を定義しておく):
#    SYSTEMD_DIR="${REPO_DIR}/deploy/systemd"
#    source "${SYSTEMD_DIR}/install_unit.sh"
#    install_unit master-control            # @REPO_DIR@ / @USER@ / @HOME@ を置換
#    install_unit control-ui PORT=80        # 追加の置換は KEY=VALUE で渡す
# =============================================================================
install_unit() {
  local name="$1"; shift
  local tpl="${SYSTEMD_DIR}/${name}.service.in"
  local dst="/etc/systemd/system/${name}.service"
  if [ ! -f "${tpl}" ]; then
    echo "[install_unit] テンプレートが見つかりません: ${tpl}" >&2
    return 1
  fi
  local sedargs=(-e "s|@REPO_DIR@|${REPO_DIR}|g" -e "s|@USER@|${USER_NAME}|g" -e "s|@HOME@|${HOME}|g")
  local kv
  for kv in "$@"; do
    sedargs+=(-e "s|@${kv%%=*}@|${kv#*=}|g")
  done
  sed "${sedargs[@]}" "${tpl}" | sudo tee "${dst}" >/dev/null
  echo "[install_unit] 設置しました: ${dst}"
}

# /etc/default/<name> を初回だけ設置する (既存は号機ごとの設定なので上書きしない)。
install_default() {
  local name="$1"
  local src="${SYSTEMD_DIR}/${name}.default"
  local dst="/etc/default/${name}"
  [ -f "${src}" ] || { echo "[install_default] 見つかりません: ${src}" >&2; return 1; }
  if [ -f "${dst}" ]; then
    echo "[install_default] 既存を尊重しました (上書きなし): ${dst}"
  else
    sudo cp "${src}" "${dst}"
    echo "[install_default] 設置しました: ${dst}"
  fi
}
