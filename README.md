# レスキューロボコン ダミー操作画面

レスキューロボットコンテストの各種オペレーター画面を再現した**動画撮影・デモ用のダミーアプリ**です。
複数の端末（PC・スマホ）から同時に開き、WebSocket で状態を共有しながら 5 種類の画面を操作できます。

> 実機制御は行いません。撮影や説明のために「本物らしく動く」ことを目的としたモックです。

---

## 5 つのモード

| ルート | モード | 想定端末 | 役割 |
|---|---|---|---|
| `/control` | コントロールモード | PC（横） | 操縦担当。機体カメラ・操作ボタン・タスク進行 |
| `/analytics` | アナリティクスモード | PC（横） | 解析担当。映像監視＋部屋ごとの解析結果入力 |
| `/engineer` | エンジニアモード | PC（横） | 監視エンジニア。通信状態・機体メニュー操作 |
| `/reporter` | 報告者モード | スマホ（iPhone 11 縦・PWA） | 報告担当。全部屋の状態確認と報告記録 |
| `/master` | マスターモード | PC/タブレット | 撮影進行。各画面へ情報投入・全体リセット |

トップページ `/` は各モードへのリンク一覧です。

---

## 起動方法

### 1. 操作画面サーバー（必須・ポート 8765）

```
pip install -r requirements.txt
python main.py
```

`http://localhost:8765/` を開く。`main.py` は `host="0.0.0.0"` なので、同一 LAN の他端末からは
`http://<このPCのIPアドレス>:8765/` でアクセスできます（Windows ファイアウォールの受信許可が必要）。

### 2. 音声中継サーバー（任意・ポート 8766）

プッシュ・トゥ・トーク音声を使う場合のみ、**別プロセス**で起動します。

```
cd voice_comm
pip install -r requirements.txt
python server.py
```

詳細は [`voice_comm/README.md`](voice_comm/README.md)。マイクはブラウザの制約で
**localhost か HTTPS でのみ**利用可能（LAN の他端末で使う場合の回避策は同 README 参照）。

### 3. 動画素材

`video/` に `1号機.mp4`〜`5号機.mp4`・`全体カメラ.mp4` を配置（[`video/README.md`](video/README.md)）。
無い場合は画面上で「映像 No Connect」プレースホルダーが出ます（レイアウトは確認可能）。

---

## 構成

```
main.py                  FastAPI サーバー（状態管理・WebSocket・REST・静的配信）
requirements.txt         サーバー依存（fastapi, uvicorn）

static/
  index.html             モード一覧
  control.html           コントロールモード
  analytics.html         アナリティクスモード
  engineer.html          エンジニアモード
  reporter.html          報告者モード（スマホ・PWA）
  master.html            マスターモード
  audio_analyzer.html    アナリティクスの iframe 内ダミー音声解析ツール
  common.css             全画面共通スタイル（テーマ変数・通知バー・タスク表示 等）
  common.js              全画面共通 JS（RescueCommon: WS・映像・タスク描画・通知 等）
  manifest.webmanifest   報告者モードの PWA マニフェスト
  icons/                 PWA アイコン

voice_comm/
  server.py              音声中継サーバー（単体・ポート 8766・最大 5 台）
  voice-client.js        ブラウザ用音声クライアント（全画面に組み込み済み）
  README.md              音声システムの説明

video/                   動画素材（Git 管理外）
docs/                    仕様・プロトコル等のドキュメント
situation_reporter.html  ※報告者モードの旧プロトタイプ（現在は未使用・参考用）
仕様1.md                 最初の要求仕様（原典。最新仕様は docs/SPEC.md）
```

---

## アーキテクチャ

- **サーバー（`main.py`）が唯一の状態（`AppState`）を保持**し、変更があるたびに
  全 WebSocket クライアントへ最新スナップショットをブロードキャストします。
- 各画面は接続時に現在状態を受信し、以後はブロードキャストで再描画します
  （クライアント間の直接通信は無し）。
- 音声のみ別系統（`voice_comm`、ポート 8766）で、操作画面サーバーとは独立。

```
[control] [analytics] [engineer] [reporter] [master]
     \        \          |          /         /
      \        \         |         /         /     ws://host:8765/ws/<role>
       ----------  main.py (AppState)  ----------
                         |  状態変更 → 全員へ {type:"state", payload: snapshot}
       ----------  voice_comm/server.py  ---------  ws://host:8766/voice
      （PTT 音声中継・操作画面サーバーとは独立）
```

状態の中身・WebSocket メッセージ・REST API は [`docs/STATE_AND_PROTOCOL.md`](docs/STATE_AND_PROTOCOL.md) を参照。
各モードの要求仕様は [`docs/SPEC.md`](docs/SPEC.md) を参照。

---

## 開発メモ

- フロントは **素の HTML / CSS / JS のみ**（TypeScript やビルド工程は使わない方針）。
- テーマは「やや明るいグレーのダークテーマ・白ボーダー・影や複雑な背景なし」。
  色やスペーシングは `static/common.css` の CSS 変数（`--bg` `--surface` `--accent` 等）に集約。
- 共通処理は `static/common.js` の `window.RescueCommon`（略称 `RC`）に集約。
  新しい画面を足すときも `RC.createWsClient` / `RC.renderTasks` / `RC.createNotifier` 等を使う。
- `python main.py` は `reload=True` で起動するので、編集すると自動再読込されます。
