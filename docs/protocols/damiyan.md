# damiyan 契約 — control_ui ⇔ damiyan-signal-processing（外部リポジトリ）

音声解析（ダミヤン周波数の検出）を担う `damiyan-detector` は **本リポジトリに
含めない**。kkrtx 上で control_ui が子プロセスとして起動・再起動し、mic_hub から
音声を購読する。両者は本文書の契約だけで結合しているので、**片方を変えたら
必ずこの文書を更新すること**（統合できない相手との唯一の接点）。

- 実装: `damiyan-signal-processing`（別リポジトリ。既定の置き場所は
  `/home/kk/kk_ws/src/damiyan-signal-processing`）
- 呼び出し側: `server/control_ui/main.py` の
  `_write_damiyan_freq_file()` / `_restart_damiyan_detector_sync()`

## 起動コマンド

```
cd $DAMIYAN_DIR && uv run damiyan-detector \
  -f <周波数ファイル> \
  --stream http://<mic_hub>:8770/<号機> \
  --label unit<N> --web 877<N-2> --web-host 0.0.0.0
```

| 項目 | 値 | 備考 |
|---|---|---|
| 音源 | `http://127.0.0.1:8770/<号機>` | mic_hub の**単一ポート + パス**。ポートは `config/units.json` の `server.mic_hub_port` |
| 検出器がある号機 | 3 / 4 / 5 | `DAMIYAN_UNITS` |
| web ポート | 8771 / 8772 / 8773 | `8768 + 号機` |
| ラベル | `unit3` / `unit4` / `unit5` | プロセス特定にも使う |
| 実行ユーザ | `kk` | control_ui が root の場合は `runuser -u kk -- bash -lc`（`uv` の PATH を login shell で解決） |
| ログ | `$DAMIYAN_LOG_DIR/damiyan_unit<N>_reporter.log` | 追記 |

上書きできる環境変数: `DAMIYAN_DIR` / `DAMIYAN_LOG_DIR` / `MIC_HUB_HOST`。

> 旧構成では音源が `127.0.0.1:500N`（号機ごとに別ポートの mic_relay）だった。
> mic 系が単一ポート + パスへ移行した後もこの経路だけ旧契約に取り残されており、
> 統合時に修正した（D2）。**500N 系のポートはもう存在しない。**

## 周波数ファイル

control_ui（報告者モードの入力）が号機別ファイルを書き出してから検出器を再起動する。
検出器は起動時にしか読まないため、実行中の変更 API は無い。

```
$DAMIYAN_DIR/frequencies_unit<N>.json
```

```json
{
  "_source": "VideoControl reporter 入力（ルーム<部屋> → unit<N>）",
  "frequencies": [440.0, 880.0]
}
```

共有の `frequencies.json`（検出器側のフォールバック）は触らない。
ファイルは `kk:kk` に chown を試みる（失敗しても 644 で読めるので致命ではない）。

### 検証ルール（`cli.py validate_frequencies` と同じ制約）

| 制約 | 値 |
|---|---|
| 個数 | 1〜12 個 |
| 範囲 | 200〜3000 Hz |
| 最小間隔 | 40 Hz 以上離す |

規定 Appendix B.4 の上限が 3000 Hz なので、mic 系の 16 kHz サンプリング
（ナイキスト 8 kHz）で帯域は足りる（→ [mic.md](mic.md)）。

## 再起動の手順（多重起動防止）

1. `pkill -f "damiyan-detector.*--label unit<N>"`
2. 最大 5 秒待って消滅を確認（残っていれば `pkill -9`）
3. 上記コマンドで起動
4. `http://127.0.0.1:877<N-2>/` が応答するまで最大 25 秒待つ

`_damiyan_lock`（asyncio.Lock）で再起動は直列化される。

## 変えるときのチェックリスト

- 検出器の CLI（`--stream` / `--label` / `--web` / `-f`）を変えた → `main.py` とこの文書
- mic_hub のパス設計を変えた → `--stream` の組み立てとこの文書、[mic.md](mic.md)
- 周波数の制約を変えた → `main.py` の `_validate_room_frequencies()` とこの文書
