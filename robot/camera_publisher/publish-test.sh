#!/bin/bash
# このPC(Ubuntu)での動作確認用パブリッシャ (ラズパイ役)
#   - カメラ番号: 1=実Webカメラ(初期値), 2=合成映像(ボール)
#   - 画面取得(0)は既定で無効。検証で使うなら CAM0 を設定する
#     (例: CAM0="videotestsrc pattern=smpte" / 実画面は ximagesrc・pipewiresrc)
#
# 使い方:
#   PI_ID=PI01 ./publish-test.sh
#   PI_ID=PI02 SERVER=ws://127.0.0.1:8080/ws ./publish-test.sh   # 2台目Pi役
set -e
cd "$(dirname "$0")"

export PI_ID="${PI_ID:-PI01}"
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"
export DEFAULT_CAM="${DEFAULT_CAM:-1}"

# 1 = 実Webカメラ (MJPEG出力をデコード)
export CAM1="${CAM1:-v4l2src device=/dev/video0 ! image/jpeg,framerate=30/1 ! jpegdec}"
# 2 = 合成映像 (動くボール)
export CAM2="${CAM2:-videotestsrc is-live=true pattern=ball}"

export ENCODER="${ENCODER:-x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30}"

exec python3 publish.py
