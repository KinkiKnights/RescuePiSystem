# USB WiFi ドングルのセットアップ手順(RTL8811AU / BUFFALO WI-U2-433 系)

BUFFALO の USB WiFi ドングルを Raspberry Pi(Ubuntu 24.04 / ROS2 Jazzy 環境)で
使えるようにし、アクセスポイントへ接続するまでの手順。内蔵 WiFi(`wlan0`)とは
別の無線インターフェースを増設したいとき(例: 内蔵で母艦ネットに繋ぎつつ、ドングルで
現場の AP に接続)に使う。

- **検証環境**: Raspberry Pi 5 / Ubuntu 24.04 (Noble) / kernel `6.8.0-1057-raspi` / NetworkManager
- **対象ドングル**: BUFFALO WI-U2-433 系(`lsusb` ID `0411:029b`, チップ **Realtek RTL8811AU**)
- **接続先の例**: SSID `test` / WPA2-PSK

> 別の型番のドングルの場合は「[別のドングルの場合](#別のドングルの場合)」を参照。

---

## 0. 前提と注意

- **既存のネットワーク経路を壊さないこと。** SSH 等で作業している場合、その経路
  (例: `wlan0`)を落とすと切断される。本手順ではドングル側のルートメトリックを
  高く設定し、既定経路を奪わないようにする。
- インターネット接続(apt / git 用)が必要。内蔵 `wlan0` か有線で確保しておく。
- `sudo` 権限が必要。

---

## 1. ドングルの認識確認

```bash
lsusb | grep -i wlan
# 例) Bus 002 Device 002: ID 0411:029b BUFFALO INC. ... 802.11ac WLAN Adapter

ip -br link          # この時点では新しい wlanX は現れない(ドライバ未導入のため)
```

`0411:029b` が見えるのに `ip link` に無線インターフェースが増えない場合、
**メインラインカーネルに RTL8811AU 用ドライバが無い**ため、out-of-tree ドライバの
導入が必要(次項)。

> **⚠️ 落とし穴:** Ubuntu universe の `rtl8812au-dkms`(2014年版 v4.3.8)は **使わない**。
> RTL8811AU 非対応で、`new_id` で強制バインドすると **カーネルが oops する**。
> もし導入済みなら撤去する:
> ```bash
> sudo apt-get purge -y rtl8812au-dkms
> echo "blacklist 8812au" | sudo tee /etc/modprobe.d/blacklist-rtl8812au.conf
> ```

---

## 2. ドライバ(morrownr/8821au)を DKMS で導入

> **📌 自動化済み:** この項のドライバ導入は `setup/env_setup.sh`(手順 4。
> `SETUP_WIFI_DONGLE=1` を付けて実行した場合)が自動で行います。
> セットアップスクリプトを実行した Pi では本項の作業は不要で、
> 手順 3(AP への接続)から始めれば OK。以下は手動で行う場合と仕組みの説明。

RTL8811AU / RTL8821AU 用の、現行カーネルに対応した保守されているドライバを使う。
DKMS で入れておくと **カーネル更新時に自動で再ビルド**される。

```bash
# 2-1. ビルド環境と現行カーネルのヘッダを導入
sudo apt-get update
sudo apt-get install -y "linux-headers-$(uname -r)" dkms build-essential git iw

# 2-2. ドライバ取得(RTL8811AU/8821AU 対応。USB ID 0411:029b を内蔵)
git clone --depth 1 https://github.com/morrownr/8821au-20210708.git
cd 8821au-20210708

# 2-3. DKMS へ登録・ビルド・インストール(数分)
#      バージョンは dkms.conf の PACKAGE_VERSION と一致させる(この版は 5.12.5.2)
sudo dkms add ./
sudo dkms build  rtl8821au/5.12.5.2
sudo dkms install rtl8821au/5.12.5.2

# 2-4. 確認
dkms status | grep 8821          # -> rtl8821au/5.12.5.2, <kernel>, aarch64: installed
```

### モジュールのロード

```bash
sudo modprobe 8821au
```

以降はドングルを挿すと **udev が自動でロード**する(モジュールに
`usb:v0411p029B...` の alias が登録されるため)。手動 `modprobe` は初回のみでよい。

```bash
ip -br link         # 新しい無線 IF が出る(例: wlx002bf5142001。MAC ベースの命名)
```

> インターフェース名は MAC から決まる `wlxXXXXXXXXXXXX` になる。以降の手順では
> 自分の環境の名前に読み替える。名前は次で確認:
> ```bash
> DEV=$(ls /sys/class/net | grep -E '^wlx'); echo "$DEV"
> ```

---

## 3. アクセスポイントへ接続(NetworkManager)

内蔵 `wlan0` を主経路として残すため、ドングル側の **ルートメトリックを高く**(既定より
大きく)設定して既定経路を奪わせない。`wlan0` の既定メトリックは通常 600 なので 700 にする。

```bash
DEV=$(ls /sys/class/net | grep -E '^wlx')   # ドングルの IF 名

sudo nmcli connection add type wifi con-name dongle-test ifname "$DEV" ssid test \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk 12345678 \
  ipv4.route-metric 700 ipv6.route-metric 700 connection.autoconnect yes

sudo nmcli connection up dongle-test
```

- `ssid` / `wifi-sec.psk` は接続先に合わせて変更(この例は SSID `test` / パス `12345678`)。
- `connection.autoconnect yes` により、再起動後もドングルを挿していれば自動接続する。
- プロファイルは `/etc/NetworkManager/system-connections/dongle-test.nmconnection` に永続化される。

---

## 4. 接続確認

```bash
nmcli device status
#   wlx002bf5142001   wifi   connected   dongle-test   のようになる

ip -br addr show "$DEV"                 # ドングルに IP が付いているか
ping -I "$DEV" -c 3 <APのゲートウェイ>   # ドングル経由で疎通するか

ip route | grep default
#   default ... dev wlan0            metric 600   <- 主経路(内蔵)が優先
#   default ... dev wlx...           metric 700   <- ドングルは従
```

`0% packet loss` かつ `wlan0` が引き続き既定経路(metric 600)なら成功。

---

## 5. 永続性(再起動・カーネル更新後)

この構成は再現・永続化まで含めて自動化されている:

| 事象 | 挙動 |
|---|---|
| 再起動 | udev がドングル挿抜で `8821au` を自動ロード → NM が `dongle-test` を autoconnect |
| カーネル更新 | DKMS が新カーネル向けにドライバを自動再ビルド(`dkms status` で確認可) |
| ドングル抜き差し | modalias により自動で認識、インターフェース再生成 |

特別な `/etc/modules` への追記は不要。

---

## 別のドングルの場合

チップにより使うドライバが変わる。まず `lsusb` の ID でチップを特定する。

| チップ | 例 | ドライバ |
|---|---|---|
| RTL8811AU / RTL8821AU(1x1, 433Mbps) | 本手順 (`0411:029b`) | [morrownr/8821au](https://github.com/morrownr/8821au-20210708) |
| RTL8812AU / RTL8814AU(2x2 以上, 867Mbps+) | 多くの「AC1200」系 | [morrownr/8812au](https://github.com/morrownr/8812au-20210820) |
| RTL8188EUS / 8188FU / 8192EU 等 | 安価な 2.4GHz | morrownr の該当リポジトリ |
| rtw88 系(8821CU/8822BU/8822CU 等) | 比較的新しい | メインライン `rtw88`(ドライバ導入不要な場合あり) |

共通の注意:
- ヘッダは必ず**動作中のカーネルに一致**させる: `linux-headers-$(uname -r)`。
  (Raspberry Pi OS〔Debian〕系なら `sudo apt install raspberrypi-kernel-headers`)
- 導入したいドライバの `os_dep/linux/usb_intf.c` などに自分の USB ID があるか
  `grep` で確認してから使うと確実:
  ```bash
  grep -rni "029b" .    # 自分の ProductID に置き換える
  ```

---

## トラブルシューティング

- **`ip link` に IF が出ない**: `sudo dmesg | tail` を確認。`8821au ... renamed from wlanN`
  が出ていれば成功。何も出ない場合はドライバ未ロード → `sudo modprobe 8821au`。
- **カーネル oops / フリーズ**: 誤って `rtl8812au-dkms`(2014版)を入れていないか確認し、
  上記「落とし穴」の手順で撤去。
- **接続できるがネットに出られない**: それは AP 側の仕様(その AP にインターネットが
  無い)の可能性。`ping -I "$DEV" <ゲートウェイ>` で L2/L3 の疎通だけ先に確認する。
- **ドングル経由が既定経路になって主回線が変わってしまう**: `dongle-test` の
  `ipv4.route-metric` を主 IF より大きくする:
  ```bash
  sudo nmcli connection modify dongle-test ipv4.route-metric 700
  sudo nmcli connection up dongle-test
  ```
- **DKMS ビルド失敗**: ヘッダがカーネルと不一致のことが多い。
  `sudo apt-get install -y "linux-headers-$(uname -r)"` を再確認。

---

## この環境での実績値(参考)

- ドングル: BUFFALO `0411:029b`(RTL8811AU) → IF `wlx002bf5142001`
- ドライバ: `rtl8821au/5.12.5.2`(morrownr/8821au-20210708)、モジュール `8821au.ko`
- 接続先: SSID `test`(WPA2, ch6)→ 取得 IP `192.168.137.224/24`, GW `192.168.137.1`
- 主経路の内蔵 `wlan0`(metric 600)は維持、ドングルは metric 700 の従経路
