#!/bin/bash
# Raspberry Pi 5 用パブリッシャ (SWエンコード x264enc)
#   - Pi5は専用H.264 HWエンコーダを廃止しているためソフトウェアエンコード。
#   - カメラ番号: 0=スクリーン(ximagesrc), 1=カメラ(初期値)
#
# 使い方:
#   PI_ID=PI02 SERVER=ws://<relayのIP>:8080/ws ./publish-pi5.sh
set -e
cd "$(dirname "$0")"

export PI_ID="${PI_ID:-PI01}"
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
export DEFAULT_CAM="${DEFAULT_CAM:-1}"

# --- 入力ソース ---
export CAM0="${CAM0:-ximagesrc use-damage=false}"
export CAM1="${CAM1:-libcamerasrc}"
# 例: 2台目カメラ
#   export CAM2="v4l2src device=/dev/video2 ! image/jpeg,framerate=30/1 ! jpegdec"

# --- ソフトウェアH.264エンコード (低遅延) ---
export ENCODER="${ENCODER:-x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30}"

exec python3 publish.py
