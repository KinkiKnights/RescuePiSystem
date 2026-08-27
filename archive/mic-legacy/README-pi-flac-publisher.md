# mic_publisher

USB マイクの音声を FLAC ロスレス圧縮して TCP 配信する publisher。

## 出自と対向コンポーネント

- 移設元: [KinkiKnights/MicStreamRes2026](https://github.com/KinkiKnights/MicStreamRes2026) の `publisher/` @ `d565fa9`
- 対向(受信側 PC): `mic_receiver.py` は引き続き MicStreamRes2026 にあります。

## プロトコル契約(receiver との互換性)

配信フォーマットは **FLAC over TCP**(GStreamer `flacenc ! tcpserversink`)。
受信側は GStreamer → numpy で解析します。
**エンコード形式・サンプルレート・フレーミングを変更する場合は、
MicStreamRes2026 の receiver も必ず同時に更新してください。**

## 起動

master_control の programs.json から起動されます:

```bash
ALSA_DEV=hw:1,0 RATE=48000 PORT=5005 ./mic-publish.sh
```

受信例: `tcp://<PiのIP>:5005` に接続。
