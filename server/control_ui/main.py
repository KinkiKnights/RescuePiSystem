"""レスキューロボコン 汎用ツール — FastAPI サーバー

号機まわりの汎用ツールだけを持つ。競技（Res26）の操作画面と状態管理は
res26_control_ui（別リポジトリ・ポート 8001）へ分離した。
ここに大会固有のロジックを足さないこと。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# 設定はリポジトリ直下の config/ に集約されている（号機アドレスの単一の真実）。
REPO_ROOT = BASE_DIR.parents[1]
CONFIG_PATH = Path(os.environ.get("RESCUE_UNITS_CONFIG") or REPO_ROOT / "config" / "units.json")
# ping 監視対象は units.json から導出した既定値を使う。UI から編集した内容だけを
# 下記の override ファイルへ保存する（git 管理外。既定値は常に units.json 由来）。
PING_CONFIG_PATH = Path(
    os.environ.get("RESCUE_PING_CONFIG") or REPO_ROOT / "config" / "ping_devices.override.json"
)
# 自己署名証明書はリポジトリ直下の certs/ に置き、control_ui と voice_comm で共有する
# （make_cert.py が生成。git 管理外）。
CERT_DIR = Path(os.environ.get("RESCUE_CERT_DIR") or REPO_ROOT / "certs")


def load_units_config() -> dict[str, Any]:
    """config/units.json を読み込む（号機・機器アドレスの単一の真実）。

    無い/壊れている場合は警告を出し、空設定で起動を継続する（従来と同じ方針）。
    """
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {CONFIG_PATH} が見つかりません。号機アドレスは空で起動します。")
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {CONFIG_PATH} を読み込めませんでした（{exc}）。号機アドレスは空で起動します。")
        return {}
    if not isinstance(data, dict):
        print(f"[WARN] {CONFIG_PATH} が不正な形式です。号機アドレスは空で起動します。")
        return {}
    return data


# units.json は起動時に 1 度だけ読む（運用上の固定設定。クライアントからは変更不可）。
UNITS_CONFIG = load_units_config()


# ===== 号機 宛先マルチパス化（候補アドレス多重化＋live経路の自動選択） =====
# units[n].addrs: 号機ごとの「優先順の候補アドレス配列」。並び順がそのまま優先順で、
#   192.168.10.11N(無線/最優先) > 192.168.10.13N(調整用WiFi) >
#   192.168.10.12N(有線) > kk0N.local(mDNSフォールバック)。
# 号機側は全経路 0.0.0.0 で待受＝どの候補でも応答する前提。
RESOLVE_DEFAULTS: dict[str, Any] = {
    "probe_interval_sec": 3.0,
    "probe_timeout_sec": 1.0,
    "sticky": True,
}


def _addr_str(entry: Any) -> str:
    """addrs の 1 要素を宛先文字列にする（{"ip":…} / {"host":…} / 素の文字列）。"""
    if isinstance(entry, dict):
        return str(entry.get("ip") or entry.get("host") or "").strip()
    return str(entry).strip()


def load_unit_addrs() -> dict[int, list[str]]:
    """units.json の units[n].addrs を号機→優先順候補配列で読み込む。"""
    addrs: dict[int, list[str]] = {i: [] for i in range(1, 6)}
    raw = UNITS_CONFIG.get("units", {})
    if not isinstance(raw, dict):
        print(f"[WARN] {CONFIG_PATH} の units が不正な形式です。号機アドレスは空で起動します。")
        return addrs
    for k, v in raw.items():
        try:
            n = int(k)
        except (ValueError, TypeError):
            continue
        if not (1 <= n <= 5) or not isinstance(v, dict):
            continue
        cands: list[str] = []
        for a in v.get("addrs", []) if isinstance(v.get("addrs"), list) else []:
            sv = _addr_str(a)
            if sv and sv not in cands:
                cands.append(sv)
        addrs[n] = cands
    return addrs


def load_resolve_config() -> dict[str, Any]:
    """units.json の resolve 設定（probe 間隔・タイムアウト・sticky）を読み込む。"""
    cfg = dict(RESOLVE_DEFAULTS)
    raw = UNITS_CONFIG.get("resolve", {})
    if isinstance(raw, dict):
        try:
            cfg["probe_interval_sec"] = (
                float(raw.get("probe_interval_sec", cfg["probe_interval_sec"]))
                or cfg["probe_interval_sec"]
            )
        except (TypeError, ValueError):
            pass
        try:
            cfg["probe_timeout_sec"] = (
                float(raw.get("probe_timeout_sec", cfg["probe_timeout_sec"]))
                or cfg["probe_timeout_sec"]
            )
        except (TypeError, ValueError):
            pass
        cfg["sticky"] = bool(raw.get("sticky", cfg["sticky"]))
    return cfg


def _route_type(addr: str | None) -> str | None:
    """採用アドレスから経路種別ラベルを判定する（API 表示用）。"""
    if not addr:
        return None
    if addr.endswith(".local"):
        return "mdns"
    if addr.startswith("192.168.10.11"):
        return "11x"
    if addr.startswith("192.168.10.13"):
        return "13x"
    if addr.startswith("192.168.10.12"):
        return "wired"
    return "other"


class UnitRouteResolver:
    """号機ごとの候補アドレスから live 経路を選び、現用アドレスを保持する経路選択器。

    - _units_ping_loop が候補を優先順に probe し、その結果で状態を更新する。
    - sticky=True の間は現用アドレスが live な限り切替えない（無駄な再接続を避ける）。
    - resolve(n) は現在採用中の live アドレス（無ければ None）を返す単一解決関数。
    """

    def __init__(self, unit_addrs: dict[int, list[str]], sticky: bool) -> None:
        self.unit_addrs = unit_addrs
        self.sticky = sticky
        # 現用アドレス（live 経路が無い号機は None）
        self.current: dict[int, str | None] = {n: None for n in unit_addrs}
        # 候補ごとの生死（True/False/None=未確認）
        self.liveness: dict[int, dict[str, bool | None]] = {
            n: {a: None for a in cands} for n, cands in unit_addrs.items()
        }
        # 号機×候補ごとの連続 live 回数（上位への昇格ヒステリシス用）
        self._live_streak: dict[int, dict[str, int]] = {n: {} for n in unit_addrs}
        self.updated: float = 0.0

    def choose(self, n: int, live: dict[str, bool]) -> str | None:
        """probe 結果(live: 候補→bool)から現用アドレスを決める。

        候補は優先順(先頭=最優先)。sticky=True でも上位候補が復活したら昇格する。
        - 現用が dead（未設定含む）→ 優先順で最上位 live を即採用（従来どおり・即時）。
        - 現用が live → より上位に「2連続 probe で live」な候補があればその最上位へ昇格。
          spurious な 1 回の live で飛びつかないためのヒステリシス（ダウン方向は即時）。
        - 現用が既に最優先 live／上位に昇格対象が無ければ維持。全 dead なら None。
        """
        cands = self.unit_addrs.get(n, [])
        cur = self.current.get(n)

        # 連続 live 回数を更新（live で +1、dead で 0）。上位昇格の条件に使う。
        streak = self._live_streak.setdefault(n, {})
        for a in cands:
            streak[a] = streak.get(a, 0) + 1 if live.get(a) else 0

        # sticky 無効時は従来どおり優先順で最上位 live を即採用（張り付き無し）。
        if not self.sticky:
            for a in cands:
                if live.get(a):
                    return a
            return None

        # 現用が live でない（未設定/dead）→ 優先順で最上位 live へ即フェイルオーバー。
        if not (cur and live.get(cur)):
            for a in cands:
                if live.get(a):
                    return a
            return None

        # 現用は live。より上位に「2連続 live」候補があれば最上位へ昇格する。
        cur_idx = cands.index(cur) if cur in cands else len(cands)
        for a in cands[:cur_idx]:
            if live.get(a) and streak.get(a, 0) >= 2:
                return a
        return cur  # 昇格対象なし → 現用維持

    def resolve(self, n: int) -> str | None:
        """号機 n の現在採用中の live アドレスを返す（無ければ None）。"""
        return self.current.get(n)


# 号機→優先順候補配列（units.json 由来・固定設定）と経路選択器の実体
UNIT_ADDRS = load_unit_addrs()
# 号機ごとの代表 IP。候補配列の先頭から導出する派生値で、live 経路が決まるまでの
# 初期値として使う（設定ファイルに単一 IP は持たない）。
UNIT_IPS: dict[int, str] = {n: (a[0] if a else "") for n, a in UNIT_ADDRS.items()}
RESOLVE_CONFIG = load_resolve_config()
resolver = UnitRouteResolver(UNIT_ADDRS, bool(RESOLVE_CONFIG["sticky"]))


def resolve_unit_addr(n: int) -> str | None:
    """号機 n の現用 live アドレスを返す単一解決関数（全宛先参照はこれを経由）。"""
    return resolver.resolve(n)


# ===== Ping 監視（ネットワーク機器の死活監視。旧 ping-monitor から統合） =====
PING_DEFAULTS: dict[str, Any] = {"interval_sec": 5, "ping_timeout_sec": 1, "devices": []}


def default_ping_devices() -> list[dict[str, str]]:
    """units.json から ping 監視対象の既定リストを導出する。

    各号機の IP を持つ候補アドレス（mDNS 名は ping 対象にしない）を
    「N号機 <label>」として並べ、続いて infra（オペレータ端末・サーバ等）を並べる。
    アドレスの定義が units.json の 1 箇所に集約されるため、機器表の二重管理が無くなる。
    """
    devices: list[dict[str, str]] = []
    units = UNITS_CONFIG.get("units", {})
    if isinstance(units, dict):
        for k in sorted(units, key=lambda x: (len(str(x)), str(x))):
            v = units[k]
            if not isinstance(v, dict):
                continue
            for a in v.get("addrs", []) if isinstance(v.get("addrs"), list) else []:
                if not isinstance(a, dict):
                    continue
                ip = str(a.get("ip", "")).strip()
                if not ip:            # mDNS など IP を持たない候補は ping しない
                    continue
                label = str(a.get("label", "")).strip()
                devices.append({"name": f"{k}号機 {label}".strip(), "ip": ip})
    infra = UNITS_CONFIG.get("infra", [])
    if isinstance(infra, list):
        for d in infra:
            if not isinstance(d, dict):
                continue
            ip = str(d.get("ip", "")).strip()
            if ip:
                devices.append({"name": str(d.get("name", "")).strip(), "ip": ip})
    return devices


def load_ping_config() -> dict[str, Any]:
    """ping 監視設定を読み込む。

    既定は units.json 由来（間隔・タイムアウトは `ping` セクション、対象は
    `default_ping_devices()` で導出）。UI から編集された場合のみ override
    ファイル（PING_CONFIG_PATH・git 管理外）が存在し、そちらを優先する。
    """
    cfg: dict[str, Any] = dict(PING_DEFAULTS)
    raw_defaults = UNITS_CONFIG.get("ping", {})
    if isinstance(raw_defaults, dict):
        for key, fallback in (("interval_sec", 5), ("ping_timeout_sec", 1)):
            try:
                cfg[key] = float(raw_defaults.get(key, fallback)) or fallback
            except (TypeError, ValueError):
                cfg[key] = fallback
    cfg["devices"] = default_ping_devices()

    try:
        with PING_CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg                      # override 無し = units.json の既定値で運用
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {PING_CONFIG_PATH} を読み込めませんでした（{exc}）。units.json の既定値を使います。")
        return cfg

    if not isinstance(data, dict):
        print(f"[WARN] {PING_CONFIG_PATH} が不正な形式です。units.json の既定値を使います。")
        return cfg

    try:
        cfg["interval_sec"] = float(data.get("interval_sec", cfg["interval_sec"])) or cfg["interval_sec"]
    except (TypeError, ValueError):
        pass
    try:
        cfg["ping_timeout_sec"] = float(data.get("ping_timeout_sec", cfg["ping_timeout_sec"])) or cfg["ping_timeout_sec"]
    except (TypeError, ValueError):
        pass

    raw_devices = data.get("devices")
    if isinstance(raw_devices, list):
        devices: list[dict[str, str]] = []
        for d in raw_devices:
            if isinstance(d, dict):
                ip = str(d.get("ip", "")).strip()
                if ip:
                    devices.append({"name": str(d.get("name", "")).strip(), "ip": ip})
        cfg["devices"] = devices
    return cfg


def validate_ping_config(cfg: Any) -> dict[str, Any]:
    """クライアントから受け取った ping 設定を検証・正規化する。不正なら ValueError。"""
    if not isinstance(cfg, dict):
        raise ValueError("オブジェクト形式ではありません")
    try:
        interval = float(cfg.get("interval_sec", 5))
        timeout = float(cfg.get("ping_timeout_sec", 1))
    except (TypeError, ValueError):
        raise ValueError("interval_sec / ping_timeout_sec は数値で指定してください")
    if not (1 <= interval <= 3600):
        raise ValueError("interval_sec は 1〜3600 の範囲で指定してください")
    if not (1 <= timeout <= 60):
        raise ValueError("ping_timeout_sec は 1〜60 の範囲で指定してください")

    devices_in = cfg.get("devices", [])
    if not isinstance(devices_in, list):
        raise ValueError("devices は配列で指定してください")
    devices: list[dict[str, str]] = []
    for i, d in enumerate(devices_in):
        if not isinstance(d, dict):
            raise ValueError("devices[%d] がオブジェクトではありません" % i)
        ip = str(d.get("ip", "")).strip()
        if not ip:
            raise ValueError("devices[%d] の ip が空です" % i)
        devices.append({"name": str(d.get("name", "")).strip(), "ip": ip})

    return {
        "interval_sec": interval if interval % 1 else int(interval),
        "ping_timeout_sec": timeout if timeout % 1 else int(timeout),
        "devices": devices,
    }


def save_ping_config(cfg: dict[str, Any]) -> None:
    """UI から編集された ping 設定を override ファイルへ保存する（上書き）。

    units.json（配布物）は書き換えない。override を削除すれば既定値へ戻る。
    """
    PING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PING_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


class PingState:
    """最新の ping 結果キャッシュ。バックグラウンドタスクが更新し、API が配信する。"""

    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = []  # [{name, ip, online(bool|None)}]
        self.updated: float = 0.0
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {"devices": [dict(d) for d in self.devices], "updated": self.updated}


ping_state = PingState()


async def _ping_once(ip: str, timeout_sec: float) -> bool:
    """ip に 1 回 ping を送り、応答があれば True。OS 差異を吸収（macOS/Linux/Windows）。"""
    system = platform.system().lower()
    if system == "windows":
        # Windows: -n 回数, -w タイムアウト(ミリ秒)
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), ip]
    elif system == "darwin":
        # macOS: -c 回数, -W タイムアウト(ミリ秒), -t 全体タイムアウト(秒)
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_sec * 1000)),
               "-t", str(max(1, int(timeout_sec))), ip]
    else:
        # Linux 等: -c 回数, -W タイムアウト(秒)
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_sec))), ip]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return False
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout_sec + 2)
        return rc == 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    except Exception:
        return False


async def _ping_loop() -> None:
    """各機器を interval 秒周期でまとめて ping し、結果を ping_state にキャッシュする。

    イベントループをブロックしないよう asyncio サブプロセスで並行実行する。
    ping 設定は毎周期読み直すため、設定変更（POST）は次周期で反映される。
    """
    while True:
        cfg = load_ping_config()
        interval = float(cfg["interval_sec"])
        timeout = float(cfg["ping_timeout_sec"])
        devices = cfg["devices"]

        # まだ ping していない機器は online=None（確認中）として即時に反映する。
        async with ping_state.lock:
            prev = {(d["ip"], d["name"]): d.get("online") for d in ping_state.devices}
            ping_state.devices = [
                {"name": d["name"], "ip": d["ip"],
                 "online": prev.get((d["ip"], d["name"]))}
                for d in devices
            ]
            ping_state.updated = time.time()

        if devices:
            results = await asyncio.gather(
                *[_ping_once(d["ip"], timeout) for d in devices]
            )
            async with ping_state.lock:
                ping_state.devices = [
                    {"name": d["name"], "ip": d["ip"], "online": bool(ok)}
                    for d, ok in zip(devices, results)
                ]
                ping_state.updated = time.time()

        await asyncio.sleep(max(1.0, interval))


# ===== 号機 接続状況の実 Ping 監視（units[n].connected を実測で更新） =====
UNITS_PING_INTERVAL_SEC = 5.0
UNITS_PING_TIMEOUT_SEC = 1.0


async def _ping_or_false(ip: str, timeout_sec: float) -> bool:
    """IP が空なら False、そうでなければ _ping_once の結果を返す。"""
    if not ip:
        return False
    return await _ping_once(ip, timeout_sec)

async def _units_ping_loop() -> None:
    """各号機の候補アドレスを優先順に probe し、live 経路を自動選択する経路選択器。

    - 各号機の候補配列を並行 probe(probe_timeout_sec)して生死表を作り、resolver で
      現用アドレスを決める（sticky=true の間は現用が live な限り切替えない）。
    - 結果は resolver に持たせ、/api/units がそれを返す。
    - ループ間隔は resolve.probe_interval_sec、probe タイムアウトは probe_timeout_sec。
    """
    interval = float(RESOLVE_CONFIG["probe_interval_sec"])
    timeout = float(RESOLVE_CONFIG["probe_timeout_sec"])
    while True:
        units = sorted(UNIT_ADDRS)
        probe_jobs: list[tuple[int, str]] = [
            (n, a) for n in units for a in UNIT_ADDRS[n]
        ]
        probe_results = await asyncio.gather(
            *[_ping_or_false(a, timeout) for _, a in probe_jobs]
        )
        live_by_unit: dict[int, dict[str, bool]] = {n: {} for n in units}
        for (n, a), ok in zip(probe_jobs, probe_results):
            live_by_unit[n][a] = bool(ok)

        for n in units:
            live = live_by_unit[n]
            resolver.current[n] = resolver.choose(n, live)
            resolver.liveness[n] = {a: live.get(a) for a in UNIT_ADDRS[n]}
        resolver.updated = time.time()
        await asyncio.sleep(max(1.0, interval))



VOICE_DIR = BASE_DIR.parent / "voice_comm"   # server/voice_comm（隣のディレクトリ）

VOICE_DIR = BASE_DIR.parent / "voice_comm"   # server/voice_comm（隣のディレクトリ）

app = FastAPI(title="Rescue Generic Tools")



# WebRTC クライアントライブラリは relay と同じ実体を配信する（複製を持たない）。
# 実体は server/webrtc_relay/web/webrtc-camera.js（relay の web/ と同一ファイル）。
# /static のマウントより先に登録することで、このパスだけ実体へ振り向ける。
WEBRTC_CLIENT_JS = REPO_ROOT / "server" / "webrtc_relay" / "web" / "webrtc-camera.js"


@app.get("/static/webrtc-camera.js")
async def webrtc_client_js() -> FileResponse:
    """relay 側にある WebRTC クライアント JS をそのまま返す。"""
    return FileResponse(WEBRTC_CLIENT_JS, media_type="application/javascript")


# 号機マイクの集約ハブ（mic_hub）と WebRTC ビュワー（relay 同梱）の画面も、この
# 8000 番から配信する。運用時に人が開く URL を一本化するため。実体のディレクトリを
# そのままマウントし、ファイルの複製は持たない（CLAUDE.md 規約 1）。
#
# 配信するのは「画面」だけで、データ接続はプロキシしない。ブラウザは relay の
# シグナリング（ws://…:8080/ws）や mic hub の状態・音声（http://…:8770/…）へ
# 直接つなぐ。中継を挟まないので、PTT 音声や映像の経路に段が増えない。
# 接続先のポートは config/units.json が単一の真実（規約 5）で、下の
# /static/endpoints.js がそれを JS へ渡す。
RELAY_WEB_DIR = REPO_ROOT / "server" / "webrtc_relay" / "web"
MIC_HUB_STATIC_DIR = REPO_ROOT / "server" / "mic_hub" / "static"

# Res26 コントロールシステム（別リポジトリ res26_control_ui）の待受ポート。
# 8000/80・relay(8080)・voice(8766)・mic hub(8770)・joy_node(8700) と衝突しない
# 番号を固定で使う。あちらの main.py も同じ 8001 を固定している。
RES26_PORT = 8001

_ENDPOINT_KEYS = ("control_ui_port", "relay_port", "voice_port", "mic_hub_port")


def server_endpoints() -> dict[str, Any]:
    """units.json の server.* のうちポートだけを返す。

    ホスト名は返さない。ブラウザは自分が開いているホスト（location.hostname）の
    ポートだけ差し替えれば各サービスへ届くので、IP を JS へ渡す必要が無い。

    res26_port だけは units.json 由来ではない。Res26 コントロールシステムは
    kkrtx にしか無い大会固有アプリで、号機側は知る必要がないため units.json
    （フリート共通のポート表）には書かず、下の RES26_PORT を単一の真実とする。
    """
    cfg = UNITS_CONFIG.get("server") or {}
    endpoints = {key: cfg[key] for key in _ENDPOINT_KEYS if key in cfg}
    endpoints["res26_port"] = RES26_PORT
    return endpoints


@app.get("/api/endpoints")
async def get_endpoints() -> dict[str, Any]:
    """各サービスの待受ポート（units.json 由来）。"""
    return server_endpoints()


@app.get("/static/endpoints.js")
async def endpoints_js() -> Response:
    """ポート表を JS のグローバルへ流し込む小さなシム。

    8000 から配信されるページはこれを読んでから relay / mic hub へ直接つなぐ。
    ポート番号を JS 側に書き写さないための経路。/static のマウントより先に
    登録することで、このパスだけ生成結果へ振り向ける。
    """
    body = "window.RESCUE_ENDPOINTS = " + json.dumps(server_endpoints(), ensure_ascii=False) + ";\n"
    return Response(content=body, media_type="application/javascript")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ping-monitor")
async def ping_monitor_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "ping-monitor.html")


@app.get("/all-monitor")
async def all_monitor_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "all-monitor.html")


@app.get("/control-panel")
async def control_panel_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "control-panel.html")


@app.get("/grid")
async def grid_page() -> FileResponse:
    """全号機のカメラをタイル表示するグリッド視聴画面。"""
    return FileResponse(STATIC_DIR / "grid.html")

@app.on_event("startup")
async def _start_ping_monitor() -> None:
    # ping 監視のバックグラウンドタスクを起動する。
    asyncio.create_task(_ping_loop())
    # 号機の経路解決（/api/units が返す live 経路）を実 ping で更新するタスク。
    asyncio.create_task(_units_ping_loop())



@app.get("/api/units")
async def get_units() -> dict[str, Any]:
    """各号機の採用アドレス・接続状態・候補生死・経路種別を返す（マルチパス経路の可視化）。

    - addr: 現在採用中の live アドレス（live 経路が無ければ null）。
    - connected: 採用アドレスの有無（＝いずれかの経路が live か）。
    - route_type: 採用経路の種別（11x=無線 / 13x=調整用WiFi / wired=有線 / mdns）。
    - candidates: 優先順の候補ごとに {addr, up(true/false/null=未確認), route_type}。
    - pi_id: units.json の pi_id（KK0N）。号機 ID の単一の真実。
    """
    cfg_units = UNITS_CONFIG.get("units") or {}
    units: dict[str, Any] = {}
    for n in sorted(UNIT_ADDRS):
        chosen = resolver.current.get(n)
        live = resolver.liveness.get(n, {})
        units[str(n)] = {
            "unit": n,
            "pi_id": (cfg_units.get(str(n)) or {}).get("pi_id"),
            "addr": chosen,
            "connected": chosen is not None,
            "route_type": _route_type(chosen),
            "candidates": [
                {"addr": a, "up": live.get(a), "route_type": _route_type(a)}
                for a in UNIT_ADDRS[n]
            ],
        }
    return {
        "units": units,
        "updated": resolver.updated,
        "sticky": resolver.sticky,
        "probe_interval_sec": RESOLVE_CONFIG["probe_interval_sec"],
        "probe_timeout_sec": RESOLVE_CONFIG["probe_timeout_sec"],
    }


@app.get("/api/ping/status")
async def get_ping_status() -> dict[str, Any]:
    """各機器の最新 ping 結果（online: true/false/null）をまとめて返す。"""
    async with ping_state.lock:
        return ping_state.snapshot()


@app.get("/api/ping/config")
async def get_ping_config() -> dict[str, Any]:
    """現在の ping 監視設定（間隔・タイムアウト・機器一覧）を返す。"""
    return load_ping_config()


@app.post("/api/ping/config")
async def post_ping_config(body: dict[str, Any]) -> Any:
    """ping 監視設定を保存する。次のポーリング周期から反映される。"""
    try:
        cfg = validate_ping_config(body)
    except (ValueError, KeyError) as exc:
        return JSONResponse(status_code=400, content={"error": f"設定が不正です: {exc}"})
    try:
        save_ping_config(cfg)
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return {"ok": True}


# ===== 号機の再起動 / シャットダウン（操作中の号機の master-control API を呼び出す） =====
MASTER_CONTROL_PORT = 80


def _master_control_power_sync(ip: str, action: str) -> tuple[bool, str]:
    """master-control の /system/reboot|shutdown へ POST する（同期・ブロッキング）。

    標準ライブラリ urllib で HTTP POST（空ボディ）を送る。master-control は
    `{ok: true, message: "Rebooting..."/"Shutting down..."}` を返してから実処理する。
    接続失敗・非2xx・タイムアウト時は (False, 理由) を返し、実機を落とさずに
    グレースフルへ失敗する。成功時は (True, 説明)。
    """
    endpoint = "shutdown" if action == "shutdown" else "reboot"
    url = f"http://{ip}:{MASTER_CONTROL_PORT}/system/{endpoint}"
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            body = resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as exc:
        return False, f"{url} が HTTP {exc.code} を返しました"
    except urllib.error.URLError as exc:
        return False, f"{ip} の master-control へ接続できません（{exc.reason}）"
    except Exception as exc:
        return False, f"master-control 呼び出しに失敗しました（{exc}）"
    if 200 <= status < 300:
        return True, f"{url} へ POST しました（HTTP {status}）: {body or '応答本文なし'}"
    return False, f"{url} が HTTP {status} を返しました: {body}"


async def _master_control_power(ip: str, action: str) -> tuple[bool, str]:
    """操作中の号機の master-control API を叩いて reboot/shutdown を要求する。

    IP 未設定時は (False, 理由) を返す（グレースフル）。HTTP 呼び出しはブロッキング
    のため asyncio.to_thread で別スレッド実行し、イベントループを塞がない。
    """
    if not ip:
        return False, "対象号機の IP が未設定です（config.json 要確認）"
    return await asyncio.to_thread(_master_control_power_sync, ip, action)


async def _parse_unit_from_request(request: Request) -> int:
    """リクエスト JSON ボディから対象号機を取り出す（無効・欠落時は 0）。

    機体選択はクライアントローカルのため、対象号機はクライアントが
    {"unit": n} で明示指定する。
    """
    try:
        body = await request.json()
        unit = int(body.get("unit", 0))
    except Exception:
        unit = 0
    return unit if 1 <= unit <= 5 else 0


async def _unit_power_action(action: str, unit: int) -> Any:
    """指定号機へ reboot/shutdown を試み、結果を JSON で返す。"""
    label0 = "シャットダウン" if action == "shutdown" else "再起動"
    if not (1 <= unit <= 5):
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "unit": unit,
                "message": f"{label0}に失敗: 対象号機が指定されていません（unit=1..5 を送信してください）",
            },
        )
    # 宛先はマルチパス経路選択器が採用中の live アドレス（無ければ空＝グレースフル失敗）。
    ip = resolve_unit_addr(unit) or ""
    ok, msg = await _master_control_power(ip, action)
    label = "シャットダウン" if action == "shutdown" else "再起動"
    if ok:
        # 状態バス（WebSocket 配信）は Res26 側（8001）へ移ったので、ここでは
        # 通知を配らず結果だけ返す。呼び出し元の画面が JSON を見て表示する。
        return {"status": "ok", "unit": unit, "message": msg}
    return JSONResponse(
        status_code=502,
        content={"status": "error", "unit": unit, "message": f"{label}に失敗: {msg}"},
    )


@app.post("/api/unit/reboot")
async def post_unit_reboot(request: Request) -> Any:
    """クライアントが指定した号機（body: {"unit": n}）を master-control API 経由で再起動する。"""
    unit = await _parse_unit_from_request(request)
    return await _unit_power_action("reboot", unit)


@app.post("/api/unit/shutdown")
async def post_unit_shutdown(request: Request) -> Any:
    """クライアントが指定した号機（body: {"unit": n}）を master-control API 経由でシャットダウンする。"""
    unit = await _parse_unit_from_request(request)
    return await _unit_power_action("shutdown", unit)


async def _units_power_all(action: str) -> Any:
    """全号機（units.json の 1..5）を master-control API 経由で一斉に reboot/shutdown する。

    各号機へ _master_control_power(ip, action) を asyncio.gather で並行に呼び出し、
    オフライン等で失敗した号機は個別にグレースフルへ倒して他号機の処理をブロックしない。
    号機ごとの結果を {"results": {"1": {"ok": true, "message": ...}, ...}} 形式で返す。
    """
    # 各号機の宛先はマルチパス経路選択器の採用アドレス（未 live は空＝個別にグレースフル）。
    targets = {unit: (resolve_unit_addr(unit) or "") for unit in sorted(UNIT_ADDRS)}

    async def _one(unit: int, ip: str) -> tuple[int, bool, str]:
        ok, msg = await _master_control_power(ip, action)
        return unit, ok, msg

    outcomes = await asyncio.gather(*[_one(u, ip) for u, ip in targets.items()])
    results: dict[str, Any] = {}
    success = 0
    for unit, ok, msg in outcomes:
        results[str(unit)] = {"ok": ok, "message": msg}
        if ok:
            success += 1
    total = len(outcomes)
    # 状態バス（WebSocket 配信）は Res26 側（8001）へ移ったので、ここでは通知を
    # 配らず結果だけ返す。呼び出し元の画面が JSON を見て表示する。
    return {"status": "ok", "action": action, "results": results, "success": success, "total": total}


@app.post("/api/units/shutdown_all")
async def post_units_shutdown_all() -> Any:
    """全号機を master-control API 経由で一斉シャットダウンする。"""
    return await _units_power_all("shutdown")


@app.post("/api/units/reboot_all")
async def post_units_reboot_all() -> Any:
    """全号機を master-control API 経由で一斉再起動する。"""
    return await _units_power_all("reboot")


def _ssl_args() -> dict[str, str]:
    """certs/cert.pem と certs/key.pem があれば HTTPS で起動する。

    iOS Safari は localhost 以外ではマイク利用に HTTPS(セキュアコンテキスト)が
    必須。自己署名証明書は `python make_cert.py` で生成できる（環境変数
    SSL_CERT / SSL_KEY でパス上書き可）。
    """
    import os

    cert = os.environ.get("SSL_CERT") or str(CERT_DIR / "cert.pem")
    key = os.environ.get("SSL_KEY") or str(CERT_DIR / "key.pem")
    if Path(cert).exists() and Path(key).exists():
        print(f"[HTTPS] {cert} / {key} を使用します（https://<host>/）")
        return {"ssl_certfile": cert, "ssl_keyfile": key}
    print("[HTTP] 証明書が無いため HTTP で起動します（iPhoneのマイクは不可）")
    return {}

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/voice", StaticFiles(directory=str(VOICE_DIR)), name="voice")
app.mount("/viewer", StaticFiles(directory=str(RELAY_WEB_DIR), html=True), name="viewer")
app.mount("/mic", StaticFiles(directory=str(MIC_HUB_STATIC_DIR), html=True), name="mic")


# ---- Res26 コントロールシステムへのリダイレクト ------------------------------
#  操作画面 5 モードは res26_control_ui（ポート 8001）へ移った。旧 URL を叩いた
#  ブックマークや手癖を拾って飛ばす。301 ではなく 302 なのは、ブラウザに恒久
#  キャッシュさせないため（大会ごとに構成が変わりうる）。
#  ホスト名はリクエストのものを使う。IP でも kkrtx.local でも同じように動く。
RES26_PAGES = ("control", "analytics", "engineer", "reporter", "master")


def _res26_url(request: Request, path: str) -> str:
    return f"http://{request.url.hostname}:{RES26_PORT}/{path}"


def _make_res26_redirect(path: str):
    async def _redirect(request: Request) -> RedirectResponse:
        return RedirectResponse(_res26_url(request, path), status_code=302)

    return _redirect


for _page in RES26_PAGES:
    app.add_api_route(f"/{_page}", _make_res26_redirect(_page), methods=["GET"])


if __name__ == "__main__":
    import uvicorn

    # ポートは units.json の server.control_ui_port（既定 80）。開発時は自動再読込
    # するが、常駐時は CONTROL_UI_RELOAD=0 で切る。
    _port = int(
        os.environ.get("CONTROL_UI_PORT")
        or (UNITS_CONFIG.get("server") or {}).get("control_ui_port")
        or 80
    )
    _reload = os.environ.get("CONTROL_UI_RELOAD", "1") not in ("0", "false", "no")
    uvicorn.run("main:app", host="0.0.0.0", port=_port, reload=_reload, **_ssl_args())
