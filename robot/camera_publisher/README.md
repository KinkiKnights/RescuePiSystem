# camera_publisher

Pi 上で USB カメラを H.264 化し、外部 relay(SFU)へ WebRTC 配信する publisher。

## 出自と対向コンポーネント

- 移設元: `sanjofumihiro/ClaudeShareContents` の `webrtc-camera/publisher/` @ `c6dcc87`
- 対向(他デバイス側): relay(Go 製 SFU)・web ビューアは引き続き
  [ClaudeShareContents/webrtc-camera](https://github.com/sanjofumihiro/ClaudeShareContents) にあります。

## プロトコル契約(relay との互換性)

publisher と relay は WebSocket シグナリング(`ws://<relay>:8080/ws`)で接続します。
**relay 側のシグナリング仕様を変更した場合は、必ずこの publisher も同時に更新してください。**
逆にこちらを変更する場合も relay の対応を確認すること。

## 起動

master_control の programs.json から起動されます:

```bash
PI_ID=KK05 SERVER=ws://192.168.137.1:8080/ws CAM1="v4l2src device=/dev/video0 ! image/jpeg,width=1024,height=768,framerate=30/1 ! jpegdec" ./publish-pi5.sh
```

- `publish-pi5.sh` — Raspberry Pi 5 用(ソフトウェアエンコード)
- `publish-pi4.sh` — Raspberry Pi 4 用(v4l2 ハードウェアエンコード)
- `publish-test.sh` — テストパターン配信

## 既知の未修正

`publish.py` の `set_cam` は request pad を `get_static_pad` で取得しようとして失敗する
(複数カメラの camChange のみ影響。単一カメラは input-selector の自動選択で動作)。
