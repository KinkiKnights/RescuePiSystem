# mic_relay.py — 中継 & 常時録音サーバ

号機(Pi)群が FLAC/TCP で配信する音声を受け取り、**号機ごとに別ポートで再配信**しつつ
**号機ごとに10秒単位の WAV を常時録音**する単一プロセスの中継サーバです。

既存の `publisher/mic-publish.sh`（送信=Pi）と `receiver/mic_receiver.py`（解析PC）は
**一切改変せずにそのまま利用できます**。解析PCは接続先ポートを変えるだけで号機を選択します。

## なぜポートで号機を選ぶのか

- 送信側(`mic-publish.sh`)は `tcpserversink` = **TCPサーバ**。1号機が1本の FLAC/TCP を配信し、複数の接続を受け付ける。
- 受信側(`mic_receiver.py`)は `tcpclientsrc` = **TCPクライアント**で、選択手段は `--host` / `--port` のみ（帯域内の号機選択プロトコルは無い）。
- そこで中継サーバが**号機ごとに別々の下流ポート**で再配信する。解析PCは `--port` を変えるだけで号機を選べる。

## アーキテクチャ

```
  号機3(Pi) tcpserversink:5005 ─┐
  号機4(Pi) tcpserversink:5005 ─┼─(中継サーバがクライアントとして接続)
  号機5(Pi) tcpserversink:5005 ─┘

  ┌───────────────────────── mic_relay.py (1プロセス / 1 GLib mainループ) ─────────────────────────┐
  │ 号機ごと:                                                                                        │
  │   tcpclientsrc(号機) → flacparse → tee ┬→ queue → tcpserversink :500N   (解析PCへ再配信)          │
  │                                        └→ queue → flacdec → audioconvert → S16LE → appsink(録音) │
  │                                                                     └→ 10秒ごとに WAV(標準wave)   │
  └───────────────────────────────────────────────────────────────────────────────────────────────┘

  下流(解析PC向け):  号機3 → :5003    号機4 → :5004    号機5 → :5005
```

- `flacparse` の出力には **streamheader** が付くため、`tcpserversink` は途中参加(late-join)を含む
  各クライアントへ先頭を正しく配信する。よって未改変の受信側が号機直結時と同一にデコードできる。
- FLAC は可逆圧縮。`appsink` で得た S16LE は元マイクの PCM とビット完全一致で、それをそのまま
  `wave`(Python標準ライブラリ)で WAV 化する（追加依存なし）。

## 号機 → 下流ポート 対応（既定）

| 号機 | 上流(号機)         | 下流(再配信) | 録音ファイル                       |
|------|--------------------|--------------|------------------------------------|
| 3    | `<pi3>:5005`       | `:5003`      | `rec_unit3_<YYYYmmdd-HHMMSS>.wav`  |
| 4    | `<pi4>:5005`       | `:5004`      | `rec_unit4_<YYYYmmdd-HHMMSS>.wav`  |
| 5    | `<pi5>:5005`       | `:5005`      | `rec_unit5_<YYYYmmdd-HHMMSS>.wav`  |

## インストール(Ubuntu / apt標準パッケージのみ)

```bash
sudo apt install python3-gi python3-numpy \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools
```

（中継サーバ自体は numpy 不要。numpy は解析側 `mic_receiver.py` が使用）

## 実行方法（本番: 3台の実機Piに対して）

各号機のホスト名/IP を指定して起動します（号機の配信ポートは既定 5005）:

```bash
python3 relay/mic_relay.py \
    --unit 3:pi3.local:5005:5003 \
    --unit 4:pi4.local:5005:5004 \
    --unit 5:pi5.local:5005:5005 \
    --rate 48000 \
    --outdir ./recordings \
    --segment 10
```

`--unit` の書式は `N:pub_host[:pub_port][:down_port]`（`pub_port` 省略時は `--pub-port`、
`down_port` 省略時は `500N`）。号機の配信ポートが全台 5005 なら簡潔に:

```bash
python3 relay/mic_relay.py \
    --unit 3:pi3.local --unit 4:pi4.local --unit 5:pi5.local \
    --outdir ./recordings
```

主なオプション:

| オプション      | 既定         | 説明                               |
|-----------------|--------------|------------------------------------|
| `--unit`        | 3/4/5        | 号機定義。複数指定可               |
| `--pub-host`    | `127.0.0.1`  | `--unit`省略時の上流ホスト          |
| `--pub-port`    | `5005`       | 上流ポート既定値                   |
| `--rate`        | `48000`      | サンプリングレート(号機と一致させる)|
| `--outdir`      | `./recordings` | 録音WAVの出力先                  |
| `--segment`     | `10`         | 1WAVあたりの秒数                   |
| `--reconnect`   | `2`          | 上流切断時の再接続待ち秒数         |

`Ctrl-C`(SIGINT) / SIGTERM でクリーン終了します。

## 解析プログラム(未改変)の接続方法

`mic_receiver.py` は改変不要。中継サーバのIPと、解析したい号機のポートを指定するだけ:

```bash
# 号機3を解析
python3 receiver/mic_receiver.py --host <relay-ip> --port 5003 --rate 48000
# 号機4を解析
python3 receiver/mic_receiver.py --host <relay-ip> --port 5004 --rate 48000
# 号機5を解析
python3 receiver/mic_receiver.py --host <relay-ip> --port 5005 --rate 48000
```

同一ポートに複数の解析PCを同時接続でき（1号機を複数解析）、途中参加も可能です。

## 録音の保存先

`--outdir`（既定 `./recordings/`）に、号機ごと10秒単位で
`rec_unit<N>_<YYYYmmdd-HHMMSS>.wav`（48000Hz / mono / S16LE）が連続生成されます。
録音は**解析PCの接続有無に関係なく常時**動作します。

## 耐障害性

ある号機の上流接続が切れても、その号機のパイプラインだけを畳んで `--reconnect` 秒ごとに
自動再接続します。他号機の再配信・録音とプロセス全体には影響しません。ログに
`[unitN] ... reconnecting` / `[unitN] connecting upstream ...` が出ます。

## 動作確認（実機なしでローカル模擬）

3号機を `gst-launch` の tcpserversink で模擬し（号機ごとに別周波数）、中継→未改変受信→録音を検証できます。

```bash
# 3台の模擬パブリッシャ（unit3=660Hz, unit4=880Hz, unit5=1100Hz / ポート6003-6005）
for N in 3 4 5; do
  gst-launch-1.0 audiotestsrc is-live=true freq=$((N*220)) ! audioconvert ! \
    audio/x-raw,format=S16LE,channels=1,rate=48000 ! flacenc ! flacparse ! \
    tcpserversink host=0.0.0.0 port=$((6000+N)) sync=false &
done

# 中継サーバ（上流6003-6005 → 下流5003-5005）
python3 relay/mic_relay.py \
  --unit 3:127.0.0.1:6003:5003 \
  --unit 4:127.0.0.1:6004:5004 \
  --unit 5:127.0.0.1:6005:5005 \
  --rate 48000 --outdir /tmp/rec --segment 10

# 別端末: 未改変の解析プログラムでポート選択を確認（:5004なら880Hzが出るはず）
python3 receiver/mic_receiver.py --host 127.0.0.1 --port 5004 --rate 48000
```
