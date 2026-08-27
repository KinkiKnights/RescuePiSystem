# アーキテクチャ

RescuePiSystem は **3 種類のホスト**にまたがる 1 つのシステムを 1 リポジトリで持つ。
どこに何を置くかはディレクトリ名がそのまま示す（`robot/` = 号機、`server/` = kkrtx）。

## デプロイ先マップ

| ホスト | 実体 | 動くもの | セットアップ |
|---|---|---|---|
| 号機 Pi × 5 | Raspberry Pi 5 / Ubuntu 24.04 / ROS 2 Jazzy | `robot/` | `deploy/robot/kk_robot_setup.sh` |
| kkrtx | 運用サーバ（192.168.10.3） | `server/` | `deploy/server/kkrtx_setup.sh` |
| 操作端末 | PC / スマホのブラウザ | なし（kkrtx を見るだけ） | — |

外部にあるもの: `joy_node_web`(submodule)、`ros2_socketcan`(`.repos`)、
`damiyan-signal-processing`（kkrtx 上で動くが別リポジトリ。→ [protocols/damiyan.md](protocols/damiyan.md)）。

## データフロー

```
                号機 Pi (robot/)                        kkrtx (server/)                操作端末
  ┌─────────────────────────────┐        ┌────────────────────────────┐    ┌──────────────┐
  │ camera_publisher            │        │ webrtc_relay (SFU)  :8080  │    │              │
  │   GStreamer → WebRTC   ─────┼───push─┼──▶ /ws  ──── 映像 ────────┼───▶│ control      │
  │                             │        │                            │    │ analytics    │
  │ mic_publisher               │        │ mic_hub             :8770  │    │ engineer     │
  │   arecord → 16kHz PCM  ─────┼───push─┼──▶ /ingest/<号機>          │    │ reporter     │
  │                             │        │      └▶ /<号機> ──┬────────┼───▶│ master       │
  │ joy_node_web         :8700  │◀──ws───┼── control_ui  :80 │        │    │              │
  │   /joys → /joy, /goal_pose  │        │   状態一元管理 + WS       │    │              │
  │                             │        │      └▶ damiyan-detector   │    │              │
  │ master_control       :80    │◀──HTTP─┼── /system/reboot|shutdown │    │              │
  │   programs.json を起動/停止  │        │ voice_comm          :8766  │◀──▶│ PTT 音声     │
  │                             │        │                            │    │              │
  │ kk_can_bringup ──▶ CAN      │        └────────────────────────────┘    └──────────────┘
  └─────────────────────────────┘
```

要点:

- **状態は control_ui が一元保持**し、変更のたび全 WebSocket クライアントへ
  スナップショットをブロードキャストする。クライアント間の直接通信は無い
  （→ [protocols/state.md](protocols/state.md)）。
- **号機からサーバへは push**（カメラ・マイクとも）。号機の IP が変わっても
  サーバ側の設定は不要で、起動順にも依存しない。
- **サーバから号機へは pull/コマンド**（joy WebSocket、master_control の HTTP）。
  宛先は `config/units.json` の候補配列から live 経路を解決して決める。
- 音声だけ 2 系統ある。**号機のマイク**（mic_hub・解析用）と
  **オペレータ間の PTT**（voice_comm）は無関係な別経路。

## アドレス計画

`config/units.json` が唯一の真実。号機 N について:

| 用途 | アドレス | 優先順 |
|---|---|---|
| 無線（USB ドングル） | `192.168.10.11N` | 1 |
| 調整無線（内蔵 WiFi） | `192.168.10.13N` | 2 |
| 有線 | `192.168.10.12N` | 3 |
| mDNS | `kk0N.local` | 4（フォールバック） |

インフラ: `.1`/`.2` オペレータ端末、`.3` kkrtx、`.4`/`.5` エンジニア端末。
号機側は全経路 `0.0.0.0` で待受するので、どの候補でも応答する。control_ui が
候補を周期 probe し、`resolve.sticky` が true の間は現用経路が live なら維持する。

## ポート一覧

| ポート | プロセス | ホスト | 備考 |
|---|---|---|---|
| 80 | master_control | 号機 | `CAP_NET_BIND_SERVICE` で kk ユーザが bind |
| 8700 | joy_node_web | 号機 | `/joys`（WebSocket） |
| 80 | control_ui | kkrtx | `config/units.json` の `server.control_ui_port` |
| 8080 | webrtc_relay | kkrtx | `/ws`, `/pis` |
| 8766 | voice_comm | kkrtx | `/voice`（WebSocket） |
| 8770 | mic_hub | kkrtx | `/ingest/<号機>`(POST), `/<号機>`(GET) |
| 8771-8773 | damiyan-detector | kkrtx | 外部リポジトリ。`8768 + 号機` |

## 起動順

依存関係は緩く、どれから起動しても最終的に繋がる（push 側が再試行する）。
それでも素直な順番は:

1. **kkrtx**: `webrtc_relay` → `mic_hub` → `control_ui` → `voice_comm`
   （`kkrtx_setup.sh` が systemd で常時起動にする）
2. **号機**: `master-control.service` が自動起動 → Web UI から camera / joy / mic を起動
   （`programs.json` の `autostart` が true のものは master_control が自動で起動）
3. **damiyan-detector**: 報告者モードで周波数を入力した時点で control_ui が起動・再起動する

## 各コンポーネントの入口

| コンポーネント | 入口 | 詳しい文書 |
|---|---|---|
| master_control | `robot/master_control/master_server.py` | [robot.md](robot.md) |
| camera_publisher | `robot/camera_publisher/publish-pi5.sh` | [webrtc-camera.md](webrtc-camera.md) |
| mic_publisher | `robot/mic_publisher/mic_publisher.py` | [mic-system.md](mic-system.md) |
| joy_node_web | submodule（`ros2 run joy_node_web joy_node`） | [protocols/joy.md](protocols/joy.md) |
| kk_can_bringup | `robot/ros2/kk_can_bringup/launch/can_bridge.launch.xml` | [robot.md](robot.md) |
| control_ui | `server/control_ui/main.py` | [control-ui.md](control-ui.md) |
| voice_comm | `server/voice_comm/server.py` | `server/voice_comm/README.md` |
| mic_hub | `server/mic_hub/mic_hub.py` | [mic-system.md](mic-system.md) |
| webrtc_relay | `server/webrtc_relay/main.go` | [webrtc-camera.md](webrtc-camera.md) |
