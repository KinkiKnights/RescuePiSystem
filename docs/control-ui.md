# 操作画面サーバ (control_ui)

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

### 1. 操作画面サーバー（必須・ポート 80）

kkrtx では `deploy/server/kkrtx_setup.sh` が systemd 常駐まで設定します
（`control-ui.service`）。手元で動かす場合:

```
pip install -r server/control_ui/requirements.txt
python3 server/control_ui/main.py
```

`http://localhost/` を開く。`main.py` は `host="0.0.0.0"` なので、同一 LAN の他端末からは
`http://<このPCのIPアドレス>/` でアクセスできます（Windows ファイアウォールの受信許可が必要）。
ポートは `config/units.json` の `server.control_ui_port`（既定 80）で、`CONTROL_UI_PORT`
環境変数でも上書きできます。**80 番は特権ポート**なので、非 root で動かすときは
`CONTROL_UI_PORT=8765` のように変えるか、systemd ユニット（`CAP_NET_BIND_SERVICE` 付き）を使います。

### 2. 音声中継サーバー（任意・ポート 8766）

プッシュ・トゥ・トーク音声を使う場合のみ、**別プロセス**で起動します。

```
cd voice_comm
pip install -r requirements.txt
python server.py
```

詳細は [`voice_comm/README.md`](../server/voice_comm/README.md)。マイクはブラウザの制約で
**localhost か HTTPS でのみ**利用可能（LAN の他端末で使う場合の回避策は同 README 参照）。

#### iPhone など LAN の他端末で送話を使う（HTTPS）

iOS Safari は localhost 以外ではマイク利用に **HTTPS（セキュアコンテキスト）が必須**で、
http のままだと「マイク不可（HTTPSが必要）」になります。自己署名証明書で HTTPS 起動します。

```
pip install cryptography
python make_cert.py            # certs/cert.pem, certs/key.pem を生成（LAN IP を自動検出）
python main.py                 # 証明書があれば自動的に HTTPS で起動
python voice_comm/server.py    # 音声サーバーも同じ certs/ を使い WSS になる
```

iPhone で `https://<このPCのIP>/reporter` を開き、証明書の警告を許可（または
`certs/cert.pem` を構成プロファイルとして信頼）すると送話できます。
証明書は `make_cert.py` で各自生成（`certs/` は Git 管理外）。

### 3. 実機カメラ映像（WebRTC・唯一の映像ソース）

映像ソースは WebRTC（機体上カメラの中継）のみです。同じリポジトリの中継サーバー
（[`server/webrtc_relay/`](../server/webrtc_relay/)・Go・ポート 8080）を起動すると、各画面に
リアルタイム映像が表示されます。号機 1〜5 がカメラ ID `KK01`〜`KK05` に対応します（`config/units.json` の `pi_id`）。
中継先はマスターモードで指定（空なら画面ホストの 8080 を自動推定）。ストリーム未接続時は
「映像 No Connect」プレースホルダーが表示されます。詳細は
[`docs/protocols/state.md`](protocols/state.md) の「映像」節。

### 4. 号機アドレス（固定設定・変更不可）

号機ごとのアドレス（コントロールの joy_node_web 接続先 `ws://<号機>:8700/joys`）は
リポジトリ直下の **[`config/units.json`](../config/units.json)** が唯一の真実です。
`main.py` が起動時に読み込みます。**マスターモードからは編集できません**（読み取り専用表示のみ）。
変更したら `config/units.json` を編集してサーバーを再起動してください。

`units[n].addrs` は**優先順の候補配列**で、先頭が最優先です（既定は
無線 `192.168.10.11N` → 調整無線 `.13N` → 有線 `.12N` → mDNS `kk0N.local`）。
サーバーは候補を周期的に probe し、live な経路を自動採用します（`resolve.sticky`
が true の間は現用経路が live なら切り替えません）。ping 監視の対象も
このファイルから導出されるので、機器表を別に持つ必要はありません。

---

### 5. 暗室座標指定（エンジニアモード・全モード共有）

フィールドは **1800×1800mm の全面暗室（正方形・単一部屋）**。エンジニアモードの
「暗室座標指定」パネルで、そのフィールドを模したマップ上をクリックすると暗室（被災者）の
位置を 1 点だけ指定できます。指定した座標は既存の WebSocket 状態同期を通じて
**全モード（コントロール／アナリティクス／報告者／マスター／エンジニア）へ即時共有**され、
同じマップ上にマーカーとして表示されます（エンジニアのみ操作可能、他モードは読み取り専用）。

- マップは正方形（旧・3部屋レイアウト 広場A/暗室B/2階C は廃止し、1 枚の暗い全面暗室に統一）。
- 座標は**マップ表面に対する 0〜1 の正規化値**（`{x, y}`）で保持し、画面サイズやモードに依存しません。
  数値表示のみ 1800mm フィールドに合わせた **mm 表記**（`x*1800`, `y*1800` を整数化）ですが、
  ワイヤ／保存値は正規化 0〜1 のまま（サーバー検証も不変）。
- 単一座標のみ。新規クリックで上書き、エンジニアの「クリア」ボタンで解除。全体リセットでも解除されます。
- **フィールド陣営（赤／青）と入口:** マスターモードで「赤フィールド／青フィールド」を選択すると、
  共有状態 `field_side` として全モードへ配信され、マップ辺に「入口」が描かれます。
  **赤＝右辺の下半分／青＝左辺の下半分**（SVG 原点は左上のため下半分＝画面下側）。未選択時は入口なし。
- マップは外部アセット・ネットワーク不要の自作 SVG です。詳細は
  [`docs/protocols/state.md`](protocols/state.md) の「7. 暗室座標」節を参照。

---

## 構成

```
main.py                  FastAPI サーバー（状態管理・WebSocket・REST・静的配信）
config.json              号機ごとの固定 IP 設定（joy_node_web 接続先。変更不可・Git 管理）
requirements.txt         サーバー依存（fastapi, uvicorn）

static/
  index.html             モード一覧
  control.html           コントロールモード
  analytics.html         アナリティクスモード
  engineer.html          エンジニアモード
  reporter.html          報告者モード（スマホ・PWA）
  master.html            マスターモード
  common.css             全画面共通スタイル（テーマ変数・通知バー・タスク表示 等）
  common.js              全画面共通 JS（RescueCommon: WS・映像・タスク描画・通知 等）
  manifest.webmanifest   報告者モードの PWA マニフェスト
  icons/                 PWA アイコン

voice_comm/
  server.py              音声中継サーバー（単体・ポート 8766・最大 5 台）
  voice-client.js        ブラウザ用音声クライアント（全画面に組み込み済み）
  README.md              音声システムの説明

video/                   ※旧ダミー映像素材フォルダ（廃止・未使用。映像は WebRTC のみ）
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
      \        \         |         /         /     ws://host/ws/<role>  
       ----------  main.py (AppState)  ----------
                         |  状態変更 → 全員へ {type:"state", payload: snapshot}
       ----------  voice_comm/server.py  ---------  ws://host:8766/voice
      （PTT 音声中継・操作画面サーバーとは独立）
```

状態の中身・WebSocket メッセージ・REST API は [`docs/protocols/state.md`](protocols/state.md) を参照。
各モードの要求仕様は [`docs/spec.md`](spec.md) を参照。

---

## 開発メモ

- フロントは **素の HTML / CSS / JS のみ**（TypeScript やビルド工程は使わない方針）。
- テーマは「やや明るいグレーのダークテーマ・白ボーダー・影や複雑な背景なし」。
  色やスペーシングは `static/common.css` の CSS 変数（`--bg` `--surface` `--accent` 等）に集約。
- 共通処理は `static/common.js` の `window.RescueCommon`（略称 `RC`）に集約。
  新しい画面を足すときも `RC.createWsClient` / `RC.renderTasks` / `RC.createNotifier` 等を使う。
- `python main.py` は `reload=True` で起動するので、編集すると自動再読込されます。
