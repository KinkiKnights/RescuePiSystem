#!/bin/bash
# Raspberry Pi 4 用パブリッシャ
#   - H.264 ハードウェアエンコード (v4l2h264enc) を使用 → CPUほぼ不使用
#   - CSIカメラ(libcamera)を想定。USBカメラの場合は SOURCE を v4l2src に変更。
#
# 使い方:
#   SERVER=ws://<relayのIP>:8080/ws ./publish-pi4.sh
set -e
cd "$(dirname "$0")"

# relayサーバーのアドレス (環境変数で上書き可)
export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"

# --- カメラ入力 ---
# CSIカメラ (Pi Camera Module): libcamerasrc
export SOURCE="${SOURCE:-libcamerasrc ! video/x-raw,width=1280,height=720,framerate=30/1,format=NV12}"
# USBカメラの例:
#   export SOURCE="v4l2src device=/dev/video0 ! video/x-raw,width=1280,height=720,framerate=30/1"

# --- ハードウェアH.264エンコード ---
# v4l2h264enc: VideoCoreのHWエンコーダ。低遅延設定でI/Pのみ。
export ENCODER="${ENCODER:-v4l2h264enc extra-controls=\"controls,video_bitrate=2500000,h264_i_frame_period=30\"}"

exec python3 publish.py
