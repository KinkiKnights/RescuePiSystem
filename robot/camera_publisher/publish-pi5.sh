#!/bin/bash
# Raspberry Pi 5 用パブリッシャ (SWエンコード x264enc)
#   - Pi5は専用H.264 HWエンコーダを廃止しているためソフトウェアエンコード。
#   - カメラ番号: 1=カメラ(初期値)。画面取得(0)は既定で無効(CAM0設定で有効化)。
#
# 使い方:
#   PI_ID=KK02 SERVER=ws://<relayのIP>:8080/ws ./publish-pi5.sh
set -e
cd "$(dirname "$0")"

# 号機 ID は KK0N 形式に統一 (config/units.json の pi_id と一致させる)。
# 既定はホスト名の大文字化 (kk05 -> KK05)。操作画面は "KK"+号機番号2桁 を購読する。
export PI_ID="${PI_ID:-$(hostname | cut -d. -f1 | tr '[:lower:]' '[:upper:]')}"
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
export DEFAULT_CAM="${DEFAULT_CAM:-1}"

# --- 入力ソース ---
export CAM1="${CAM1:-v4l2src device=/dev/video0 ! image/jpeg,framerate=30/1 ! jpegdec}"
# 画面取得を使う場合のみ有効化 (X11はximagesrc / Waylandはpipewiresrc):
#   export CAM0="ximagesrc use-damage=false"
# 例: 2台目カメラ
#   export CAM2="v4l2src device=/dev/video2 ! image/jpeg,framerate=30/1 ! jpegdec"

# --- ソフトウェアH.264エンコード (低遅延) ---
export ENCODER="${ENCODER:-x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30}"

exec python3 publish.py
