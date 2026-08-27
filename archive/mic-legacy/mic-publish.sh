#!/bin/bash
# =============================================================================
#  mic-publish.sh  — USBマイク音声を「ロスレス(FLAC)」で TCP 配信する（送信側=Pi）
# -----------------------------------------------------------------------------
#  方式: alsasrc(マイク) → FLAC可逆エンコード → TCPサーバで配信
#    - FLAC は可逆圧縮。受信側で元の PCM(S16LE) がビット完全に復元される。
#    - 帯域は生PCMの約 50〜60%（48kHz/mono/16bit の生 768kbps → 約 400kbps 前後）。
#    - TCP なので全サンプルが確実に届く（UDP/RTPと違い欠落でサンプルが飛ばない）。
#      → 多少の遅延は許容、というロスレス解析の要件に最適。
#    - この Pi が TCP サーバ。受信側(解析PC)が接続しに来る。複数受信も可。
#
#  使い方:
#    ./mic-publish.sh                          # 既定 hw:1,0 / 48000Hz / port5005
#    ALSA_DEV=hw:1,0 RATE=48000 PORT=5005 ./mic-publish.sh
#
#  環境変数:
#    ALSA_DEV : 録音デバイス（arecord -l で確認。USB Audio Device は通常 hw:1,0）
#    RATE     : サンプリングレート（このマイクのネイティブは 48000 または 44100）
#    PORT     : 待受TCPポート
#    COMP     : FLAC圧縮レベル 0..8（既定5。負荷を下げたいなら小さく、帯域を絞るなら大きく）
# =============================================================================
set -euo pipefail

ALSA_DEV="${ALSA_DEV:-hw:1,0}"
RATE="${RATE:-48000}"
PORT="${PORT:-5005}"
COMP="${COMP:-5}"

echo "[mic-publish] device=$ALSA_DEV rate=$RATE port=$PORT flac-comp=$COMP"
echo "[mic-publish] 受信側は tcp://<このPiのIP>:$PORT へ接続してください"

# audioconvert は念のため（alsasrcが既に S16LE/mono を出すのでリサンプルは発生しない＝可逆）
exec gst-launch-1.0 -e \
  alsasrc device="$ALSA_DEV" ! \
  audio/x-raw,format=S16LE,rate="$RATE",channels=1 ! \
  audioconvert ! \
  flacenc quality="$COMP" streamable-subset=true ! \
  flacparse ! \
  tcpserversink host=0.0.0.0 port="$PORT" sync=false
