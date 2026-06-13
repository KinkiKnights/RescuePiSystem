# 音声通信システム (voice_comm)

操作画面の各モード間で音声をやり取りするプッシュ・トゥ・トーク(PTT)方式の
音声通信システム。操作画面用サーバー(main.py)とは独立して動作する。

## 構成

| ファイル | 役割 |
|---|---|
| `server.py` | 音声中継サーバー(単体・ポート8766・最大5端末) |
| `voice-client.js` | ブラウザ用クライアントライブラリ(各モードから呼び出す) |

## 起動方法

```
pip install -r requirements.txt
py -3.10 server.py
```

操作画面サーバー(main.py)とは別プロセスとして起動する。

## 各モードへの組み込み

main.py が `voice_comm/` を `/voice` として配信しているので、HTMLに
以下を追加するだけでよい(全モード組み込み済み):

```html
<script src="/voice/voice-client.js"></script>
<script>VoiceComm.init({ role: "操縦" });</script>
```

## 操作方法

- **スペースキーを押している間**だけマイクの音声が他の端末へ送信される
- 受信した音声は自動で再生される
- 画面右下のインジケータに接続状態・送話中・受話中(誰が話しているか)が表示される
- 6台目の接続は「満員」として拒否される

## 注意事項

- ブラウザのマイク利用は **localhost または HTTPS でのみ許可** される。
  他端末から `http://<IPアドレス>:8765/...` で開く場合、Chrome では
  `chrome://flags/#unsafely-treat-insecure-origin-as-secure` に
  `http://<IPアドレス>:8765` と `http://<IPアドレス>:8766` を登録して
  Enabled にすると使用できる(撮影用途のローカルネットワーク前提)。
- 音声は 16kHz / モノラル / Int16 PCM をWebSocketで中継するシンプルな方式。

## プロトコル

- 接続: `ws://<host>:8766/voice?role=<表示名>`
- バイナリ(クライアント→サーバー): Int16LE PCM (16kHz mono)
- バイナリ(サーバー→クライアント): 先頭1バイト=送信元ID + PCM
- テキスト: `welcome` / `join` / `leave` / `talk` / `full` のJSONメッセージ
