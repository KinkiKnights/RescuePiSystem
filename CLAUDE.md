# 作業規約 (RescuePiSystem)

## このリポジトリの前提

号機 (Raspberry Pi) と運用サーバ (kkrtx) と操作画面を **1 リポジトリで運用する**。
2026-08 に 4 リポジトリを統合した経緯は [README.md](README.md) を参照。

## 乖離防止のルール

1. **各コンポーネントの実体はただ 1 つ。** コピーを別ディレクトリに置かない。
   共有したいファイルは実体を 1 つにして配信側で参照する
   （例: WebRTC クライアント JS は `server/webrtc_relay/web/webrtc-camera.js` が実体で、
   control_ui は `/static/webrtc-camera.js` としてその実体を返す）。
2. **プロトコルを変えたら対向も同じコミットで直す。** mic (publisher ⇔ hub)、
   WebRTC (publisher ⇔ relay ⇔ ブラウザ)、joy (control_ui ⇔ joy_node_web)、
   master_control API (control_ui ⇔ 号機) はすべて同一リポジトリ内にある。
   契約は `docs/protocols/` に書く。
3. **外部にあるのは 3 つだけ**: `joy_node_web`(submodule)、`ros2_socketcan`(`.repos` 参照)、
   `damiyan-signal-processing`(契約は `docs/protocols/damiyan.md`)。増やさない。
4. **systemd ユニットは `deploy/systemd/*.service.in` が単一の真実。**
   実 `.service` はコミットせず、`install_unit`（`deploy/systemd/install_unit.sh`）で
   展開・設置する。スクリプト内にユニット本文を書き写さない。
5. **アドレス・ポートは `config/units.json`。** コードやスクリプトに IP を直書きしない。
   **号機 ID は `KK0N` に統一**（`units[n].pi_id` / camera_publisher の `PI_ID` /
   操作画面が購読する ID / relay の `/pis` がすべて同じ文字列になる）。
   `RES0N` や `PI0N` といった別系統の ID を新たに作らない。
6. **号機ごとの運用値は `/etc/default/*`。** リポジトリ内の設定ファイルに書くと
   `git pull` で差分が巻き戻る（`programs.json` で実際に起きた）。

## 変更するときの目印

- 号機だけに影響する変更 → `robot/` と `deploy/robot/`
- kkrtx だけに影響する変更 → `server/` と `deploy/server/`
- 両方に影響する変更（プロトコル・アドレス）→ `config/` か `docs/protocols/` も更新する

## 動作確認

```bash
python3 tools/mic_selftest.py                       # mic 系（実機なしで通る）
python3 -c "import sys;sys.path.insert(0,'server/control_ui');import main"   # 設定読み込み
bash -n deploy/robot/*.sh deploy/server/*.sh        # スクリプトの構文
```
