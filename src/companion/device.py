"""device：手。看屏幕、点、打字、开程序。

四类目标共用一个闸，各自一套实现（实现拆在三个文件里，见 docs/AV.md）：

    目标      看                    动                     开
    phone    adb screencap         adb shell input        am start
    desktop  GDI BitBlt (devwin)   SendInput (devwin)     ShellExecute
    browser  CDP (devcdp)          CDP Input (devcdp)     /json/new
    game     同 desktop（按窗口）   同 desktop + 坐标映射   前台窗口置顶

**先有闸，再有手。** 五条：
  1. 白名单：`devices.json` 里列出的包名/窗口/域名才可控
  2. 危险动作：关机、卸载、转账、支付、删除、改密码、装 APK —— 直接拒，不二次询问
     （二次询问会被话术绕过，"不询问"本身就是防线）
  3. 主人限定：只认 owner 会话，群里 @ 她、陌生人私聊一律不触发
  4. 留痕：每个动作写 logs/device.jsonl，可回放
  5. 幂等 + 超时：同一动作 3 秒内不重复；单动作 10 秒中止

边界：只做屏幕与输入。不注入进程、不读内存、不碰反作弊。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Home
from .devcdp import (BrowserError, browser_click, browser_look, browser_open,
                     browser_type, cdp_target)
from .devwin import (DesktopError, click, foreground, screen_size,
                     screenshot_desktop, type_text)
from .imgpng import _shot_path, write_png


LOG = "logs/device.jsonl"
STATE = "devices.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "owner_only": True,
    "allow": {
        "phone": {"packages": []},
        "desktop": {"windows": []},
        "browser": {"domains": []},
        "game": {"windows": []},
    },
    "deny_words": [
        "关机", "重启", "卸载", "删除", "格式化", "转账", "支付", "付款", "汇款",
        "密码", "验证码", "身份证", "安装包", "apk", "shutdown", "format", "uninstall",
    ],
    "idempotent_seconds": 3,
    "timeout_seconds": 10,
    "max_screenshot_kb": 3000,
}


# ── 闸 ─────────────────────────────────────────────────────
@dataclass
class Verdict:
    ok: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason}


def config(home: Home) -> dict[str, Any]:
    p = home.root / STATE
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    out.update({k: v for k, v in d.items() if k in DEFAULT_CONFIG})
    return out


def _state(home: Home) -> dict[str, Any]:
    p = home.root / "logs/device_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _save_state(home: Home, st: dict[str, Any]) -> None:
    p = home.root / "logs/device_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def check(
    home: Home,
    *,
    target: str,
    action: str,
    args: dict[str, Any] | None = None,
    is_owner: bool = True,
    now: float | None = None,
) -> Verdict:
    cfg = config(home)
    args = args or {}
    now = now or time.time()

    if not cfg.get("enabled", True):
        return Verdict(False, "设备控制没开（devices.json: enabled=false）")
    if cfg.get("owner_only", True) and not is_owner:
        return Verdict(False, "只在主人会话里能动手；群聊/陌生人一律不触发")

    blob = _norm(action + " " + " ".join(str(v) for v in args.values()))
    for w in cfg.get("deny_words") or []:
        if _norm(w) and _norm(w) in blob:
            return Verdict(False, f"危险动作被拒：出现「{w}」。这类事必须本人亲手做")

    allow = (cfg.get("allow") or {}).get(target) or {}
    keys = {"open": ("packages", "windows", "domains"), "launch": ("packages", "windows", "domains")}.get(action, ())
    for k in keys:
        listed = [str(x).lower() for x in (allow.get(k) or [])]
        if not listed:
            continue
        want = ""
        if k == "packages":
            want = _norm(args.get("package", ""))
        elif k == "windows":
            want = _norm(args.get("window") or args.get("app") or args.get("what") or "")
        elif k == "domains":
            want = _norm(args.get("url") or args.get("what") or "")
        if want and not any(x in want for x in listed):
            return Verdict(False, f"不在白名单里：{want}（devices.json 的 allow.{target}.{k}）")

    st = _state(home)
    key = hashlib.sha1(f"{target}|{action}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:16]
    last = float((st.get("recent") or {}).get(key) or 0)
    if now - last < float(cfg.get("idempotent_seconds", 3)):
        return Verdict(False, f"重复动作（{cfg.get('idempotent_seconds')} 秒内），没有再执行一次")
    return Verdict(True)


def _mark(home: Home, key: str, now: float) -> None:
    st = _state(home)
    recent = st.get("recent") or {}
    recent = {k: v for k, v in recent.items() if now - float(v) < 60}
    st["recent"] = recent
    st["recent"][key] = now
    _save_state(home, st)


def _log(home: Home, row: dict[str, Any]) -> None:
    home.append_jsonl(LOG, row)


def _key(target: str, action: str, args: dict[str, Any]) -> str:
    return hashlib.sha1(f"{target}|{action}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:16]


# ── Android（adb）──────────────────────────────────────────
class PhoneError(RuntimeError):
    pass


def _adb(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    exe = shutil.which("adb") or shutil.which("adb.exe")
    if not exe:
        raise PhoneError("找不到 adb（装 platform-tools 或把它放进 PATH）")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout)


def phone_devices() -> list[str]:
    try:
        r = _adb("devices")
    except Exception:
        return []
    out = []
    for line in r.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def screenshot_phone(home: Home, *, serial: str = "") -> Path:
    args = (["-s", serial] if serial else []) + ["exec-out", "screencap", "-p"]
    r = _adb(*args)
    if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
        raise PhoneError("adb 截图失败：" + r.stderr.decode("utf-8", "ignore")[:120] + "（手机连着吗？adb devices 有输出吗）")
    p = _shot_path(home, "phone")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.stdout)
    return p


def phone_tap(x: int, y: int, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "tap", str(int(x)), str(int(y)))


def phone_swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 300, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "swipe",
         str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms)))


def phone_type(text: str, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "text", text.replace(" ", "%s"))


def phone_open(package: str, *, serial: str = "") -> str:
    r = _adb(*(["-s", serial] if serial else []), "shell", "monkey", "-p", package,
             "-c", "android.intent.category.LAUNCHER", "1")
    return (r.stdout or b"").decode("utf-8", "ignore")[-200:]


# ── 对外三个动作：看 / 动 / 开 ──────────────────────────────
def look(home: Home, target: str = "desktop", *, serial: str = "") -> dict[str, Any]:
    v = check(home, target=target, action="look")
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    try:
        if target == "phone":
            p = screenshot_phone(home, serial=serial)
            meta = {"devices": phone_devices()}
        elif target == "browser":
            p = browser_look(home)
            meta = {"cdp": bool(cdp_target())}
        elif target in ("desktop", "game"):
            p = screenshot_desktop(home, target)
            meta = {"foreground": foreground(), "screen": list(screen_size())}
        else:
            return {"ok": False, "error": f"不认识的目标：{target}（phone/desktop/browser/game）"}
        size_kb = max(1, p.stat().st_size // 1024)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": True, "shot": str(p), "kb": size_kb})
        return {"ok": True, "target": target, "shot": str(p), "kb": size_kb, **meta}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def act(
    home: Home,
    target: str,
    action: str,
    args: dict[str, Any] | None = None,
    *,
    is_owner: bool = True,
    serial: str = "",
) -> dict[str, Any]:
    args = args or {}
    v = check(home, target=target, action=action, args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    key = _key(target, action, args)
    started = time.time()
    try:
        if target == "phone":
            if action in ("tap", "click"):
                phone_tap(args["x"], args["y"], serial=serial)
            elif action == "swipe":
                phone_swipe(args["x"], args["y"], args["x2"], args["y2"], int(args.get("ms", 300)), serial=serial)
            elif action in ("type", "text"):
                phone_type(str(args.get("text", "")), serial=serial)
            elif action == "key":
                _adb(*(["-s", serial] if serial else []), "shell", "input", "keyevent", str(args.get("key", "")))
            else:
                return {"ok": False, "error": f"phone 不支持：{action}"}
        elif target in ("desktop", "game"):
            if action in ("tap", "click"):
                click(args["x"], args["y"], double=bool(args.get("double")))
            elif action in ("type", "text"):
                type_text(str(args.get("text", "")))
            else:
                return {"ok": False, "error": f"{target} 不支持：{action}（只做点击/输入）"}
        elif target == "browser":
            if action in ("tap", "click"):
                browser_click(args["x"], args["y"])
            elif action in ("type", "text"):
                browser_type(str(args.get("text", "")))
            else:
                return {"ok": False, "error": f"browser 不支持：{action}"}
        else:
            return {"ok": False, "error": f"不认识的目标：{target}"}

        dt = time.time() - started
        if dt > config(home).get("timeout_seconds", 10):
            return {"ok": False, "error": f"动作超时（{dt:.1f}s）"}
        _mark(home, key, time.time())
        after = look(home, "browser" if target == "browser" else target, serial=serial)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "args": args, "ok": True, "ms": int(dt * 1000)})
        return {"ok": True, "target": target, "action": action, "ms": int(dt * 1000),
                "after": after.get("shot"), "foreground": foreground() if target != "phone" else ""}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def open_thing(home: Home, target: str, what: str, *, is_owner: bool = True) -> dict[str, Any]:
    args = {"what": what, "url": what, "package": what, "window": what}
    v = check(home, target=target, action="open", args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    try:
        if target == "phone":
            how = "adb:" + phone_open(what)[-60:]
        elif target == "browser":
            how = browser_open(what)
        elif target in ("desktop", "game"):
            if sys.platform == "win32":
                os.startfile(what)  # type: ignore[attr-defined]
                how = "shell"
            else:
                subprocess.Popen([what])
                how = "exec"
        else:
            return {"ok": False, "error": f"不认识的目标：{target}"}
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": True, "how": how})
        return {"ok": True, "target": target, "what": what, "how": how}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def log_tail(home: Home, n: int = 20) -> list[dict[str, Any]]:
    return home.read_jsonl(LOG, limit=n)
