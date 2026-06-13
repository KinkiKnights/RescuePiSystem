#!/bin/bash
# このPC(Ubuntu)での動作確認用パブリッシャ (ラズパイのカメラ役)
#   - 実Webカメラ(/dev/video0)を入力、x264enc(SW)でエンコード。
#   - カメラが無い/使えない場合は USE_TEST=1 で videotestsrc に切替。
#
# 使い方:
#   ./publish-test.sh                 # 実Webカメラ
#   USE_TEST=1 ./publish-test.sh      # 合成映像(videotestsrc)
set -e
cd "$(dirname "$0")"

export SERVER="${SERVER:-ws://127.0.0.1:8080/ws}"

if [ "${USE_TEST:-0}" = "1" ]; then
  export SOURCE="${SOURCE:-videotestsrc is-live=true pattern=ball ! video/x-raw,width=1280,height=720,framerate=30/1}"
else
  # 実Webカメラ。多くのUVCカメラはMJPEG/YUYVを出すので decodebin で吸収。
  export SOURCE="${SOURCE:-v4l2src device=/dev/video0 ! image/jpeg,framerate=30/1 ! jpegdec}"
fi

export ENCODER="${ENCODER:-x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30}"

exec python3 publish.py
