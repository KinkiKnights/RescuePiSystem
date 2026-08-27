#!/bin/bash
# Raspberry Pi 4 用パブリッシャ (HWエンコード v4l2h264enc)
#   - カメラ番号: 1=カメラ(初期値)。複数カメラは CAM2/CAM3... を追加すると camChangeで切替可能
#   - 画面取得(0)は既定で無効。必要なら CAM0 を設定して有効化する。
#
# 使い方:
#   PI_ID=KK01 SERVER=ws://<relayのIP>:8080/ws ./publish-pi4.sh
set -e
cd "$(dirname "$0")"

# 号機 ID は KK0N 形式に統一 (config/units.json の pi_id と一致させる)。
# 既定はホスト名の大文字化 (kk05 -> KK05)。操作画面は "KK"+号機番号2桁 を購読する。
export PI_ID="${PI_ID:-$(hostname | cut -d. -f1 | tr '[:lower:]' '[:upper:]')}"
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
export DEFAULT_CAM="${DEFAULT_CAM:-1}"

# --- 入力ソース ---
# 1 = カメラ (CSI: libcamerasrc / USB: v4l2src)。MJPEG出力カメラは ! image/jpeg ! jpegdec を付与
export CAM1="${CAM1:-libcamerasrc}"
# 画面取得を使う場合のみ有効化 (X11はximagesrc / Waylandはpipewiresrc):
#   export CAM0="ximagesrc use-damage=false"
# 例: 2台目カメラ
#   export CAM2="v4l2src device=/dev/video2 ! image/jpeg,framerate=30/1 ! jpegdec"

# --- ハードウェアH.264エンコード ---
export ENCODER="${ENCODER:-v4l2h264enc extra-controls=\"controls,video_bitrate=2500000,h264_i_frame_period=30\"}"

exec python3 publish.py
