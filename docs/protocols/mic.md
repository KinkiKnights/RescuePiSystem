# ワイヤ契約

送信側・ハブ・購読側が守る約束。ここに書いてあることだけ守れば、
どの実装（別言語・既製ツール）でも相互運用できる。

## 共通の音声フォーマット

| 項目 | 値 |
|---|---|
| サンプリングレート | **16000 Hz** |
| チャンネル | **1（mono）** |
| サンプル形式 | **S16LE**（符号付き 16bit リトルエンディアン） |
| ビットレート | 256 kbps（32000 バイト/秒） |

経路上どこでも圧縮しない。ハブは受け取ったバイト列をそのまま配り、
そのまま WAV に書く（購読者が得る PCM は号機の `arecord` 出力とビット一致する）。

レートを変える場合は `hub/mic_hub.py` の `SAMPLE_RATE` と
`publisher/mic_publisher.py` の `OUT_RATE` を**両方**変えること。
不一致のまま接続すると、ハブが `X-Mic-Rate` を見て `400` で弾く。

## 号機 ID

`[A-Za-z0-9_-]{1,32}`。URL パスにそのまま載る。
ハブへの事前登録は不要で、最初の `POST /ingest/<号機>` で自動的に現れる。
`--unit` で事前宣言しておくと、まだ繋がっていない号機も UI に offline として並ぶ。

## 送信: `POST /ingest/<号機>`

```http
POST /ingest/5 HTTP/1.1
Host: 192.168.10.3:8770
Transfer-Encoding: chunked
Content-Type: audio/L16; rate=16000; channels=1
X-Mic-Protocol: 1
X-Mic-Rate: 16000
X-Mic-Channels: 1
X-Mic-Format: S16LE
X-Mic-Source: kk05:arecord hw:1,0 @48000
```

- 本文は生の S16LE を延々と流し続ける。1 チャンク 100 ms（3200 バイト）が推奨。
- `Transfer-Encoding: chunked` が標準。`Content-Length` 指定と
  「EOF まで」の 2 形式もハブは受け付ける（`curl` や `ffmpeg` から手で流す用）。
- `X-Mic-Rate` はハブの期待値と一致しなければならない。不一致なら即 `400`。
- 応答はストリーム終了時に `200`。異常時は下記のとおり。

### 1 号機に 2 つの送信が来たとき

**現職が生きていれば新参を拒否する（`409 Conflict`）。**
「後勝ち」にすると二重起動した送信同士が互いを蹴り合って音が途切れ続けるため。

現職が `INGEST_STALE_SEC`（3 秒）データを送っていなければ、新参が奪取する。
Wi-Fi 断でハブ側にだけ残ったゾンビ接続はデータを送らないので、この経路で置き換わる
（取りこぼしても読み取りタイムアウト 10 秒で刈られる）。

送信側は `409` を受けたら 2→3→5→15 秒と間隔を伸ばして再試行する。
最初の数回を短くしてあるのは、上記のゾンビ奪取を待つため。

| 状況 | ハブの応答 |
|---|---|
| 正常終了 | `200` |
| レート不一致 | `400`（本文に期待値） |
| 号機 ID が不正 | `400` |
| 現職が送信中 | `409` |
| パスが `/ingest/` 以外 | `404` |

## 購読: `GET /<号機>`

```http
GET /5 HTTP/1.1
```

```http
HTTP/1.1 200 OK
Content-Type: audio/wav
X-Mic-Protocol: 1
X-Mic-Rate: 16000
X-Mic-Channels: 1
X-Mic-Format: S16LE
Connection: close
```

- 本文は **44 バイトの WAV ヘッダ + 生 PCM**。ヘッダの RIFF/data サイズ欄は
  `0xFFFFFFFF`（長さ未定）。ffmpeg・VLC・ブラウザはこれを「終わりの分からない
  ストリーム」として扱い EOF まで読み続ける。
- `GET /listen/5`、`GET /5.wav` も同じ。
- `?format=raw` を付けるとヘッダ無しの生 S16LE（`Content-Type: audio/L16`）。
- 途中参加できる。同じ号機に何本でも同時接続できる。
- 号機がオフラインなら `503`。**無音を配って生きているふりはしない**ので、
  購読側の再接続ループがそのまま復帰処理として機能する。
- 上流が切れると、購読側にはストリームの EOF として伝わる。

### 遅い購読者の扱い

購読者ごとに 64 チャンク（約 6.4 秒）のキューを持つ。
溢れたらその購読者だけを切断する。**取り込みと録音は決して止めない。**

## 状態: `GET /api/status`

```json
{
  "protocol": "1",
  "rate": 16000, "channels": 1, "format": "S16LE",
  "started_at": "2026-08-24T00:00:00+00:00",
  "uptime_sec": 3600.0,
  "recording": true,
  "outdir": "/home/kk/kk_ws/logs/mic-recordings",
  "units": [
    {
      "unit": "5",
      "online": true,
      "publisher": "192.168.10.135:51234",
      "source": "kk05:arecord hw:1,0 @48000",
      "connected_at": "2026-08-24T00:00:05+00:00",
      "uptime_sec": 3594.0,
      "last_data_age_sec": 0.05,
      "level_dbfs": -32.4,
      "listeners": 2,
      "session_bytes": 115008000,
      "total_bytes": 115008000,
      "disconnects": 0,
      "recording": "unit5_20260824-010000.wav"
    }
  ]
}
```

`last_data_age_sec` が 1 秒を超えていれば取り込みが詰まっている
（UI では stalled 表示）。`disconnects` は上流が切れた回数。

## 録音

`--outdir/unit<号機>/unit<号機>_<YYYYmmdd-HHMMSS>.wav`
（16000 Hz / mono / S16LE）。`--segment` 秒ごとにファイルを切り替え、
切り替えのたびに `--retention-hours` より古いものと、
`--max-gb` を超えたぶんの古いものを消す。
購読者の有無に関係なく、上流が繋がっている間はずっと録る。

16 kHz mono は **115 MB/時**（旧 48 kHz の 1/3）。

## バージョン

`X-Mic-Protocol: 1`。互換性を壊す変更を入れる場合はこの値を上げ、
ハブ側で古い送信を明示的に拒否すること。
