# カメラ / マイクのデバイス設定

号機のカメラとマイクは **master_control の Web UI（`http://<号機IP>/` の
DEVICE CONFIGURATION）から、実際に接続されている候補を選んで設定する**。
選んだ内容は号機ごとの設定ファイルに記録され、次回起動以降も効く。

- 実装: [`robot/device_config.py`](../robot/device_config.py)（設定の読み書き・候補列挙・
  GStreamer ソース組み立ての単一の実装）
- UI と API: [`robot/master_control/`](../robot/master_control/)

## なぜこうなっているか

以前はデバイス指定が `programs.json` の `cmd` 文字列に埋め込まれていた。

```
CAM1="v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec" publish-pi5.sh
… mic_publisher.py --hub … --unit 5 --device hw:1,0
```

これには 2 つ問題があった。

1. **番号固定**（`/dev/video0` / `hw:1,0`）。USB の抜き差し・起動順・カメラや
   マイクの交換でデバイス番号がずれると映像や音が出なくなり、UI からは
   原因が分からなかった。
2. **変更手段が長いコマンド行の手編集**しかなかった。カメラとマイクはロボットに
   必須なのに、設定が一番触りにくい場所にあった。

いまは **安定した識別子** で持ち、**候補から選ぶ** 形にしている。

| | 指定に使う識別子 | 変わらない理由 |
|---|---|---|
| カメラ | `/dev/v4l/by-id/usb-<製品名>_<シリアル>-video-index0` | USB ポートとデバイス固有 ID から作られる。番号ではない |
| マイク | `hw:CARD=<カード名>,DEV=0` | カード**番号**は起動順で変わるが、カード名は変わらない |

## 設定ファイル

先に見つかったものを使う（いずれも**リポジトリの外**なので `git pull` で消えない）。

1. 環境変数 `RESCUE_DEVICES_CONFIG`
2. `~/.config/rescue-pi/devices.json` ← 通常はここ。UI の保存先
3. `/etc/rescue-pi/devices.json` ← 読み取りのフォールバック

```json
{
  "schema_version": 1,
  "camera": {
    "kind": "usb",
    "device": "/dev/v4l/by-id/usb-HD_USB_Camera_HD_USB_Camera_2020040501-video-index0",
    "format": "mjpeg",
    "width": 1024,
    "height": 768,
    "framerate": 30,
    "pipeline": ""
  },
  "mic": {
    "device": "hw:CARD=Device,DEV=0",
    "capture_rate": 48000
  }
}
```

`camera.kind` は `usb` / `csi`（libcamerasrc）/ `test`（videotestsrc）/
`raw_pipeline`（`pipeline` に GStreamer 文字列を直接書く逃げ道）。

## 値の優先順

```
コマンドライン引数  >  環境変数  >  devices.json  >  接続機器からの自動検出  >  既定値
```

- `programs.json` の `cmd` で `CAM1=...` や `--device` を明示すればそれが勝つ
  （一時的な検証用。恒久設定は UI で行う）
- systemd 常駐の場合も同じ。`/etc/default/mic-publisher` で `MIC_DEVICE` を
  設定するとそれが勝つので、**通常はコメントアウトしたまま**にして UI 設定を使う
- 設定ファイルが無い号機でも、接続されているデバイスから自動検出して動く

## Web UI での操作

`http://<号機IP>/` の **DEVICE CONFIGURATION** セクション。

| 操作 | 動き |
|---|---|
| 再スキャン | 接続されているカメラ / マイクを再列挙する（交換直後はこれを押す） |
| 保存 | devices.json に書く。**次回のプログラム起動から**有効 |
| 保存して即反映 | 保存 → `camera` と `mic` を再起動（停止中なら起動）してその場で反映 |

カメラは候補を選ぶと、そのデバイスが対応している解像度がプルダウンに出る
（`v4l2-ctl --list-formats-ext` 由来）。手入力も可能。マイクは
`hw:CARD=...`（変換なし）と `plughw:CARD=...`（ALSA が変換）の両方が候補に出る。

**保存済みのデバイスが今は繋がっていない場合**は警告が出て、選択肢の先頭に
`(未接続) …` として残る。交換したときはそのまま候補から選び直せばよい。

## API

master_control が提供する（UI もこれを使う）。

| メソッド・パス | 用途 |
|---|---|
| `GET /devices` | 現在の設定・候補一覧・解決結果・デバイス存在確認・対象プログラムの状態 |
| `POST /devices` | 設定を保存（`{"config": {...}, "apply": true, "target": "all"}`）。`apply` で即反映まで |
| `POST /devices/apply` | 保存済み設定を即反映（`{"target": "camera"｜"mic"｜"all"}`） |

## CLI（号機上での確認）

```bash
python3 robot/device_config.py            # 解決結果を表示（設定が効いているかの確認）
python3 robot/device_config.py --list     # 接続されている候補を JSON で列挙
python3 robot/device_config.py --init     # 設定ファイルが無ければ自動検出して作成
```

`deploy/robot/app_setup.sh` はセットアップ時に `--init` を実行するので、
初回から「その号機に繋がっているデバイス」が入った状態で始まる。

## 関連

- カメラ映像の経路: [webrtc-camera.md](webrtc-camera.md) / [protocols/webrtc.md](protocols/webrtc.md)
- マイク音声の経路: [mic-system.md](mic-system.md) / [protocols/mic.md](protocols/mic.md)
- 号機のセットアップ: [robot.md](robot.md)
