# WebRTC 契約 — camera_publisher ⇔ webrtc_relay ⇔ ブラウザ

号機のカメラ映像を kkrtx の SFU 経由で操作画面へ配る経路のワイヤ契約。
3 者すべてが本リポジトリ内にある（`robot/camera_publisher` /
`server/webrtc_relay` / `server/webrtc_relay/web/webrtc-camera.js`）ので、
変更は必ず同じコミットで揃えること。

## エンドポイント

| パス | 用途 |
|---|---|
| `ws://<kkrtx>:8080/ws` | シグナリング（publisher と viewer が共用） |
| `GET http://<kkrtx>:8080/pis` | 接続中の号機 ID 一覧 `{"pis":["RES1",…]}` |
| `GET http://<kkrtx>:8080/` | 視聴ページ（`-web` で指定したディレクトリを配信） |

relay の起動は `webrtc_relay -addr :8080 -web <webdir>`（既定 `-web ../web`）。
systemd では `deploy/systemd/webrtc-relay.service.in` が絶対パスで渡す。

## シグナリングメッセージ（JSON・双方向）

```
{ "type": …, "role": …, "id": …, "sdp": …, "candidate": …, "cam": …, "message": … }
```

| type | 送る側 | 内容 |
|---|---|---|
| `hello` | publisher | `{"type":"hello","role":"publisher","id":"RES5"}`（`id` 省略時 `PI01`） |
| `hello` | viewer | `{"type":"hello","role":"viewer","id":"RES5"}`（購読したい号機 ID） |
| `offer` | publisher | webrtcbin が作った SDP offer |
| `answer` | relay → publisher / viewer → relay | SDP answer |
| `candidate` | 双方 | ICE candidate（`sdp` 確立前は relay 側でキューされる） |
| `camChange` | viewer | `{"type":"camChange","cam":<番号>}` を該当号機へ転送（`0`=画面共有） |
| `error` | relay | `{"type":"error","message":"no publisher for id: RES5"}` 等 |

- 号機 ID は操作画面では `RES1`〜`RES5`（号機 1〜5 に対応）。
  publisher 側は `PI_ID`（ホスト名由来。例 `KK05`）を渡すため、
  **運用では ID を揃えること**（揃っていないと viewer が publisher を見つけられない）。
- viewer が接続した時点で publisher が居ない ID は `error` が返る。
  操作画面はこのとき「映像 No Connect」を表示する。

## 号機側（publisher）

`robot/camera_publisher/publish-pi5.sh`（Pi 5・ソフトエンコード）または
`publish-pi4.sh`（Pi 4・ハードエンコード）が `publish.py` を起動する。主な環境変数:

| 変数 | 既定 | 説明 |
|---|---|---|
| `PI_ID` | ホスト名の大文字 | シグナリングで名乗る ID |
| `SERVER` | `ws://<relay>:8080/ws` | relay のシグナリング URL |
| `CAM1` | `v4l2src … ! jpegdec`（USB） | カメラ 1 の GStreamer ソース。CSI は `libcamerasrc` |

relay 切断時は publisher 側が自動再接続する。

## クライアント（ブラウザ）

`server/webrtc_relay/web/webrtc-camera.js` が実体で、操作画面は
`/static/webrtc-camera.js` として**同じファイル**を配信する（複製を持たない）。

```js
new WebRTCCamera({ server: "ws://<kkrtx>:8080/ws" }).connect(videoEl, "RES5");
```

中継先は操作画面のマスターモードで指定でき、空なら画面ホストの 8080 を自動推定する。
