# 状態・通信プロトコル リファレンス

サーバー（`main.py`）が保持する共有状態と、クライアントとの通信仕様をまとめます。
**サーバーが唯一の状態を持ち**、変更のたびに全 WebSocket クライアントへスナップショットを配信します。

---

## 1. 共有状態スナップショット

`GET /api/state` および WebSocket の `{type:"state", payload:<これ>}` で配信されるオブジェクト。
（実体は `main.py` の `AppState.snapshot()`）

| キー | 型 | 説明 |
|---|---|---|
| `control_video_unit` | int(1–5) | コントロールのカメラ表示号機 |
| `control_operating_unit` | int(1–5) | コントロールが操縦中の号機 |
| `analytics_target_unit` | int(1–5) | アナリティクスの解析対象号機。音声解析タイルはこの号機に応じて damiyan 検出器（実機）の Web ビューアを iframe 埋め込みする（号機3→8771 / 4→8772 / 5→8773、`http://<location.hostname>:<port>/`・HTTP）。号機1／2 は検出器が無いためタイルを非表示にする。「8. 音声解析（damiyan 検出器）」節参照 |
| `notification` | `{text, active, timestamp}` | 通知バー。`active` は新着パルス用、`timestamp` で重複再生を防止 |
| `tasks` | `[{id, text, room, done}]` | タスク一覧。`room` は `null`(共通)/`"A"`/`"B"`/`"C"` |
| `units` | `{1..5: {...}}` | 機体ごとの状態（下表） |
| `room_units` | `{A,B,C: int}` | 部屋に対応する号機。`0` = 未割当。**1 機体は 1 部屋のみ** |
| `room_analysis` | `{A,B,C: {...}}` | 部屋ごとの解析結果（下表）。control / analytics / reporter が共有 |
| `webrtc_server` | str | WebRTC中継先。空なら `ws://<host>:8080/ws` を自動推定。`host[:port]` か `ws://…/ws` |
| `analysis` | `{stove,injury,color,audio,pattern,notes,status}` | 旧・全体解析データ（master のプリセット投入用。現行 UI ではほぼ未使用） |
| `master_overlay` | `{visible, title, lines}` | アナリティクス画面への撮影指示オーバーレイ |
| `analysis_request` | `{pending, unit, timestamp}` | エンジニア→アナリティクスの解析要請 |
| `control_request` | `{pending, unit, timestamp}` | エンジニア→コントロールの割り込み要請 |
| `unit_ips` | `{"1".."5": str}` | 号機ごとの固定 IP（`config.json` 由来・読み取り専用）。control が joy 接続に使用。「6. 号機 IP」節参照 |
| `dark_room_coord` | `{x, y}` または `null` | 暗室座標（エンジニアがマップ上をクリックして指定・全モード共有）。`null` = 未設定。`x`,`y` はマップ表面に対する **0〜1 正規化座標**（画面サイズ・モード非依存）。単一座標のみ保持し、新規指定で上書き。表示は 1800mm 換算（後述）。「7. 暗室座標」節参照 |
| `field_side` | `"red"` / `"blue"` / `null` | フィールド陣営（マスターが選択・全モード共有）。`null` = 未選択。マップ上の「入口」描画位置を決める（赤＝右辺下半分／青＝左辺下半分）。「7. 暗室座標」節参照 |
| `unit5_auto_run` | `"off"` / `"armed"` / `"lit"` | 5号機 自動走行ボタンの状態（サーバー権威・全モード共有）。`off`=消灯（既定）／`armed`=点滅（暗室座標が入力/変更された）／`lit`=点灯（自動走行中）。「9. 5号機 自動走行」節参照 |

### `units[n]`

| キー | 型 | 説明 |
|---|---|---|
| `unit` | int | 号機番号 |
| `delay_ms` | int | 通信遅延（ダミー値） |
| `connected` | bool | 接続状態 |
| `method` | `"WiFi"`/`"TPIP"` | 通信方式 |
| `disabled` | bool | 行動不能フラグ |
| `other_op` | bool | 別オペレーターが操縦中（ダミー） |

### `room_analysis[room]`

| キー | 型 | 説明 |
|---|---|---|
| `stove` | str | ストーブ有無（`""`/`"不明"`/`"無し"`/`"有り"`） |
| `stoveDone` | bool | ストーブ項目の確定フラグ |
| `qr` | str | 負傷QR読取テキスト（例 `"右足負傷"`）。control/analytics がメイン映像をQR解析して取得 |
| `injuryDone` | bool | 負傷（QR）項目の確定フラグ |
| `color` | str | 顔色（`""`/`"不明"`/`"黒"`/`"赤"`/`"緑"`/`"青"`/`"黄"`/`"紫"`/`"水"`/`"白"`） |
| `colorDone` | bool | 顔色項目の確定フラグ |
| `notes` | str | 備考（自由記述） |

> 部屋名の対応: A=広場 / B=暗室 / C=2階（`common.js` の `RC.ROOM_NAMES`。タスク／解析のルーム区分として使用）。
> なお暗室座標マップ自体は 1800×1800mm の**全面暗室（正方形・単一部屋）**を表す（旧・3部屋レイアウト 広場A/暗室B/2階C は廃止）。

---

## 2. WebSocket

### 接続

```
ws://<host>:8765/ws/<role>
```

`role` は `control` / `analytics` / `engineer` / `reporter` / `master`（未知の値は `all` 扱い）。
接続直後にサーバーが現在のスナップショットを 1 回送信します。

### サーバー → クライアント

```json
{ "type": "state", "payload": { ...スナップショット... } }
```

唯一のメッセージ種別。状態が変わるたびに全クライアントへ送られます。

### クライアント → サーバー

| `type` | 追加フィールド | 効果 |
|---|---|---|
| `notify` | `text` | 通知バーに表示（全画面へ） |
| `set_analytics_target` | `unit` | 解析対象号機を変更 |
| `set_control_video` | `unit` | コントロールのカメラ号機を変更 |
| `set_control_operating` | `unit` | 操縦号機を変更 |
| `engineer_action` | `action`, `unit`, （`room`） | エンジニア操作（下表） |
| `accept_control_request` | — | 保留中の割り込み要請を承諾（カメラ・操縦を切替） |
| `accept_analysis_request` | — | 保留中の解析要請を承諾（解析対象を切替） |
| `update_analysis` | `analysis` の各キー | 旧・全体解析データを更新 |
| `set_room_analysis` | `room`, （`stove`/`color`/`notes`/`qr`/`stoveDone`/`injuryDone`/`colorDone`） | 部屋ごとの解析を更新（送られたキーのみ） |
| `complete_task` | `task_id` | 該当タスクを完了に |
| `set_dark_room_coord` | `coord` | 暗室座標を設定／解除（全モード共有）。`coord` が `{x, y}`（ともに 0〜1 の実数）なら設定、`null` なら解除。範囲外・型不正なペイロードは無視（state 不変）。主にエンジニアモードから送信 |
| `set_field_side` | `side` | フィールド陣営を設定／解除（全モード共有）。`side` が `"red"`/`"blue"` なら設定、`null` なら未選択に解除。それ以外の値は無視（state 不変）。主にマスターモードから送信 |
| `unit5_auto_run_toggle` | — | 5号機自動走行ボタンのトグル。`armed`→`lit`（5号機へ `set_goal` 送信・走行開始）、`lit`→`off`（`cancel_goal` 送信・キャンセル）。`off` のときは無視。主にコントロールモードから送信。「9. 5号機 自動走行」節参照 |
| `reporter_cue` | `room`, `text` | 報告キュー由来の通知（`[ルームX] text`） |

#### `engineer_action` の `action`

| `action` | 効果 |
|---|---|
| `interrupt1` / `interrupt2` | コントロールへ割り込み要請（`control_request` を保留に） |
| `analysis1` / `analysis2` | アナリティクスへ解析要請（`analysis_request` を保留に） |
| `toggle_method` | 通信方式 WiFi⇔TPIP を切替＋通知 |
| `disable_unit` | 行動不能⇔復活をトグル＋通知 |
| `reboot_pi` | Raspberry Pi 再起動要求の通知のみ |
| `set_room` | `room` に `unit` を割当（他部屋からは自動的に外す）＋通知 |

---

## 3. REST API

| メソッド・パス | ボディ | 用途 |
|---|---|---|
| `GET /api/state` | — | 現在のスナップショットを取得 |
| `POST /api/notify` | `{text}` | 通知を送信 |
| `POST /api/analysis` | `analysis` の各キー | 旧・全体解析データを更新 |
| `POST /api/master` | `{action, ...}` | マスター操作（下表） |

### `POST /api/master` の `action`

| `action` | 追加フィールド | 効果 |
|---|---|---|
| `show_overlay` | `title`, `lines[]` | アナリティクスへ撮影指示オーバーレイを表示 |
| `hide_overlay` | — | オーバーレイを消す |
| `set_analysis` | `preset{}`, `status` | 旧・全体解析データを一括投入 |
| `set_analytics_target` | `unit` | 解析対象号機を変更 |
| `set_webrtc_server` | `server` | WebRTC中継先を設定（空で自動推定） |
| `complete_task` | `task_id` | 指定タスクを完了 |
| `complete_next` | `room` | その部屋（空文字=共通）の未完了タスクを 1 つ完了 |
| `reset` | — | 全状態を初期値へリセット |

各操作後、サーバーは更新後スナップショットを全 WebSocket クライアントへ配信します。

> **注:** 号機 IP は `config.json` 固定・変更不可のため、以前存在した `set_unit_ips` は
> 無効化されています。クライアントが送っても無視され（サーバーログに警告）、state は変化しません。

---

## 4. 音声（別系統・ポート 8766）

操作画面サーバーとは独立した `voice_comm/server.py` が中継します。
プロトコルの詳細は [`../voice_comm/README.md`](../voice_comm/README.md) を参照。

---

## 5. 映像（WebRTC）

各画面のカメラ映像は **WebRTC（機体上カメラ中継）のみ**です。以前あったローカル動画ファイル
（`video/<号機>.mp4`）を再生する「ダミー映像」機能は廃止されました。

- 号機 `n` → カメラ ID **`RES<n>`**（RES1〜RES5）。
  - 中継サーバー（SFU）は**別プロジェクト** `ClaudeShareContents/webrtc-camera`（Go・既定ポート 8080）。
    このアプリには含まれないため別途起動が必要。
  - 視聴クライアントは `static/webrtc-camera.js`（`new WebRTCCamera({server}).connect(videoEl, "RES1")`）。
  - 中継先は `webrtc_server`（空なら `ws://<画面を開いたホスト>:8080/ws` を自動推定）。
  - ストリーム未接続時は「映像 No Connect」プレースホルダーを表示。
  - 号機以外の映像枠（overview 等）は WebRTC 対象外のため常に「No Connect」表示。

---

## 6. 号機 IP（joy_node_web 接続先・固定設定）

号機ごとの IP はリポジトリ直下の **`config.json`** に固定設定されています（Git 管理対象）。
`main.py` が起動時に読み込み `state.unit_ips` に反映し、state スナップショットで配信します。
コントロール画面はこれを使って joy WebSocket（`ws://<号機IP>:8700/joys`）へ自動接続します。

- **クライアント（マスター等）からは変更不可。** マスター画面は読み取り専用表示のみ。
  `set_unit_ips` は無効化済み（送っても無視）。`reset` でも IP は初期化されません（固定設定のため）。
- `config.json` が無い/壊れている場合はサーバーログに警告を出し、空 IP で起動を継続します。

| 号機 | IP |
|---|---|
| 1 | `192.168.10.121` |
| 2 | `192.168.10.111` |
| 3 | `192.168.10.112` |
| 4 | `192.168.10.113` |
| 5 | `192.168.10.114` |

---

## 7. 暗室座標（エンジニア指定・全モード共有）

フィールドは **1800×1800mm の全面暗室（正方形・単一部屋）**。エンジニアモードの
「暗室座標指定」パネルで、そのフィールドを模したマップ上をクリックすると暗室（被災者）
位置を 1 点だけ指定できます。値は既存の WebSocket 状態同期で全モード
（master / analytics / reporter / control / engineer）へ配信され、同じマーカーが表示されます。

- **フィールド形状:** 正方形（旧・3部屋レイアウト 広場A/暗室B/2階C は廃止）。
  マップは 1 枚の暗い正方形で全面暗室を表す。SVG viewBox は正方形（200×200）、
  CSS の `aspect-ratio` も `1 / 1` に固定してあり、クリック位置＝正規化座標が成立する。
- **入力方式:** マップ（自作 SVG・外部アセット/通信なし）上のクリック。
- **座標契約（ワイヤ／保存）:** `{x, y}` の **0〜1 正規化座標**（マップ表面の左上 = `(0,0)`、
  右下 = `(1,1)`）。画面サイズやモードに依存しないため、どの画面でも同じ相対位置に描画される。
  マップの描画・当たり判定は `common.js` の `RC.createFieldMap(el, opts)` に共通化。
- **表示（mm 換算）:** 数値読み出しは 1800mm フィールドに合わせ **mm 表示**（`x*1800`, `y*1800`
  を整数に丸め・0〜1800）。ただし**ワイヤ／保存値は正規化 0〜1 のまま**（契約・サーバー検証は不変）。
  換算は表示専用で `common.js` の `RC.formatCoord()` が行う（正規化値も併記）。
- **件数:** 単一座標のみ。新規クリックで上書き。`null` 送信（エンジニアの「クリア」ボタン）で解除。
- **設定手段:** WebSocket メッセージ `set_dark_room_coord`（`coord` = `{x,y}` か `null`）。
  サーバー側 `_parse_norm_coord()` で型・範囲（0〜1）を検証し、不正値は無視する。
- **リセット挙動:** 固定設定ではなくランタイム注記のため、`reset` で解除される（`null` に戻る）。
- **表示:** engineer は操作可能（クリック指定＋クリア＋数値表示）、他モードは読み取り専用マップ
  ＋数値表示（reporter は縮小表示）。

### 7.1 フィールド陣営と入口（`field_side`）

マスターモードで「赤フィールド／青フィールド」を選択すると、共有状態 `field_side`
（`"red"`/`"blue"`/`null`）として全モードへ配信され、マップ辺上に「入口」が描かれます。

- **選択元:** マスターモードのみ（赤／青ボタン＋解除）。他モードは読み取り専用で入口を表示。
- **設定手段:** WebSocket メッセージ `set_field_side`（`side` = `"red"`/`"blue"`/`null`）。
  サーバーは `"red"`/`"blue"`/`null` のみ受理し、それ以外は無視する。
- **入口の位置:** SVG 原点は左上のため「下半分」= y の大きい側（マップ下側）。
  - `"red"`（赤フィールド）→ **右辺の下半分**に入口帯＋「入口」ラベル。
  - `"blue"`（青フィールド）→ **左辺の下半分**に入口帯＋「入口」ラベル。
  - `null`（未選択）→ 入口を描かない。
- **リセット挙動:** オペレータ設定のため `reset` で `null`（未選択）に戻る。

## 8. 音声解析（damiyan 検出器）

アナリティクスモードの音声解析タイルは、別リポジトリ `damiyan-signal-processing` の
検出器（ダミヤン人形の音声・鳴動パターンを識別する実機ツール）の Web ビューアを
**iframe 埋め込み**で表示します。ダミーではなく実際の検出器です。

- **号機 → ポート対応:** 号機3 → 8771 / 号機4 → 8772 / 号機5 → 8773。
  `analytics_target_unit` の選択に応じて iframe の `src` を
  `http://<location.hostname>:<port>/` に切り替える（`analytics.html` の `updateAudioFrame`）。
  ポートが変わったときだけ `src` を更新し、不要な再読込を避ける。
- **ホスト:** `location.hostname` を使うため、リモートPC／mDNS 経由でアクセスしても
  検出器の同一ホスト名で解決される。本番は **HTTP**（TLS なし）。
- **LAN 公開が前提:** 検出器はオペレータの別 PC のブラウザから見えるよう、
  `--web-host 0.0.0.0`（全インターフェイス bind）で起動する。認証は無いので
  信頼できる会場 LAN 内でのみ使用すること。
- **号機1／2:** 検出器を持たないため、音声解析タイルを**非表示**にする（該当号機が
  中央メインに選択されていた場合は対象機ビューへ退避してレイアウト崩れを防ぐ）。
- **入力系統:** 各検出器は mic_relay の下流ストリーム（`127.0.0.1:5003/5004/5005`）を
  号機3/4/5 として受信する。監視する 12 周波数は damiyan リポジトリ直下の
  共有 `frequencies.json`（本番当日に公式値へ差し替え）。
- **起動:** リポジトリ直下の `start-servers.sh detector`（`all` にも含まれる）が
  検出器 3 台を起動する。VideoControlSystem 本体（`main.py`）は iframe が検出器を
  直接指すため、この機能のために追加のサーバー処理は持たない。

## 9. 5号機 自動走行（暗室座標へのオート走行）

コントロールモードの「操作」セクションに **「5号機自動走行」ボタン**を置く。暗室座標
（`dark_room_coord`）を目標に **5号機**を自動走行させ、再操作で停止・キャンセルする。
状態はサーバー権威の `unit5_auto_run`（`"off"`/`"armed"`/`"lit"`）で全モードへ共有され、
**操作中の号機に依存せず常に表示**される。

### 9.1 状態遷移

| 現状態 | 契機 | 次状態 | ロボットへの送信 |
|---|---|---|---|
| `off` | 既定 | `off` | — |
| いずれか | エンジニアが暗室座標を**新規設定/変更**（`set_dark_room_coord` の `coord` が新値） | `armed`（点滅） | 走行中(lit)だった場合のみ旧ゴールを `cancel_goal` |
| `armed` | ボタンクリック（`unit5_auto_run_toggle`） | `lit`（点灯） | `set_goal`（暗室座標→フィールド座標[m]） |
| `lit` | ボタンをもう一度クリック | `off`（消灯） | `cancel_goal` |
| `armed`/`lit` | 暗室座標が**クリア**（`coord: null`） | `off` | 走行中(lit)だった場合のみ `cancel_goal` |
| `off` | ボタンクリック | `off`（無視） | — |

- **座標変更時の判断（実装採用）:** 走行中(`lit`)に暗室座標が変更/クリアされた場合、旧ゴールは
  陳腐化するため即座に `cancel_goal` を送り、状態を `armed`（変更時）／`off`（クリア時）へ戻す。
  同一値の再送では状態を変えない（点灯中に同じ座標を送っても `lit` を維持）。

### 9.2 ロボットへのコマンド（joy_node_web）

サーバー（`main.py`）が **5号機の joy_node_web**（`ws://<unit_ips["5"]>:8700/joys`。既存の joy
接続と同一エンドポイント）へ JSON コマンドを 1 回送る（エッジトリガ）。仕様は
`kk_rescue26_pi:ros2/joy_node_web/docs/COMMUNICATION_SPEC.md`「2.2 コマンド」に準拠。

```jsonc
// 走行開始（armed→lit）
{ "command": "set_goal", "x": <m>, "y": <m>, "yaw": 0.0, "frame_id": "map" }
// キャンセル（lit→off、座標変更/クリア時）
{ "command": "cancel_goal" }
```

- ノード側は `set_goal` を `/goal_pose`（`geometry_msgs/PoseStamped`）、`cancel_goal` を
  `/cancel_goal`（`std_msgs/Empty`）へ 1 回 Publish する。
- 送信は `main.py` の `_send_robot_command()`。接続失敗・IP 未設定・`websockets` 不在時は
  ログを残して握りつぶす（サーバー継続）。

### 9.3 座標変換（正規化 0..1 → フィールド座標[m]）

暗室座標は 1800×1800mm フィールドの 0..1 正規化（`nx`=右方向・`ny`=下方向、左上=(0,0)）。
COMMUNICATION_SPEC.md「4. 座標系」に従い `main.py` の `_dark_room_goal()` が換算する。

- **青フィールド**（原点=左上, X=下向き正, Y=右向き正）: `x = ny*1.8`, `y = nx*1.8`
- **赤フィールド**（原点=右上, X=下向き正, Y=左向き正）: `x = ny*1.8`, `y = (1-nx)*1.8`
- `yaw` は暗室座標に向き情報が無いため `0.0` 固定（**要確認**）。
- `field_side` 未選択時は青（左上原点）規約で暫定換算する（**要確認**：`map` フレームの基準は
  運用側で確定のこと）。
