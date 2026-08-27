# legacy — 旧構成（2026-08 以前）

GStreamer と FLAC/TCP を使い、号機ごとに別ポートで配信していた頃の実装。
現行の `hub/` + `publisher/` に置き換わっており、**新規の配備には使わない**。

移行中の切り戻し用と、kkrtx 上でまだ旧 relay が動いている間の参照用に残してある。
新構成が全機で安定したら削除してよい。

| ファイル | 役割 | 置き換え先 |
|---|---|---|
| `mic-publish.sh` | 号機: `alsasrc → flacenc → tcpserversink :5005` | [`publisher/mic_publisher.py`](../publisher/mic_publisher.py) |
| `mic_relay.py` | kkrtx: 号機ごとに接続して**号機ごとの別ポート**で再配信 + 10 秒 WAV 録音。上流ホストのフェイルオーバー(`auto` = .11N > .13N > .12N > kk0N.local を TCP プローブ)つき | [`hub/mic_hub.py`](../hub/mic_hub.py) |
| `mic_receiver.py` | 受信サンプル（RMS/ピーク周波数を表示するだけ） | 実運用は damiyan-signal-processing の `--stream` |
| `README-relay.md` | 旧 relay の説明 | [`docs/protocol.md`](../docs/protocol.md) / [`docs/operations.md`](../docs/operations.md) |

旧構成の問題点と、それを新構成でどう直したかは
[ルートの README](../README.md#旧構成から何を変えたか) にまとめてある。

## 上流フェイルオーバーについて

旧 relay に後から入った「号機の IP 候補を順に TCP プローブして到達先を選ぶ」機能は、
**号機が下流から見て TCP サーバであること**に由来する問題への対処だった
（号機が `.11N`(ドングル) / `.13N`(内蔵無線) / `.12N`(有線) のどれに載っているか
中継側から分からない）。

新構成では**号機側から push する**ので、この問題は構造的に消えている。
号機が知るべきなのはハブの `192.168.10.3` だけで、これは固定。
ハブは号機の IP を一切知らなくてよいため、候補リストもプローブも要らない。

## 切り戻す場合

旧 relay は `python3-gi` と GStreamer プラグイン一式に依存する:

```bash
sudo apt install python3-gi gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools
python3 legacy/mic_relay.py --unit 3:192.168.10.133 --unit 4:192.168.10.134 --unit 5:192.168.10.135 \
    --outdir ~/kk_ws/logs/mic-recordings
```

号機側は `legacy/mic-publish.sh` を起動し、解析側は
`--stream <relay-ip>:500N`（号機ごとのポート）で接続する。
