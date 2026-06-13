#!/bin/bash
# Raspberry Pi 4 用パブリッシャ (HWエンコード v4l2h264enc)
#   - カメラ番号: 0=スクリーン(ximagesrc), 1=カメラ(初期値)
#   - 複数カメラがある場合は CAM2/CAM3... を追加すると camChange で切替可能
#
# 使い方:
#   PI_ID=PI01 SERVER=ws://<relayのIP>:8080/ws ./publish-pi4.sh
set -e
cd "$(dirname "$0")"

export PI_ID="${PI_ID:-PI01}"
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
export DEFAULT_CAM="${DEFAULT_CAM:-1}"

# --- 入力ソース ---
# 0 = スクリーン (X11環境はximagesrc。Wayland環境は pipewiresrc 等に変更)
export CAM0="${CAM0:-ximagesrc use-damage=false}"
# 1 = カメラ (CSI: libcamerasrc / USB: v4l2src)。MJPEG出力カメラは ! image/jpeg ! jpegdec を付与
export CAM1="${CAM1:-libcamerasrc}"
# 例: 2台目カメラ
#   export CAM2="v4l2src device=/dev/video2 ! image/jpeg,framerate=30/1 ! jpegdec"

# --- ハードウェアH.264エンコード ---
export ENCODER="${ENCODER:-v4l2h264enc extra-controls=\"controls,video_bitrate=2500000,h264_i_frame_period=30\"}"

exec python3 publish.py
