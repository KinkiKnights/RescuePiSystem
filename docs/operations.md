# 配備と運用

対象は Res26 フリート（号機 kk01〜kk05 = Raspberry Pi 5 / Ubuntu 24.04、
kkrtx = x86 Ubuntu、ハブ兼アプリホスト、`192.168.10.3`）。

## 1. ハブ（kkrtx）

```bash
cd ~/kk_ws/src && git clone git@github.com:KinkiKnights/RescuePiSystem.git
cd RescuePiSystem

# 手で動かして確認
python3 server/mic_hub/mic_hub.py --port 8770 --outdir ~/kk_ws/logs/mic-recordings

# 常駐化
sudo cp systemd/mic-hub.service /etc/systemd/system/
sudo cp systemd/mic-hub.default /etc/default/mic-hub
sudo systemctl daemon-reload
sudo systemctl enable --now mic-hub
systemctl status mic-hub
```

追加パッケージは不要（Python 標準ライブラリのみ）。

### 8000（control_ui）から開ける画面

運用中にブラウザで開く URL は **control_ui の 1 つに集約**してある。
`http://<kkrtx>/`（`server_ctl.sh` で手動起動している場合は `:8000`）を開けば、
トップからすべての画面へ辿れる。

| パス | 画面 | 実体 |
|---|---|---|
| `/` | トップ（全画面へのリンク） | `control_ui/static/index.html` |
| `/control` `/analytics` `/engineer` `/reporter` `/master` | 操作画面 5 モード | `control_ui/static/` |
| `/ping-monitor` `/all-monitor` `/control-panel` | 監視・統合画面 | 同上 |
| `/grid` | WebRTC グリッド視聴（全号機のカメラをタイル表示） | `control_ui/static/grid.html` |
| `/viewer/` | WebRTC ビュワー（1 号機ずつ） | `webrtc_relay/web/`（relay と同一実体） |
| `/mic/` | マイク集約ハブの状態・試聴 | `mic_hub/static/`（hub と同一実体） |

`/viewer/` と `/mic/` は**マウントしているだけでファイルは複製していない**（規約 1）。

**プロキシは挟んでいない。** 8000 が配るのは HTML/JS/CSS だけで、データ接続は
ブラウザから各サービスへ直接行く:

- WebRTC のシグナリング → `ws://<kkrtx>:8080/ws`（映像そのものは WebRTC の
  UDP を直接流れる。relay は SFU なので 8080 は通らない）
- マイクの状態と試聴 → `http://<kkrtx>:8770/api/status`, `/listen/<unit>`
- PTT 音声 → `ws://<kkrtx>:8766/voice`

中継を挟まないので、映像・音声の遅延に段が増えない。**そのため 8080 / 8766 / 8770
のサービスは 8000 とは別に動かし続ける必要がある**（`server_ctl.sh start` は 4 つ
まとめて起動する）。機体側の接続先（`ws://…:8080/ws` と
`http://…:8770/ingest/<unit>`）も従来のままで、号機の設定変更は要らない。

接続先のポートは `config/units.json` の `server.*` が単一の真実（規約 5）。
control_ui が `/api/endpoints`（JSON）と `/static/endpoints.js`
（`window.RESCUE_ENDPOINTS` を定義する小さなシム）で JS へ渡すので、
**画面側のコードにポート番号を書かない**。ポートを変えるときは `units.json`
だけを直す。

### 4 サービスをまとめて起動・停止する（常駐させない運用）

kkrtx の 4 プロセス（`control_ui` / `webrtc_relay` / `voice_comm` / `mic_hub`）は
`deploy/server/server_ctl.sh` でまとめて起動・停止できる。競技や試験のときだけ
立ち上げて、終わったら落とす運用向け。**systemd 常駐（`kkrtx_setup.sh` を
`SETUP_SERVICES=1`＝既定で実行）と併用しないこと**（同じポートを取り合う）。

```bash
# 依存の導入と relay のビルドだけ済ませる（systemd ユニットは設置しない）
SETUP_SERVICES=0 ./deploy/server/kkrtx_setup.sh

./deploy/server/server_ctl.sh start             # 4 つ全部
./deploy/server/server_ctl.sh start mic_hub     # 個別指定（複数可）
./deploy/server/server_ctl.sh status            # PID・稼働時間・ポートの待受
./deploy/server/server_ctl.sh logs -f control_ui
./deploy/server/server_ctl.sh restart
./deploy/server/server_ctl.sh stop              # プロセスグループごと停止
```

- PID とログは**リポジトリの外** `~/.local/state/rescue-pi/{run,log}` に置く
  （`RESCUE_STATE_DIR` で変更可）。リポジトリを汚さないため。
- `stop` は `setsid` で作った**プロセスグループごと** SIGTERM → 猶予
  （`STOP_GRACE`、既定 8 秒）→ SIGKILL する。孤児を残さないための作りで、
  考え方は `robot/master_control/master_server.py` の `_terminate_tree()` と同じ。
- PID ファイルのプロセスが生きていれば `start` は何もしない（二重起動の防止）。
  ポートが他プロセスに使われている場合も起動を中止する。
- ポートは `config/units.json` の `server.*` を読む。一時的に変えたいときだけ
  `CONTROL_UI_PORT` / `RELAY_PORT` / `VOICE_PORT` / `MIC_HUB_PORT` で上書きする。
- `mic_hub` の録音先と上限は `MIC_HUB_OUTDIR`（既定 `~/kk_ws/logs/mic-recordings`）
  `MIC_HUB_MAX_GB` / `MIC_HUB_RETENTION_HOURS` / `MIC_HUB_SEGMENT` で渡す。
  常駐運用と違い `/etc/default/mic-hub` は読まない。ディスク残量に合わせること
  （16kHz/mono は 115 MB/時・号機あたり）。

#### 機体ごとの値は `~/.config/rescue-pi/server.env` に置く

ディスク容量やポートの都合は機体ごとに違う。リポジトリ内の既定値を書き換えると
`git pull` で巻き戻るうえ、1 台の事情がフリート共通の既定になってしまうので、
機体固有の値は**リポジトリの外**に置く（規約 6。号機の `devices.json` と同じ
`~/.config/rescue-pi/` 配下）。中身はただの `KEY=value`。ファイルは無くてよい
（無ければスクリプトの既定値で動く）。置き場所は `RESCUE_SERVER_ENV` で変えられる。

```bash
$ cat ~/.config/rescue-pi/server.env
# kkrtx 固有の運用値 (git 管理外)。優先順位: コマンドライン > このファイル > 既定値
# / の残量 19GB に対し既定 8GB x 5 号機 x 24 時間保持は約 14GB になり圧迫するため
MIC_HUB_MAX_GB=1
```

優先順位は **コマンドライン > `server.env` > スクリプト既定値**。

```bash
./deploy/server/server_ctl.sh start mic_hub                  # --max-gb 1 (server.env)
MIC_HUB_MAX_GB=2 ./deploy/server/server_ctl.sh start mic_hub # --max-gb 2 (コマンドライン)
# server.env を置かなければ                                   # --max-gb 8 (既定値)
```

**kkrtx では `MIC_HUB_MAX_GB=1` にしている。** 算出根拠は次のとおり。16kHz/mono の
録音は 115 MB/時・号機あたり。`MIC_HUB_RETENTION_HOURS=24` のまま 5 号機を 24 時間
動かすと 115MB × 24h × 5 = **約 14GB** になり、`/` の残量 19GB を圧迫する
（`MIC_HUB_MAX_GB` は号機ごとの上限なので、既定の 8GB は事実上この保持時間の側で
決まる量に張り付く）。1GB/号機なら 5 号機でも 5GB に収まる。**この値は kkrtx の
ディスク事情に由来するものなので、リポジトリ内の既定値（8GB）は変えていない。**

**操作画面のポートに注意。** systemd ユニットは `AmbientCapabilities=CAP_NET_BIND_SERVICE`
を持つので一般ユーザのまま port 80 に bind できるが、手で起動する場合それが無い。
そのため `server_ctl.sh` は `control_ui_port` が特権ポート（<1024）だったとき、
既定で `CONTROL_UI_UNPRIV_PORT`（既定 **8000**）へ退避する。操作画面の URL は
`http://<kkrtx>:8000/` になる（画面内の relay / 音声 / mic hub への接続先は
`location.hostname` と各固定ポートから組み立てるので、ここは影響を受けない）。

従来どおり port 80 で待ち受けたいときは `control_ui` だけ sudo で起動する:

```bash
CONTROL_UI_SUDO=1 ./deploy/server/server_ctl.sh start control_ui
```

ただしこのとき control_ui が起動する子プロセス（damiyan 検出器など）も root に
なり、ログが root 所有で作られる点に注意。`setcap` をシステムの python バイナリに
付ける方法は、他の python プロセス全部に影響するので採らない。

設定は `/etc/default/mic-hub` に置く。リポジトリ内の設定ファイルに書くと
`git pull` で機体ごとの差分が巻き戻る（`programs.json` で実際に起きている）。

旧 `relay/mic_relay.py` は all_start.bash 未登録の手動 nohup 起動で、
kkrtx を再起動するたびに消えていた。`systemctl enable` しておけば勝手に戻る。

## 2. 号機（Pi）

```bash
# 録音デバイスの確認（設定は Web UI から。ここは手動確認用）
arecord -L

# kk が audio グループに入っていないと録音デバイスが見えない
sudo usermod -aG audio kk        # 反映には再ログイン

cd ~/kk_ws/src && git clone git@github.com:KinkiKnights/RescuePiSystem.git
sudo apt install -y alsa-utils python3-numpy

# 手で動かして確認
python3 ~/kk_ws/src/RescuePiSystem/robot/mic_publisher/mic_publisher.py \
    --hub http://192.168.10.3:8770 --unit 5
```

常駐のさせ方は 2 通りある。**どちらか一方だけにすること**
（両方動かすと二重起動になり、後発が `409` で弾かれ続ける）。

### (a) systemd（推奨）

```bash
sudo cp systemd/mic-publisher.service /etc/systemd/system/
sudo cp systemd/mic-publisher.default /etc/default/mic-publisher
sudo editor /etc/default/mic-publisher      # MIC_UNIT と MIC_DEVICE を機体に合わせる
sudo systemctl daemon-reload
sudo systemctl enable --now mic-publisher
journalctl -u mic-publisher -f
```

### (b) master_control（既存の Web UI から起動する場合）

`programs.json` の `mic` エントリを差し替える:

```json
{"id": 3, "name": "mic", "type": "bash", "autostart": true,
 "cmd": "python3 /home/kk/kk_ws/src/RescuePiSystem/robot/mic_publisher/mic_publisher.py --hub http://192.168.10.3:8770 --unit 5"}
```

`programs.json` は tracked なので `git pull` で巻き戻るリスクがある。
巻き戻ったら mic が旧 GStreamer 版のコマンドに戻るため、更新後は必ず確認する。

#### `programs.json` をリポジトリ外へ逃がす（推奨）

`programs.json` は「号機ごとの運用値」（規約 6）でありながら、master_control 自身が
実行時に書き換える（Web UI の autostart トグルと設定エディタの保存）。tracked な
ファイルを動いているアプリが上書きするので、`git pull` との衝突は構造的に避けられない。

`MASTER_CONTROL_PROGRAMS` に**リポジトリ外の**パスを渡すと、master_control は
そちらを読み書きする。未設定なら従来どおり `robot/master_control/programs.json`
を使う（既存の号機はそのままで動く）。親ディレクトリが無ければ書き込み時に作る。

```bash
mkdir -p ~/.config/rescue-pi
cp ~/kk_ws/src/RescuePiSystem/robot/master_control/programs.json \
   ~/.config/rescue-pi/programs.json      # 既存があれば引き継ぐ
sudo editor ~/.config/rescue-pi/programs.json   # 号機に合わせて直す
```

`master-control.service` に環境変数を足す（`deploy/systemd/master-control.service.in`
は既定を触らないので、号機側は drop-in で指定する）:

```bash
sudo systemctl edit master-control.service
# [Service]
# Environment=MASTER_CONTROL_PROGRAMS=/home/kk/.config/rescue-pi/programs.json
sudo systemctl restart master-control.service
systemctl show master-control.service -p Environment   # 反映確認
```

master_control が使う環境変数:

| 変数 | 既定 | 用途 |
|---|---|---|
| `MASTER_CONTROL_PROGRAMS` | `robot/master_control/programs.json` | プログラム定義の読み書き先 |
| `MASTER_CONTROL_PORT` | `80` | 待ち受けポート（検証時に非特権ポートへ） |
| `MASTER_AUTOSTART_DRYRUN` | 未設定 | `1` で autostart の対象をログするだけで起動しない |

### 号機ごとの設定

| 号機 | `MIC_UNIT` | `MIC_DEVICE` | 備考 |
|---|---|---|---|
| kk03 | `3` | `hw:CARD=Camera,DEV=0` | H264 USB Camera 内蔵マイクに capture がある |
| kk04 | `4` | — | HD USB Camera に音声デバイスが無い。**マイクを繋ぐまで起動しない** |
| kk05 | `5` | `hw:Device,0` | 外付け USB マイク（GeneralPlus）。Alcor カメラ側には capture PCM が無い |

`MIC_DEVICE` は `arecord -L` の出力で必ず確認する。
カード名で指定（`hw:Device,0`）しておくと、USB の挿し順で番号が変わっても追従する。

## 3. 購読側

```bash
# ダミヤン検出（damiyan-signal-processing）
uv run damiyan-detector --stream http://192.168.10.3:8770/5 -f frequencies.json

# 聴く / 録る
ffplay -nodisp http://192.168.10.3:8770/5
vlc http://192.168.10.3:8770/5
curl http://192.168.10.3:8770/5 -o cap.wav      # Ctrl-C で止める
```

ブラウザなら `http://192.168.10.3:8770/` を開けば一覧から試聴できる。

## 4. 動作確認

```bash
# ハブが生きているか
curl -s http://192.168.10.3:8770/healthz

# どの号機が繋がっているか（level_dbfs で音が来ているかも分かる）
curl -s http://192.168.10.3:8770/api/status | python3 -m json.tool

# マイクなしで系全体を検証（ハブ + 模擬号機 3 台を立てて自動判定）
python3 tools/mic_selftest.py
```

## トラブルシュート

| 症状 | 見るところ |
|---|---|
| 号機が offline のまま | 号機側で `journalctl -u mic-publisher -f`。`arecord` のエラーがそのままログに出る |
| `arecord: main:830: audio open error: No such file or directory` | `MIC_DEVICE` が違う。`arecord -L` で確認 |
| `arecord` がデバイスを 1 つも見つけない | `kk` が `audio` グループに居ない。`sudo usermod -aG audio kk` して再ログイン |
| `409 Conflict` がログに繰り返し出る | 二重起動。systemd と master_control の両方から起動していないか確認 |
| `rate mismatch` で `400` | 送信側とハブの `SAMPLE_RATE` 不一致。両方を揃える |
| 購読すると `503` | その号機の送信が繋がっていない。`/api/status` で確認 |
| 音は来るが `level_dbfs` が -120 のまま | マイクがミュート/ゲイン 0。号機で `alsamixer -c 1` |
| `listener ... too slow — dropped` | 購読側が 6.4 秒ぶん詰まった。購読側の CPU かネットワークを疑う |
| `audio/wall drift` の WARNING | 号機の CPU 不足か ALSA の overrun。`MIC_CAPTURE_RATE=16000` を試す |
| 録音でディスクが埋まる | `/etc/default/mic-hub` の `MIC_HUB_RETENTION_HOURS` と `MIC_HUB_MAX_GB` を下げる |
| kkrtx 再起動でハブが消えた | `systemctl is-enabled mic-hub` を確認。enable し忘れ |

### numpy を入れられない号機

`MIC_CAPTURE_RATE=16000` にすると ALSA 側で 16 kHz に変換され、numpy は不要になる。
ただし ALSA のリサンプラはビルドによって線形補間で、16 kHz 超の成分が
可聴帯へ折り返す。ダミヤンの周波数判定の精度が落ちるので、あくまで応急処置。

## ネットワーク

号機は `192.168.10.13N`（内蔵 wlan0）または `.11N`（USB ドングル）で、
どちらに載っているかは状況で変わる。
**この構成では号機側から push するので、ハブは号機の IP を知らなくてよい。**
号機が把握すべきなのはハブの `192.168.10.3` だけで、これは固定。

kkrtx でポートを開ける必要がある場合:

```bash
sudo ufw allow 8770/tcp        # ufw を使っている場合のみ
```
