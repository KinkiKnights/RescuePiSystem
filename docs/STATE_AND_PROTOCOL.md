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
| `analytics_target_unit` | int(1–5) | アナリティクスの解析対象号機 |
| `notification` | `{text, active, timestamp}` | 通知バー。`active` は新着パルス用、`timestamp` で重複再生を防止 |
| `tasks` | `[{id, text, room, done}]` | タスク一覧。`room` は `null`(共通)/`"A"`/`"B"`/`"C"` |
| `units` | `{1..5: {...}}` | 機体ごとの状態（下表） |
| `room_units` | `{A,B,C: int}` | 部屋に対応する号機。`0` = 未割当。**1 機体は 1 部屋のみ** |
| `room_analysis` | `{A,B,C: {...}}` | 部屋ごとの解析結果（下表）。control / analytics / reporter が共有 |
| `video_mode` | `"file"`/`"webrtc"` | 映像ソース。`file`=動画ファイル / `webrtc`=機体上カメラ中継 |
| `webrtc_server` | str | WebRTC中継先。空なら `ws://<host>:8080/ws` を自動推定。`host[:port]` か `ws://…/ws` |
| `analysis` | `{stove,injury,color,audio,pattern,notes,status}` | 旧・全体解析データ（master のプリセット投入用。現行 UI ではほぼ未使用） |
| `master_overlay` | `{visible, title, lines}` | アナリティクス画面への撮影指示オーバーレイ |
| `analysis_request` | `{pending, unit, timestamp}` | エンジニア→アナリティクスの解析要請 |
| `control_request` | `{pending, unit, timestamp}` | エンジニア→コントロールの割り込み要請 |

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

> 部屋名の対応: A=広場 / B=暗室 / C=2階（`common.js` の `RC.ROOM_NAMES`）。

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
| `set_video_mode` | `mode` | 映像ソースを切替（`"file"`/`"webrtc"`） |
| `set_webrtc_server` | `server` | WebRTC中継先を設定（空で自動推定） |
| `complete_task` | `task_id` | 指定タスクを完了 |
| `complete_next` | `room` | その部屋（空文字=共通）の未完了タスクを 1 つ完了 |
| `reset` | — | 全状態を初期値へリセット |

各操作後、サーバーは更新後スナップショットを全 WebSocket クライアントへ配信します。

---

## 4. 音声（別系統・ポート 8766）

操作画面サーバーとは独立した `voice_comm/server.py` が中継します。
プロトコルの詳細は [`../voice_comm/README.md`](../voice_comm/README.md) を参照。

---

## 5. 映像（file / WebRTC）

各画面のカメラ映像は `video_mode` で 2 系統を切替えます（マスターモードから操作）。

- **file**: `video/<号機>.mp4` をループ再生（既定。素材が無ければ「No Connect」表示）。
- **webrtc**: 機体上カメラの WebRTC 中継。号機 `n` → カメラ ID **`RES<n>`**（RES1〜RES5）。
  - 中継サーバー（SFU）は**別プロジェクト** `ClaudeShareContents/webrtc-camera`（Go・既定ポート 8080）。
    このアプリには含まれないため別途起動が必要。
  - 視聴クライアントは `static/webrtc-camera.js`（`new WebRTCCamera({server}).connect(videoEl, "RES1")`）。
  - 中継先は `webrtc_server`（空なら `ws://<画面を開いたホスト>:8080/ws` を自動推定）。
  - 全体カメラ（overview）は WebRTC 非対応で、webrtc モードでも `video/全体カメラ.mp4` を表示。
