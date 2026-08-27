# 配備と運用

対象は Res26 フリート（号機 kk01〜kk05 = Raspberry Pi 5 / Ubuntu 24.04、
kkrtx = x86 Ubuntu、ハブ兼アプリホスト、`192.168.10.3`）。

## 1. ハブ（kkrtx）

```bash
cd ~/kk_ws/src && git clone git@github.com:KinkiKnights/MicStreamRes2026.git
cd MicStreamRes2026

# 手で動かして確認
python3 hub/mic_hub.py --port 8770 --outdir ~/kk_ws/logs/mic-recordings

# 常駐化
sudo cp systemd/mic-hub.service /etc/systemd/system/
sudo cp systemd/mic-hub.default /etc/default/mic-hub
sudo systemctl daemon-reload
sudo systemctl enable --now mic-hub
systemctl status mic-hub
```

追加パッケージは不要（Python 標準ライブラリのみ）。

設定は `/etc/default/mic-hub` に置く。リポジトリ内の設定ファイルに書くと
`git pull` で機体ごとの差分が巻き戻る（`programs.json` で実際に起きている）。

旧 `relay/mic_relay.py` は all_start.bash 未登録の手動 nohup 起動で、
kkrtx を再起動するたびに消えていた。`systemctl enable` しておけば勝手に戻る。

## 2. 号機（Pi）

```bash
# 録音デバイスの確認（USB マイクは通常 hw:1,0）
arecord -l

# kk が audio グループに入っていないと録音デバイスが見えない
sudo usermod -aG audio kk        # 反映には再ログイン

cd ~/kk_ws/src && git clone git@github.com:KinkiKnights/MicStreamRes2026.git
sudo apt install -y alsa-utils python3-numpy

# 手で動かして確認
python3 ~/kk_ws/src/MicStreamRes2026/publisher/mic_publisher.py \
    --hub http://192.168.10.3:8770 --unit 5 --device hw:1,0
```

常駐のさせ方は 2 通りある。**どちらか一方だけにすること**
（両方動かすと二重起動になり、後発が `409` で弾かれ続ける）。

### (a) systemd（推奨）

```bash
sudo cp systemd/mic-publisher.service /etc/systemd/system/
sudo cp systemd/mic-publisher.default /etc/default/mic-publisher
sudo editor /etc/default/mic-publisher      # MIC_UNIT と MIC_DEVICE を機体に合わせる
sudo systemctl daemon-reload
sudo systemctl enable --now mic-publisher
journalctl -u mic-publisher -f
```

### (b) master_control（既存の Web UI から起動する場合）

`programs.json` の `mic` エントリを差し替える:

```json
{"id": 3, "name": "mic", "type": "bash", "autostart": true,
 "cmd": "python3 /home/kk/kk_ws/src/MicStreamRes2026/publisher/mic_publisher.py --hub http://192.168.10.3:8770 --unit 5 --device hw:1,0"}
```

`programs.json` は tracked なので `git pull` で巻き戻るリスクがある。
巻き戻ったら mic が旧 GStreamer 版のコマンドに戻るため、更新後は必ず確認する。

### 号機ごとの設定

| 号機 | `MIC_UNIT` | `MIC_DEVICE` | 備考 |
|---|---|---|---|
| kk03 | `3` | `hw:1,0` | H264 USB Camera 内蔵マイクに capture がある |
| kk04 | `4` | — | HD USB Camera に音声デバイスが無い。**マイクを繋ぐまで起動しない** |
| kk05 | `5` | `hw:Device,0` | 外付け USB マイク（GeneralPlus）。Alcor カメラ側には capture PCM が無い |

`MIC_DEVICE` は `arecord -l` の出力で必ず確認する。
カード名で指定（`hw:Device,0`）しておくと、USB の挿し順で番号が変わっても追従する。

## 3. 購読側

```bash
# ダミヤン検出（damiyan-signal-processing）
uv run damiyan-detector --stream http://192.168.10.3:8770/5 -f frequencies.json

# 聴く / 録る
ffplay -nodisp http://192.168.10.3:8770/5
vlc http://192.168.10.3:8770/5
curl http://192.168.10.3:8770/5 -o cap.wav      # Ctrl-C で止める
```

ブラウザなら `http://192.168.10.3:8770/` を開けば一覧から試聴できる。

## 4. 動作確認

```bash
# ハブが生きているか
curl -s http://192.168.10.3:8770/healthz

# どの号機が繋がっているか（level_dbfs で音が来ているかも分かる）
curl -s http://192.168.10.3:8770/api/status | python3 -m json.tool

# マイクなしで系全体を検証（ハブ + 模擬号機 3 台を立てて自動判定）
python3 tools/selftest.py
```

## トラブルシュート

| 症状 | 見るところ |
|---|---|
| 号機が offline のまま | 号機側で `journalctl -u mic-publisher -f`。`arecord` のエラーがそのままログに出る |
| `arecord: main:830: audio open error: No such file or directory` | `MIC_DEVICE` が違う。`arecord -l` で確認 |
| `arecord` がデバイスを 1 つも見つけない | `kk` が `audio` グループに居ない。`sudo usermod -aG audio kk` して再ログイン |
| `409 Conflict` がログに繰り返し出る | 二重起動。systemd と master_control の両方から起動していないか確認 |
| `rate mismatch` で `400` | 送信側とハブの `SAMPLE_RATE` 不一致。両方を揃える |
| 購読すると `503` | その号機の送信が繋がっていない。`/api/status` で確認 |
| 音は来るが `level_dbfs` が -120 のまま | マイクがミュート/ゲイン 0。号機で `alsamixer -c 1` |
| `listener ... too slow — dropped` | 購読側が 6.4 秒ぶん詰まった。購読側の CPU かネットワークを疑う |
| `audio/wall drift` の WARNING | 号機の CPU 不足か ALSA の overrun。`MIC_CAPTURE_RATE=16000` を試す |
| 録音でディスクが埋まる | `/etc/default/mic-hub` の `MIC_HUB_RETENTION_HOURS` と `MIC_HUB_MAX_GB` を下げる |
| kkrtx 再起動でハブが消えた | `systemctl is-enabled mic-hub` を確認。enable し忘れ |

### numpy を入れられない号機

`MIC_CAPTURE_RATE=16000` にすると ALSA 側で 16 kHz に変換され、numpy は不要になる。
ただし ALSA のリサンプラはビルドによって線形補間で、16 kHz 超の成分が
可聴帯へ折り返す。ダミヤンの周波数判定の精度が落ちるので、あくまで応急処置。

## ネットワーク

号機は `192.168.10.13N`（内蔵 wlan0）または `.11N`（USB ドングル）で、
どちらに載っているかは状況で変わる。
**この構成では号機側から push するので、ハブは号機の IP を知らなくてよい。**
号機が把握すべきなのはハブの `192.168.10.3` だけで、これは固定。

kkrtx でポートを開ける必要がある場合:

```bash
sudo ufw allow 8770/tcp        # ufw を使っている場合のみ
```
