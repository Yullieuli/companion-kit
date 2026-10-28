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

from . import ask as ask_mod
from .store import Home
from .devcdp import (BrowserError, browser_click, browser_key, browser_look,
                     browser_open, browser_swipe, browser_type, cdp_target)
from .devwin import (DesktopError, click, drag, focus as focus_window, foreground,
                     key as press_key, screen_origin, screen_size, screenshot_desktop,
                     screenshot_window, type_text, window_rect, windows as list_windows)
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
    # 会改变他眼前环境的事，先问一声。点/打字不算——那是已经允许的上下文里做事。
    "ask": {"enabled": True, "actions": ["open", "focus"], "targets": ["game"]},
    # OCR 只是"帮她把图上的字先读一遍"，读不到就让模型的 vision 看，不影响别的
    "ocr": {"enabled": True, "lang": "chi_sim+eng", "min_conf": 40},
    # WSL 里跑时把 device 转发给 Windows 侧（见 devdelegate.py）。不配就一直报"本机不支持"。
    "windows": {"python": "", "src": ""},
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
    out["ask"] = {**DEFAULT_CONFIG["ask"], **(d.get("ask") or {})}
    out["ocr"] = {**DEFAULT_CONFIG["ocr"], **(d.get("ocr") or {})}
    out["windows"] = {**DEFAULT_CONFIG["windows"], **(d.get("windows") or {})}
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


def map_point(x: float, y: float, design: tuple[int, int], screen: tuple[int, int]) -> tuple[int, int]:
    """把「设计分辨率下的坐标」换成「当前屏幕坐标」。

    游戏为什么必须做这一步：攻略写的是 1920×1080 的第 960 个像素，
    而他的屏幕可能是 2560×1440 的窗口模式。差 1.33 倍，点下去就是另一个按钮。
    """
    dw, dh = max(1, int(design[0])), max(1, int(design[1]))
    sw, sh = max(1, int(screen[0])), max(1, int(screen[1]))
    return int(round(float(x) * sw / dw)), int(round(float(y) * sh / dh))


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
    keys = {"open": ("packages", "windows", "domains"),
            "launch": ("packages", "windows", "domains"),
            "focus": ("windows",)}.get(action, ())
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
def _prune_shots(home: Home, *, keep: int = 60, days: float = 7) -> int:
    """清掉旧截图。一张双屏 PNG 就 2MB，不清的话一天几百兆。

    只动 `shots/` 顶层（就是 look 拍的那些），bili 的封面/抽帧在子目录，归它自己的 7 天缓存管。
    """
    root = home.root / "shots"
    if not root.exists():
        return 0
    files = [p for p in root.glob("*.png") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - days * 86400
    removed = 0
    for i, p in enumerate(files):
        try:
            if i >= keep or p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def look(home: Home, target: str = "desktop", *, serial: str = "",
         window: str = "") -> dict[str, Any]:
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
            p = screenshot_window(home, window, target=target) if window \
                else screenshot_desktop(home, target)
            try:
                tops = [w["title"] for w in list_windows(6)]
            except Exception:
                tops = []
            meta = {"foreground": foreground(), "screen": list(screen_size()),
                    "origin": list(screen_origin()), "windows": tops, "window": window or ""}
        else:
            return {"ok": False, "error": f"不认识的目标：{target}（phone/desktop/browser/game）"}
        size_kb = max(1, p.stat().st_size // 1024)
        over = size_kb > int(config(home).get("max_screenshot_kb") or 3000)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": True, "shot": str(p), "kb": size_kb})
        _prune_shots(home)
        out = {"ok": True, "target": target, "shot": str(p), "kb": size_kb, **meta}
        if over:
            # 超过上限就明确说一句：宿主要把图缩放/裁剪，别整张塞进上下文
            out["oversize"] = True
            out["hint"] = f"图 {size_kb}KB 超过 {config(home).get('max_screenshot_kb')}KB，进上下文前先缩"
        return out
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _ask_gate(home: Home, target: str, action: str, args: dict[str, Any],
              ask_id: str = "") -> dict[str, Any] | None:
    """要问就问一次。返回 None 表示放行，返回 dict 表示这次不能做。"""
    cfg = config(home)
    if not ask_mod.required(cfg, target, action):
        return None
    tok = ""
    if ask_id and ask_mod.verify(home, ask_id, action, args, target=target):
        tok = ask_id
    else:
        tok = ask_mod.token(home, action, args, target=target)
    if tok:
        used = ask_mod.consume(home, tok)
        if used.get("ok"):
            return None
    req = ask_mod.request(home, action, args, target=target)
    _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                "action": action, "ok": False, "ask_required": True, "ask": req.get("id")})
    return {"ok": False, "ask_required": True, "ask": req, "say": req.get("say"),
            "hint": "先把这句话说出来；主人答应后用 ask_id 重试同一个动作"}


def act(
    home: Home,
    target: str,
    action: str,
    args: dict[str, Any] | None = None,
    *,
    is_owner: bool = True,
    serial: str = "",
    ask_id: str = "",
    before: bool = False,
) -> dict[str, Any]:
    args = args or {}
    v = check(home, target=target, action=action, args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    gate_args = args
    if action.lower() == "focus":
        gate_args = {"what": str(args.get("window") or args.get("text") or "")}
    blocked = _ask_gate(home, target, action, gate_args, ask_id)
    if blocked is not None:
        return blocked
    key = _key(target, action, args)
    started = time.time()
    pre_shot = ""
    if before:
        # 要"执行前那张"就得多截一次图。默认不截：每个动作两张图，磁盘与耗时都翻倍。
        pre = look(home, "browser" if target == "browser" else target, serial=serial)
        pre_shot = str(pre.get("shot") or "")
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
            cfg = config(home)
            act_l = action.lower()
            # 游戏按「设计分辨率 + 窗口偏移」换算坐标；桌面就是屏幕坐标，不动
            def _pt(px: Any, py: Any) -> tuple[int, int]:
                if target != "game":
                    return int(px), int(py)
                g = cfg.get("game") or {}
                design = tuple(g.get("design") or (1920, 1080))
                want = str(g.get("window") or "")
                rect = window_rect(want) if want else {}
                screen = (rect.get("w") or screen_size()[0], rect.get("h") or screen_size()[1])
                mx, my = map_point(px, py, (int(design[0]), int(design[1])), screen)
                return mx + int(rect.get("x") or 0), my + int(rect.get("y") or 0)

            if act_l in ("tap", "click", "swipe"):
                if "x" not in args or "y" not in args:
                    return {"ok": False, "error": f"{act_l} 需要 x / y"}
                if target == "game":
                    want = str((cfg.get("game") or {}).get("window") or "")
                    fg = foreground()
                    if want and want.lower() not in fg.lower():
                        return {"ok": False,
                                "error": f"游戏不在前台（当前是「{fg}」）→ 输入会打到别的窗口，先 device focus"}
                x1, y1 = _pt(args["x"], args["y"])
                if act_l == "swipe":
                    x2, y2 = _pt(args.get("x2", args["x"]), args.get("y2", args["y"]))
                    drag(x1, y1, x2, y2, ms=int(args.get("ms", 300)))
                else:
                    click(x1, y1, double=bool(args.get("double")))
            elif act_l in ("type", "text"):
                type_text(str(args.get("text", "")))
            elif act_l == "key":
                press_key(str(args.get("key") or ""))
            elif act_l == "focus":
                return _focus_apply(home, str(args.get("window") or args.get("text") or ""))
            else:
                return {"ok": False, "error": f"{target} 不支持：{action}（click/swipe/type/key）"}
        elif target == "browser":
            act_l = action.lower()
            if act_l in ("tap", "click"):
                browser_click(args["x"], args["y"])
            elif act_l == "swipe":
                browser_swipe(args["x"], args["y"], args.get("x2", args["x"]), args.get("y2", args["y"]))
            elif act_l in ("type", "text"):
                browser_type(str(args.get("text", "")))
            elif act_l == "key":
                browser_key(str(args.get("key") or ""))
            else:
                return {"ok": False, "error": f"browser 不支持：{action}（click/swipe/type/key）"}
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
                "before": pre_shot, "after": after.get("shot"),
                "foreground": foreground() if target != "phone" else ""}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def open_thing(home: Home, target: str, what: str, *, is_owner: bool = True,
               ask_id: str = "") -> dict[str, Any]:
    args = {"what": what, "url": what, "package": what, "window": what}
    v = check(home, target=target, action="open", args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    # 问的时候只带 what，别把 url/package 都塞进指纹
    blocked = _ask_gate(home, target, "open", {"what": what}, ask_id)
    if blocked is not None:
        return blocked
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


def parse_ocr_tsv(tsv: str, *, min_conf: float = 40) -> dict[str, Any]:
    """把 tesseract 的 TSV 输出按行拼回去。

    只取 level=5（词）这一层自己拼行，不信任它的文本输出：
    TSV 带每词的坐标与置信度，才能把"左边菜单 / 右边正文"分开读。
    """
    rows: dict[tuple[str, str, str, str], list[str]] = {}
    confs: dict[tuple[str, str, str, str], list[float]] = {}
    for line in (tsv or "").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            level = int(cols[0])
            conf = float(cols[10])
        except ValueError:
            continue
        word = cols[11].strip()
        if level != 5 or not word or conf < min_conf:
            continue
        key = (cols[1], cols[2], cols[3], cols[4])
        rows.setdefault(key, []).append(word)
        confs.setdefault(key, []).append(conf)
    lines = [{"text": " ".join(v), "conf": round(sum(confs[k]) / len(confs[k]), 1)}
             for k, v in sorted(rows.items())]
    return {"text": "\n".join(x["text"] for x in lines), "lines": lines}


def ocr_bin() -> str:
    """找一个能用的 tesseract。找不到就是没有 —— 不装、不联网下。"""
    found = shutil.which("tesseract") or shutil.which("tesseract.exe")
    if found:
        return found
    if sys.platform == "win32":
        for cand in (r"C://Program Files//Tesseract-OCR//tesseract.exe",
                     r"C://Program Files (x86)//Tesseract-OCR//tesseract.exe"):
            if Path(cand).exists():
                return cand
    return ""


def ocr_image(home: Home, shot: str | Path, *, lang: str = "") -> dict[str, Any]:
    """读图上的字。没有引擎就老实返回 none，让模型的 vision 接手。"""
    exe = ocr_bin()
    if not exe:
        return {"backend": "none", "text": "", "lines": [],
                "hint": "没装 tesseract；把截图交给宿主的 vision 模型读，别猜"}
    cfg = config(home).get("ocr") or {}
    if not cfg.get("enabled", True):
        return {"backend": "off", "text": "", "lines": [], "hint": "OCR 在 devices.json 里关着"}
    want = lang or str(cfg.get("lang") or "chi_sim+eng")
    for use in (want, "eng"):
        try:
            done = subprocess.run([exe, str(shot), "stdout", "-l", use, "--psm", "6", "tsv"],
                                  capture_output=True, text=True, timeout=60)
        except Exception as e:
            return {"backend": "error", "text": "", "lines": [],
                    "hint": f"{type(e).__name__}: {e}"}
        if done.returncode == 0:
            parsed = parse_ocr_tsv(done.stdout, min_conf=float(cfg.get("min_conf") or 40))
            return {"backend": f"tesseract:{use}", "chars": len(parsed["text"]), **parsed}
    return {"backend": "error", "text": "", "lines": [],
            "hint": (done.stderr or "")[:200]}


DESCRIBE_HINT = ("先用 ocr.text 回答能读到的内容；读不到再看图。"
                 "画面里出现的文字/按钮/报错，照原样引用，别概括。")


def observe(home: Home, target: str = "desktop", *, window: str = "",
            serial: str = "", ocr: bool = True) -> dict[str, Any]:
    """看：一张图 + 一句"这是哪" + 图上的字（如果有引擎）。

    分工写在 SPEC 5.7：能本地做的先本地做。OCR 是本地做的部分；
    "这画面在讲什么"交给宿主的视觉模型 —— companion 不调模型。
    """
    got = look(home, target, serial=serial, window=window)
    if not got.get("ok"):
        return got
    out = dict(got)
    if ocr:
        out["ocr"] = ocr_image(home, got["shot"])
    out["describe_hint"] = DESCRIBE_HINT
    _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                "action": "observe", "ok": True, "shot": got.get("shot"),
                "window": window or "", "ocr": (out.get("ocr") or {}).get("backend")})
    return out


def windows_list(home: Home, limit: int = 30) -> dict[str, Any]:
    """列可见窗口。只读标题，不过闸 —— 但也不给句柄之外的任何能力。"""
    try:
        rows = list_windows(limit)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": "desktop",
                    "action": "list", "ok": True, "n": len(rows)})
        return {"ok": True, "windows": rows, "foreground": foreground()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def focus_window_by(home: Home, what: str, *, is_owner: bool = True,
                    ask_id: str = "") -> dict[str, Any]:
    """把某个窗口提到前台。切之前过闸（窗口白名单 + 主人限定 + 幂等）。"""
    v = check(home, target="desktop", action="focus", args={"window": what}, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": "desktop",
                    "action": "focus", "what": what, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    blocked = _ask_gate(home, "desktop", "focus", {"what": what}, ask_id)
    if blocked is not None:
        return blocked
    return _focus_apply(home, what)


def _focus_apply(home: Home, what: str) -> dict[str, Any]:
    """真正去切窗口。不含闸 —— 调用方负责过闸，避免一处动作问两遍。"""
    try:
        out = focus_window(what)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": "desktop",
                    "action": "focus", "what": what, "ok": True, "title": out.get("title")})
        return out
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": "desktop",
                    "action": "focus", "what": what, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


REPLAYABLE = {"tap", "click", "swipe", "type", "key", "focus", "open"}


def replay(home: Home, last: int = 5, *, execute: bool = False, with_text: bool = False,
           is_owner: bool = True) -> dict[str, Any]:
    """把最近几条动作按原顺序重放一遍（默认只看不点）。

    为什么默认 dry-run：回放的是**已经发生过**的动作，屏幕上的东西早变了，
    同一个坐标现在可能是"删除"。所以先给一份可核对的清单，人点头才真跑。

    为什么输入要单独再确认一次：`args` 里带着当时打的字，可能是一段私信、
    一个密码。`--with-text` 是第二个刹车，不是多余的。
    """
    rows = home.read_jsonl(LOG, limit=300)
    picked = [r for r in rows
              if r.get("ok") and str(r.get("action") or "").lower() in REPLAYABLE]
    picked = picked[-max(1, int(last)):]

    plan: list[dict[str, Any]] = []
    for r in picked:
        action = str(r.get("action") or "").lower()
        step: dict[str, Any] = {"at": r.get("at"), "target": r.get("target") or "desktop",
                                "action": action}
        if action == "open":
            step["what"] = r.get("what") or (r.get("args") or {}).get("what") or ""
        else:
            step["args"] = dict(r.get("args") or {})
            if "text" in step["args"]:
                step["has_text"] = True
        plan.append(step)

    if not plan:
        return {"ok": True, "dry_run": not execute, "steps": [],
                "hint": "日志里没有可回放的动作（先 look/act 几次）"}
    if not execute:
        return {"ok": True, "dry_run": True, "steps": plan,
                "hint": "确认无误后加 --yes 真跑；含输入的动作还要再加 --with-text"}

    done: list[dict[str, Any]] = []
    for step in plan:
        action, target = step["action"], step["target"]
        if action == "open":
            out = open_thing(home, target, str(step.get("what") or ""), is_owner=is_owner)
        else:
            args = dict(step.get("args") or {})
            if "text" in args and not with_text:
                done.append({"at": step["at"], "action": action, "skipped": "含输入，未加 --with-text"})
                continue
            out = act(home, target, action, args, is_owner=is_owner, before=True)
        done.append({"at": step["at"], "action": action, "target": target,
                     "ok": bool(out.get("ok")), "before": out.get("before"),
                     "after": out.get("after"), "error": out.get("error")})
    return {"ok": all(x.get("ok") or x.get("skipped") for x in done),
            "dry_run": False, "steps": done}


def log_tail(home: Home, n: int = 20) -> list[dict[str, Any]]:
    return home.read_jsonl(LOG, limit=n)
