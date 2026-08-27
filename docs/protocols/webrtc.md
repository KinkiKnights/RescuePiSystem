# WebRTC 契約 — camera_publisher ⇔ webrtc_relay ⇔ ブラウザ

号機のカメラ映像を kkrtx の SFU 経由で操作画面へ配る経路のワイヤ契約。
3 者すべてが本リポジトリ内にある（`robot/camera_publisher` /
`server/webrtc_relay` / `server/webrtc_relay/web/webrtc-camera.js`）ので、
変更は必ず同じコミットで揃えること。

## エンドポイント

| パス | 用途 |
|---|---|
| `ws://<kkrtx>:8080/ws` | シグナリング（publisher と viewer が共用） |
| `GET http://<kkrtx>:8080/pis` | 接続中の号機 ID 一覧 `{"pis":["KK01",…]}` |
| `GET http://<kkrtx>:8080/` | 視聴ページ（`-web` で指定したディレクトリを配信） |

relay の起動は `webrtc_relay -addr :8080 -web <webdir>`（既定 `-web ../web`）。
systemd では `deploy/systemd/webrtc-relay.service.in` が絶対パスで渡す。

## シグナリングメッセージ（JSON・双方向）

```
{ "type": …, "role": …, "id": …, "sdp": …, "candidate": …, "cam": …, "message": … }
```

| type | 送る側 | 内容 |
|---|---|---|
| `hello` | publisher | `{"type":"hello","role":"publisher","id":"KK05"}`（`id` 必須。無しはエラー） |
| `hello` | viewer | `{"type":"hello","role":"viewer","id":"KK05"}`（購読したい号機 ID） |
| `offer` | publisher | webrtcbin が作った SDP offer |
| `answer` | relay → publisher / viewer → relay | SDP answer |
| `candidate` | 双方 | ICE candidate（`sdp` 確立前は relay 側でキューされる） |
| `camChange` | viewer | `{"type":"camChange","cam":<番号>}` を該当号機へ転送（`0`=画面共有） |
| `error` | relay | `{"type":"error","message":"no publisher for id: KK05"}` 等 |

- **号機 ID は `KK01`〜`KK05` に統一**（`config/units.json` の `units[n].pi_id` が宣言）。
  publisher は `PI_ID`（既定はホスト名の大文字化。`kk05` → `KK05`）で名乗り、
  操作画面は `"KK" + 号機番号2桁` を購読するので、ホスト名を `kk0N` にしておけば
  設定なしで一致する。ホスト名が違う機体では `PI_ID` を明示すること。
- publisher が `id` を送らない場合は relay がエラーを返す（既定 ID で代替すると
  号機 1 の枠を奪う事故になり得るため）。
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
new WebRTCCamera({ server: "ws://<kkrtx>:8080/ws" }).connect(videoEl, "KK05");
```

中継先は操作画面のマスターモードで指定でき、空なら画面ホストの 8080 を自動推定する。
