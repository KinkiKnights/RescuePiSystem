#!/bin/bash
# Raspberry Pi 5 用パブリッシャ
#   - Pi5は専用H.264ハードウェアエンコーダを廃止しているため、ソフトウェア
#     エンコード(x264enc)を使用。CPU(Cortex-A76)が高速なので720p/30は実用的。
#   - 負荷が高い場合は解像度/フレームレート/bitrateを下げる。
#
# 使い方:
#   SERVER=ws://<relayのIP>:8080/ws ./publish-pi5.sh
set -e
cd "$(dirname "$0")"

export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"

# --- カメラ入力 ---
export SOURCE="${SOURCE:-libcamerasrc ! video/x-raw,width=1280,height=720,framerate=30/1,format=NV12}"
# USBカメラの例:
#   export SOURCE="v4l2src device=/dev/video0 ! video/x-raw,width=1280,height=720,framerate=30/1"

# --- ソフトウェアH.264エンコード (低遅延) ---
# ultrafast + zerolatency でBフレーム無し・低遅延。CPU負荷に応じて調整。
export ENCODER="${ENCODER:-x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30}"

exec python3 publish.py
