#!/usr/bin/env python3
"""カメラ / マイクのデバイス設定 — 号機ごとの運用設定の単一の実装。

**なぜこのモジュールがあるか**

以前はデバイス指定が master_control の `programs.json` の `cmd` 文字列に
埋め込まれていた(`CAM1="v4l2src device=/dev/video0 …" publish-pi5.sh` のような
長い 1 行)。そのため

  - 変更手段が「長いコマンド行を Web UI で手編集」しかなかった
  - `/dev/video0` や `hw:1,0` という**番号固定**の指定なので、USB の抜き差しや
    起動順、カメラ/マイクの交換でデバイス番号がずれると映像や音が出なくなり、
    しかも UI からは原因が分からなかった

この 2 点を解消するため、設定を**構造化した JSON** に分離し、指定は
**安定した識別子**(カメラ = `/dev/v4l/by-id/...`、マイク = `hw:CARD=<名前>,DEV=0`)
で持つ。接続されているデバイスの列挙もここが担当し、master_control の UI は
「実際に繋がっている候補から選ぶ」だけでよくなる。

**設定ファイルの場所**(先に見つかったものを使う)

  1. 環境変数 `RESCUE_DEVICES_CONFIG`
  2. `~/.config/rescue-pi/devices.json`   ← 通常はここ (kk ユーザが書ける)
  3. `/etc/rescue-pi/devices.json`        ← 読み取りのフォールバック

いずれもリポジトリの外なので `git pull` で書き換わらない(号機固有の運用値は
リポジトリに置かない、という既存の方針どおり)。

**値の優先順**

    コマンドライン引数 > 環境変数 > devices.json > 自動検出

つまり systemd の `/etc/default/*` や programs.json で明示すればそれが勝ち、
何も指定しなければ UI で選んだ設定が効き、設定ファイルすら無ければ
接続されているデバイスから自動検出する。

**CLI**

    python3 robot/device_config.py --show     # 解決結果を表示
    python3 robot/device_config.py --list     # 接続されている候補を列挙
    python3 robot/device_config.py --init     # 設定ファイルが無ければ自動検出して作成
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import tempfile

SCHEMA_VERSION = 1

# ── 設定ファイルの探索 ────────────────────────────────────────────────
USER_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "rescue-pi", "devices.json",
)
SYSTEM_PATH = "/etc/rescue-pi/devices.json"


def config_path(for_write: bool = False) -> str:
    """使用する設定ファイルのパス。for_write=True なら書き込み先を返す。"""
    env = os.environ.get("RESCUE_DEVICES_CONFIG")
    if env:
        return env
    if for_write:
        return USER_PATH
    for p in (USER_PATH, SYSTEM_PATH):
        if os.path.exists(p):
            return p
    return USER_PATH


# ── 既定値 ────────────────────────────────────────────────────────────
DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "camera": {
        # kind: usb / csi / test / raw_pipeline
        "kind": "usb",
        "device": "",            # /dev/v4l/by-id/... (kind=usb のとき)
        "format": "mjpeg",       # mjpeg / raw
        "width": 1024,
        "height": 768,
        "framerate": 30,
        "pipeline": "",          # kind=raw_pipeline のときの GStreamer 文字列
    },
    "mic": {
        "device": "",            # hw:CARD=<名前>,DEV=0
        "capture_rate": 48000,   # 16000 の整数倍。16000 なら numpy 不要
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """設定を読む。無い/壊れている場合は既定値を返す(起動は妨げない)。"""
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _merge(DEFAULTS, {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[devices] {path} を読めませんでした({exc})。既定値を使います。", file=sys.stderr)
        return _merge(DEFAULTS, {})
    if not isinstance(data, dict):
        print(f"[devices] {path} が不正な形式です。既定値を使います。", file=sys.stderr)
        return _merge(DEFAULTS, {})
    return _merge(DEFAULTS, data)


def save(cfg: dict) -> str:
    """設定を書き込む(同一ディレクトリへ一時ファイル → rename で原子的に)。"""
    path = config_path(for_write=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = _merge(DEFAULTS, cfg)
    cfg["schema_version"] = SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".devices-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def validate(raw: object) -> dict:
    """UI から来た設定を検証・正規化する。不正なら ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("オブジェクト形式ではありません")
    cam_in = raw.get("camera") or {}
    mic_in = raw.get("mic") or {}
    if not isinstance(cam_in, dict) or not isinstance(mic_in, dict):
        raise ValueError("camera / mic はオブジェクトで指定してください")

    kind = str(cam_in.get("kind", "usb")).strip()
    if kind not in ("usb", "csi", "test", "raw_pipeline"):
        raise ValueError(f"camera.kind が不正です: {kind}")
    cam = {
        "kind": kind,
        "device": str(cam_in.get("device", "")).strip(),
        "format": str(cam_in.get("format", "mjpeg")).strip() or "mjpeg",
        "pipeline": str(cam_in.get("pipeline", "")).strip(),
    }
    if cam["format"] not in ("mjpeg", "raw"):
        raise ValueError(f"camera.format が不正です: {cam['format']}")
    if kind == "usb" and not cam["device"]:
        raise ValueError("USB カメラを選ぶ場合は device が必要です")
    if kind == "usb" and not cam["device"].startswith("/dev/"):
        raise ValueError("camera.device は /dev/ 以下のパスで指定してください")
    if kind == "raw_pipeline" and not cam["pipeline"]:
        raise ValueError("raw_pipeline を選ぶ場合は pipeline が必要です")
    for key, lo, hi in (("width", 160, 4096), ("height", 120, 4096), ("framerate", 1, 120)):
        try:
            val = int(cam_in.get(key, DEFAULTS["camera"][key]))
        except (TypeError, ValueError):
            raise ValueError(f"camera.{key} は整数で指定してください")
        if not (lo <= val <= hi):
            raise ValueError(f"camera.{key} は {lo}〜{hi} で指定してください")
        cam[key] = val

    device = str(mic_in.get("device", "")).strip()
    try:
        rate = int(mic_in.get("capture_rate", 48000))
    except (TypeError, ValueError):
        raise ValueError("mic.capture_rate は整数で指定してください")
    if rate % 16000:
        raise ValueError("mic.capture_rate は 16000 の整数倍で指定してください(16000/32000/48000)")
    if not (16000 <= rate <= 192000):
        raise ValueError("mic.capture_rate は 16000〜192000 で指定してください")

    return {"schema_version": SCHEMA_VERSION, "camera": cam,
            "mic": {"device": device, "capture_rate": rate}}


# ── GStreamer ソース / ALSA デバイスの組み立て ────────────────────────
def camera_source(cfg: dict | None = None) -> str:
    """設定から camera_publisher に渡す GStreamer ソース文字列を作る。"""
    cfg = cfg or load()
    cam = cfg.get("camera", {})
    kind = cam.get("kind", "usb")
    w, h, fps = cam.get("width", 1024), cam.get("height", 768), cam.get("framerate", 30)
    if kind == "raw_pipeline":
        return cam.get("pipeline", "")
    if kind == "csi":
        return "libcamerasrc"
    if kind == "test":
        return "videotestsrc is-live=true pattern=smpte"
    dev = cam.get("device", "")
    if not dev:
        return ""
    if cam.get("format") == "raw":
        return f"v4l2src device={dev} ! video/x-raw,width={w},height={h},framerate={fps}/1"
    return (f"v4l2src device={dev} ! image/jpeg,width={w},height={h},"
            f"framerate={fps}/1 ! jpegdec")


def mic_device(cfg: dict | None = None) -> str:
    cfg = cfg or load()
    return str(cfg.get("mic", {}).get("device", "") or "")


def mic_capture_rate(cfg: dict | None = None) -> int:
    cfg = cfg or load()
    try:
        return int(cfg.get("mic", {}).get("capture_rate", 48000))
    except (TypeError, ValueError):
        return 48000


def program_env(cfg: dict | None = None) -> dict:
    """master_control が子プロセスへ注入する環境変数。

    camera_publisher は CAM1 / DEFAULT_CAM、mic_publisher は MIC_DEVICE /
    MIC_CAPTURE_RATE を読む。解決できない項目は入れない(下位の自動検出に任せる)。
    """
    cfg = cfg or load()
    env = {}
    src = camera_source(cfg) or autodetect_camera_source()
    if src:
        env["CAM1"] = src
        env["DEFAULT_CAM"] = "1"
    dev = mic_device(cfg) or autodetect_mic_device()
    if dev:
        env["MIC_DEVICE"] = dev
    env["MIC_CAPTURE_RATE"] = str(mic_capture_rate(cfg))
    return env


# ── 候補の列挙 ────────────────────────────────────────────────────────
def _run(argv: list[str], timeout: float = 4.0) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return out.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _v4l2_info(dev: str) -> tuple[str, set[str], list[str]]:
    """v4l2-ctl から (カード名, 対応フォーマット集合, 代表的な解像度) を得る。

    v4l-utils が無い環境では ("", {"mjpeg","raw"}, []) を返して両方を候補に出す。
    """
    info = _run(["v4l2-ctl", "-d", dev, "--info"])
    card = ""
    m = re.search(r"Card type\s*:\s*(.+)", info)
    if m:
        card = m.group(1).strip()
    fmts_out = _run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    if not fmts_out:
        return card, {"mjpeg", "raw"}, []
    fmts: set[str] = set()
    if re.search(r"'MJPG'|Motion-JPEG", fmts_out):
        fmts.add("mjpeg")
    if re.search(r"'YUYV'|'UYVY'|'RGB3'|'NV12'", fmts_out):
        fmts.add("raw")
    sizes = []
    for w, h in re.findall(r"Size: Discrete (\d+)x(\d+)", fmts_out):
        s = f"{w}x{h}"
        if s not in sizes:
            sizes.append(s)
    return card, (fmts or {"mjpeg", "raw"}), sizes[:12]


def _csi_present() -> bool:
    """CSI カメラ(libcamera)が使えそうかを軽く判定する。

    /dev/media* が無い機体では libcamera は動かないので、そこで打ち切る
    (libcamera-hello の起動は数秒かかることがあり、UI の候補列挙を待たせたくない)。
    """
    if not glob.glob("/dev/media*"):
        return False
    out = _run(["libcamera-hello", "--list-cameras"], timeout=6.0)
    return "Available cameras" in out and "no cameras" not in out.lower()


def camera_candidates() -> list[dict]:
    """接続されているカメラ入力の候補。UI はこの中から選ばせる。

    USB カメラは `/dev/v4l/by-id/...`(USB ポートとデバイス固有 ID から作られる
    安定パス)で参照する。`/dev/videoN` は起動順や抜き差しで番号が変わるため使わない。
    """
    out: list[dict] = []
    for path in sorted(glob.glob("/dev/v4l/by-id/*")):
        if not path.endswith("-index0"):
            continue          # index1 以降はメタデータ用のサブデバイス
        real = os.path.realpath(path)
        card, fmts, sizes = _v4l2_info(real)
        label = card or os.path.basename(path)
        for fmt in ("mjpeg", "raw"):
            if fmt not in fmts:
                continue
            out.append({
                "id": f"usb:{os.path.basename(path)}:{fmt}",
                "kind": "usb",
                "device": path,
                "format": fmt,
                "label": f"{label} ({'MJPEG' if fmt == 'mjpeg' else 'RAW'})",
                "detail": f"{path} → {os.path.basename(real)}",
                "sizes": sizes,
            })
    if _csi_present():
        out.append({"id": "csi", "kind": "csi", "device": "", "format": "mjpeg",
                    "label": "CSI カメラ (libcamerasrc)", "detail": "libcamera 経由", "sizes": []})
    out.append({"id": "test", "kind": "test", "device": "", "format": "mjpeg",
                "label": "テストパターン (videotestsrc)",
                "detail": "カメラ無しで配信を確認する用", "sizes": []})
    return out


def mic_candidates() -> list[dict]:
    """接続されている録音デバイスの候補。

    `arecord -L` の `hw:CARD=<名前>,DEV=<n>` を使う。カード**番号**(`hw:1,0`)は
    起動順や USB の抜き差しで変わるが、カード名は変わらないため交換時に強い。
    """
    out: list[dict] = []
    text = _run(["arecord", "-L"], timeout=6.0)
    if text:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            name = line.strip()
            if not line or line[0].isspace():
                continue
            if not re.match(r"^(hw|plughw):CARD=", name):
                continue
            desc = ""
            for j in (i + 1, i + 2):
                if j < len(lines) and lines[j][:1].isspace():
                    d = lines[j].strip()
                    if d and not d.startswith("Direct hardware"):
                        desc = d
                        break
            plug = name.startswith("plughw:")
            out.append({
                "id": name,
                "device": name,
                "label": f"{desc or name}{' — 変換あり(plughw)' if plug else ''}",
                "detail": name,
            })
    if not out:
        # arecord が無い/失敗した場合のフォールバック。カード ID は変わらない。
        try:
            with open("/proc/asound/cards", encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*\d+\s+\[(\S+)\s*\]:\s*(.*)", line)
                    if m:
                        cid, desc = m.group(1), m.group(2).strip()
                        out.append({"id": f"hw:CARD={cid},DEV=0",
                                    "device": f"hw:CARD={cid},DEV=0",
                                    "label": desc or cid,
                                    "detail": "/proc/asound/cards より"})
        except OSError:
            pass
    return out


def candidates() -> dict:
    return {"cameras": camera_candidates(), "mics": mic_candidates()}


# ── 自動検出(設定が無いとき) ──────────────────────────────────────────
def autodetect_camera_source() -> str:
    for c in camera_candidates():
        if c["kind"] == "usb":
            return camera_source({"camera": {**DEFAULTS["camera"], "kind": "usb",
                                            "device": c["device"], "format": c["format"]}})
        if c["kind"] == "csi":
            return "libcamerasrc"
    return ""


def autodetect_mic_device() -> str:
    for c in mic_candidates():
        if c["device"].startswith("hw:CARD="):
            return c["device"]
    return ""


def autodetect() -> dict:
    """接続されているデバイスから設定を組む(初期設定の生成用)。"""
    cfg = _merge(DEFAULTS, {})
    for c in camera_candidates():
        if c["kind"] in ("usb", "csi"):
            cfg["camera"]["kind"] = c["kind"]
            cfg["camera"]["device"] = c["device"]
            cfg["camera"]["format"] = c["format"]
            break
    cfg["mic"]["device"] = autodetect_mic_device()
    return cfg


def status() -> dict:
    """UI 用: 現在の設定・候補・解決結果・デバイス存在確認をまとめて返す。

    保存済みのデバイスが今は繋がっていない場合(交換した・差し替えた)を
    `present: false` で伝えるのが要点。UI はそれを警告として出し、候補から
    選び直させる。
    """
    cfg = load()
    cams = camera_candidates()
    mics = mic_candidates()
    cam = cfg.get("camera", {})
    # 未設定(自動検出に任せている)と「保存済みだが今は繋がっていない」を区別する。
    cam_configured = cam.get("kind") != "usb" or bool(cam.get("device"))
    if not cam_configured:
        cam_present = True
    elif cam.get("kind") == "usb":
        cam_present = any(c["kind"] == "usb" and c["device"] == cam.get("device") for c in cams)
    elif cam.get("kind") == "csi":
        cam_present = any(c["kind"] == "csi" for c in cams)
    else:
        cam_present = True
    mic_dev = mic_device(cfg)
    mic_configured = bool(mic_dev)
    mic_present = (not mic_configured) or any(m["device"] == mic_dev for m in mics)
    return {
        "config": cfg,
        "path": config_path(),
        "write_path": config_path(for_write=True),
        "exists": os.path.exists(config_path()),
        "candidates": {"cameras": cams, "mics": mics},
        "resolved": {
            "camera_source": camera_source(cfg) or autodetect_camera_source(),
            "mic_device": mic_dev or autodetect_mic_device(),
            "mic_capture_rate": mic_capture_rate(cfg),
        },
        "present": {"camera": cam_present, "mic": mic_present},
        "configured": {"camera": cam_configured, "mic": mic_configured},
    }


def _main(argv: list[str]) -> int:
    if "--list" in argv:
        print(json.dumps(candidates(), ensure_ascii=False, indent=2))
        return 0
    if "--init" in argv:
        path = config_path(for_write=True)
        if os.path.exists(path):
            print(f"[devices] 既存の設定を尊重しました: {path}")
            return 0
        cfg = autodetect()
        save(cfg)
        print(f"[devices] 自動検出して作成しました: {path}")
        print(f"  camera: {camera_source(cfg) or '(未検出)'}")
        print(f"  mic:    {mic_device(cfg) or '(未検出)'} @ {mic_capture_rate(cfg)} Hz")
        return 0
    st = status()
    print(f"設定ファイル : {st['path']}{'' if st['exists'] else ' (未作成)'}")
    def note(kind):
        if not st["configured"][kind]:
            return "   (未設定 → 自動検出)"
        return "" if st["present"][kind] else "   ← 保存済みデバイスが見つかりません"
    print(f"カメラ       : {st['resolved']['camera_source'] or '(未検出)'}{note('camera')}")
    print(f"マイク       : {st['resolved']['mic_device'] or '(未検出)'} "
          f"@ {st['resolved']['mic_capture_rate']} Hz{note('mic')}")
    print(f"候補         : カメラ {len(st['candidates']['cameras'])} 件 / "
          f"マイク {len(st['candidates']['mics'])} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
