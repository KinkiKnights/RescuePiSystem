# マイク音声の集約配信 (mic_hub + mic_publisher)

号機(Raspberry Pi)のマイク音声を **kkrtx の 1 ポートに集約** し、
**パスで号機を選んで** 誰でも購読できるようにするシステム。
録音・状態表示・ブラウザ試聴も同じプロセスが受け持つ。

```
  kk03 ─┐                                    ┌─ damiyan-detector  GET /3
        │  POST /ingest/<号機>                │
  kk04 ─┼──────────────────▶ mic_hub ────────┼─ damiyan-detector  GET /4
        │  16kHz/mono/S16LE   kkrtx:8770      │
  kk05 ─┘  (chunked HTTP)        │            └─ ブラウザ / VLC / ffmpeg  GET /5
                                 │
                                 └─▶ 号機ごとに WAV 常時録音（自動ローテート）
```

送信側は `arecord` と Python 標準ライブラリだけ、ハブは Python 標準ライブラリだけで動く。
**GStreamer も FLAC コーデックも使わない。**

## 使い方（最短）

```bash
# kkrtx（ハブ）
python3 server/mic_hub/mic_hub.py --port 8770 --outdir ~/kk_ws/logs/mic-recordings

# 各号機（送信）
python3 robot/mic_publisher/mic_publisher.py --hub http://192.168.10.3:8770 --unit 5 --device hw:1,0

# 購読（号機はパスで選ぶ）
ffplay http://192.168.10.3:8770/5                      # 試し聴き
curl http://192.168.10.3:8770/5 -o rec.wav             # 録る
uv run damiyan-detector --stream http://192.168.10.3:8770/5 -f frequencies.json
```

ブラウザで `http://192.168.10.3:8770/` を開くと、全号機の接続状態・レベルメータ・
録音中ファイル名が出て、その場で試聴できる。

常時稼働させる手順（systemd への登録、master_control からの起動）は
[`docs/operations.md`](operations.md) を参照。

## エンドポイント

| メソッド・パス | 用途 |
|---|---|
| `GET /<号機>` | ストリーミング WAV（`/5`、`/5.wav`）。**購読はこれだけ覚えればよい** |
| `GET /listen/<号機>` | 上と同じ（明示形） |
| `GET /listen/<号機>?format=raw` | ヘッダ無しの 16kHz/mono/S16LE。ブラウザ試聴などで使う |
| `GET /api/status` | 全号機の状態 JSON |
| `GET /healthz` | 死活確認 |
| `GET /` | 状態表示 UI（試聴つき） |
| `POST /ingest/<号機>` | 送信側専用。chunked で生 PCM を流し込む |

号機 ID は `[A-Za-z0-9_-]{1,32}`。`5` でも `kk05` でもよく、
ハブ側に事前登録は不要（最初の push で自動的に現れる）。
ワイヤ契約の詳細は [`docs/protocol.md`](protocols/mic.md)。

## 旧構成から何を変えたか

| | 旧 (〜2026-08) | 新 |
|---|---|---|
| 号機の選択 | **号機ごとに別ポート**（5003/5004/5005） | **1 ポート + パス**（`/3` `/4` `/5`） |
| 接続の向き | 号機が TCP サーバ、下流が繋ぎに行く | **号機が push**、ハブは待つだけ |
| コーデック | 48kHz FLAC（約 437 kbps） | **16kHz 生 PCM（256 kbps）** |
| 送信側の依存 | GStreamer 一式（`gst-launch`, plugins-base/good） | `arecord` + numpy |
| 中継側の依存 | GStreamer + `python3-gi` | **標準ライブラリのみ** |
| 起動 | `mic_relay.py` を手動 nohup（再起動で消える） | systemd unit を同梱 |
| 号機の所在 | 中継側が `.11N`/`.13N`/`.12N` を順に TCP プローブ | **不要**（号機がハブへ繋ぎに行く） |
| 状態の可視化 | 無し（ログを読む） | `/api/status` と Web UI |
| 録音 | 10 秒 WAV が無制限に増える | セグメント + 期間/容量で自動削除 |

### なぜ 16 kHz の生 PCM なのか

解析側 `damiyan-detector` は最終的に 16 kHz へ落として処理する。
そこまでの経路を最初から 16 kHz に揃えると **16000×2 = 256 kbps** で、
旧構成の 48 kHz FLAC（実測 437 kbps）**より軽い**。
帯域が下がるうえに圧縮が不要になるので、送受信双方から
コーデック依存（GStreamer / FLAC）を丸ごと外せる。

規定 Appendix B.4 のダミヤン周波数は上限 3000 Hz なので、
16 kHz（ナイキスト 8 kHz）で帯域は十分足りる。
48 kHz→16 kHz の間引きは publisher 側でアンチエイリアス FIR を通してから行う
（`arecord -r 16000` の ALSA 自動変換は線形補間になる場合があり、
帯域外成分が可聴帯へ折り返して周波数判定を汚す）。

**トレードオフ**: 8 kHz より上を使う解析は将来にわたってできなくなる。
必要になったら `mic_hub.py` / `mic_publisher.py` の `SAMPLE_RATE` / `OUT_RATE` を
48000 に上げれば経路はそのまま動く（帯域は 768 kbps になる）。

## 構成

| パス | 中身 |
|---|---|
| [`server/mic_hub/mic_hub.py`](../server/mic_hub/mic_hub.py) | 集約ハブ。取り込み・再配信・録音・状態 API・UI |
| [`server/mic_hub/static/index.html`](../server/mic_hub/static/index.html) | 状態表示とブラウザ試聴（外部依存なし） |
| [`robot/mic_publisher/mic_publisher.py`](../robot/mic_publisher/mic_publisher.py) | 号機側の送信。arecord → 16kHz → push |
| [`deploy/systemd/`](../deploy/systemd/) | 常駐用の unit と `/etc/default` テンプレート |
| [`tools/mic_selftest.py`](../tools/mic_selftest.py) | 実機なしで系全体を検証するスモークテスト |
| [`docs/protocol.md`](protocols/mic.md) | ワイヤ契約（他言語で実装し直す場合はここだけ読めばよい） |
| [`docs/operations.md`](operations.md) | 配備・運用・トラブルシュート |

## テスト

```bash
python3 tools/mic_selftest.py
```

ハブと 3 台ぶんの模擬号機（別々の周波数）を立てて、パスによる号機選択・
WAV ヘッダ・複数購読・異常系（503 / 404 / 二重 publisher の 409）・録音までを
一通り検証する。マイクも numpy も要らない。

## 必要なもの

- **ハブ (kkrtx)**: Python 3.9 以上。追加パッケージ無し。
- **号機 (Pi)**: Python 3.9 以上、`alsa-utils`（`arecord`）、`python3-numpy`。
  numpy を入れられない機体は `--capture-rate 16000` で回避できる（音質は落ちる）。
