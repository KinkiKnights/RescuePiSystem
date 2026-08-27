# joy 契約 — control_ui ⇔ joy_node_web

操作画面（および操縦用ブラウザ）から号機の `joy_node_web` へ送る WebSocket 契約。
**実装の単一の真実は submodule 側の
[`robot/ros2/joy_node_web/docs/COMMUNICATION_SPEC.md`](../../robot/ros2/joy_node_web/docs/COMMUNICATION_SPEC.md)**。
ここには本リポジトリ側（`server/control_ui`）が依存している範囲だけを書き出す。
submodule を bump したときはこの文書と照合すること。

## 接続先

```
ws://<号機アドレス>:8700/joys
```

- ポート・パスは `server/control_ui/main.py` の `ROBOT_WS_PORT` / `ROBOT_WS_PATH`。
  号機アドレスは `config/units.json` の候補配列から live 経路を解決した結果を使う。
- control_ui はコマンド送信のたびに **接続 → 送信 → 切断**（エッジトリガ）。
  操縦用の連続送信（`/joy` 20Hz）は接続を保つブラウザ側が担う。

## メッセージの分岐

受信側は **`command` フィールドの有無**で 2 種類に分ける。

| 種別 | 判定 | 反映先 |
|---|---|---|
| ジョイスティックデータ | `command` が無い（`{id, axes, buttons}`） | `/joy`（`type==1` は `/joy2`） |
| コマンド | `command` がある | 下表のトピック |

## control_ui が送るコマンド

| コマンド | ペイロード | 反映先トピック |
|---|---|---|
| `set_goal` | `{"command":"set_goal","x":float,"y":float,"yaw":float,"frame_id":"map"}` | `/goal_pose` (`geometry_msgs/PoseStamped`) |
| `cancel_goal` | `{"command":"cancel_goal"}` | `/cancel_goal` (`std_msgs/Empty`) |

`emergency_stop` / `emergency_release`（`/emergency_stop`, `std_msgs/Bool`）も
joy_node_web 側に実装があるが、現状 control_ui からは送っていない。
未知の `command` は無視される。

## 座標系（暗室座標 → set_goal）

フィールドは 1800×1800mm の正方形。操作画面が持つ暗室座標は **0..1 正規化**
（左上 =(0,0)・`nx`=右方向・`ny`=下方向）で、`set_goal` はメートル・`map` フレーム。
`main.py` の `_dark_room_goal()` が COMMUNICATION_SPEC「4. 座標系」に従って変換する。

| フィールド | 原点 | 変換 |
|---|---|---|
| 青 | 左上（X=下向き正, Y=右向き正） | `x = ny*1.8`, `y = nx*1.8` |
| 赤 | 右上（X=下向き正, Y=左向き正） | `x = ny*1.8`, `y = (1-nx)*1.8` |

- 暗室座標には向きの情報が無いため `yaw` は 0.0 固定。
- マスターモードの原点校正オフセットは「下が +・右が −」で、`x` に加算・`y` から減算する。
- 赤フィールドでの右方向オフセットの符号は要確認（暫定で青と同じ扱い）。

## 変えるときの手順

1. `joy_node_web`（submodule）側で仕様と実装を更新して push。
2. 親リポジトリで submodule ポインタを bump（`git add robot/ros2/joy_node_web`）。
3. `server/control_ui/main.py` の送信側とこの文書を**同じコミットで**更新する。
