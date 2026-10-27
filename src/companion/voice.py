"""voice：让她能出声，也能哼两句。

分两档，因为成本差一个数量级：

    说话   TTS（本地合成）      几乎不花 token —— 只把文本送进合成器
    唱歌   降级版：TTS 念白+拖腔  不花 token，先验证"她想唱"这件事成不成立

真 SVC（so-vits-svc / DiffSinger）留了接口但**不实现**：那需要她的音色模型、
训练素材和授权，属于另一个量级的工程，也不该由代码偷偷决定。

边界（写死在这里，不靠提示词）：
  · 只发私聊（`public=True` 直接拒）
  · 单段 ≤ 30 秒
  · 不返回音频本体，只返回路径 —— 把 base64 塞进上下文是最贵的浪费
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Home

DIR = "voice"
MAX_SECONDS = 30
CHARS_PER_SECOND = 4.5

BACKENDS = ("edge-tts", "sapi", "say", "espeak")


class VoiceError(RuntimeError):
    pass


def estimate_seconds(text: str, rate: float = 1.0) -> float:
    return len(text or "") / (CHARS_PER_SECOND * max(0.5, rate))


def check(text: str, *, public: bool = False, rate: float = 1.0) -> str:
    """返回空字符串 = 放行；否则是拒绝原因。"""
    text = (text or "").strip()
    if not text:
        return "没有内容可念"
    if public:
        return "语音只发私聊，不发公开平台"
    if len(text) > 600:
        return f"太长了（{len(text)} 字），一次最多 600 字"
    if estimate_seconds(text, rate) > MAX_SECONDS:
        return f"预计 {estimate_seconds(text, rate):.0f} 秒，超过 {MAX_SECONDS} 秒上限"
    return ""


def _out(home: Home, kind: str) -> Path:
    d = home.root / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.wav"


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _sapi(text: str, path: Path, rate: int = 0) -> bool:
    """Windows 自带 SAPI，不装任何包。Rate: -10..10。"""
    if sys.platform != "win32":
        return False
    ps = (
        "$ErrorActionPreference='Stop';"
        f"$s=New-Object -ComObject SAPI.SpVoice;$s.Rate={int(rate)};"
        f"$f=New-Object -ComObject SAPI.SpFileStream;$f.Open('{path}',3,$false);"
        "$s.AudioOutputStream=$f;$s.Speak([Console]::In.ReadToEnd());$f.Close()"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            input=text.encode("utf-8"), capture_output=True, timeout=60,
        )
        return p.returncode == 0 and path.exists() and path.stat().st_size > 1000
    except Exception:
        return False


def _edge(text: str, path: Path, voice: str, rate: str) -> bool:
    exe = shutil.which("edge-tts")
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "--voice", voice, "--rate", rate, "--text", text, "--write-media", str(path)],
                           capture_output=True, timeout=90)
        return p.returncode == 0 and path.exists() and path.stat().st_size > 1000
    except Exception:
        return False


def _say(text: str, path: Path, rate: int) -> bool:
    """macOS 自带 say。它只输出 aiff，所以还要 ffmpeg 转一道。"""
    exe = shutil.which("say")
    ff = shutil.which("ffmpeg")
    if not exe or not ff:
        return False
    aiff = path.with_suffix(".aiff")
    try:
        if subprocess.run([exe, "-r", str(180 + rate * 20), "-o", str(aiff), text], timeout=60).returncode != 0:
            return False
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(aiff), str(path)], timeout=60)
        aiff.unlink(missing_ok=True)
        return r.returncode == 0 and path.exists()
    except Exception:
        return False


def _espeak(text: str, path: Path, rate: int) -> bool:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "-w", str(path), "-s", str(160 + rate * 20), text],
                           capture_output=True, timeout=60)
        return p.returncode == 0 and path.exists() and path.stat().st_size > 1000
    except Exception:
        return False


def speak(
    home: Home,
    text: str,
    *,
    kind: str = "speak",
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: float = 1.0,
    public: bool = False,
) -> dict[str, Any]:
    """合成一段语音，返回 {ok, path, seconds, backend}。不发出去，只落盘。"""
    why = check(text, public=public, rate=rate)
    if why:
        return {"ok": False, "error": why}
    text = text.strip()
    path = _out(home, kind)

    # 唱歌的降级版：慢一点、拖一点，先让"她想唱"能落地
    if kind == "sing":
        voice = voice if voice != "zh-CN-XiaoxiaoNeural" else "zh-CN-XiaoyiNeural"

    sapi_rate = int(round((rate - 1.0) * 8)) - (3 if kind == "sing" else 0)
    rate_str = f"{'+' if rate >= 1 else ''}{int(round((rate - 1) * 100))}%"

    for backend in BACKENDS:
        ok = False
        if backend == "edge-tts":
            ok = _edge(text, path, voice, rate_str)
        elif backend == "sapi":
            ok = _sapi(text, path, sapi_rate)
        elif backend == "say":
            ok = _say(text, path, sapi_rate)
        elif backend == "espeak":
            ok = _espeak(text, path, sapi_rate)
        if ok:
            secs = _duration(path)
            home.append_jsonl("logs/voice.jsonl", {
                "at": datetime.now().isoformat(timespec="seconds"),
                "kind": kind, "backend": backend, "chars": len(text),
                "seconds": round(secs, 2), "path": str(path),
            })
            return {"ok": True, "path": str(path), "seconds": round(secs, 2),
                    "backend": backend, "kind": kind, "chars": len(text)}

    return {"ok": False, "error": "没有可用的合成后端（Windows 用 SAPI，Linux 装 espeak-ng，macOS 自带 say）"}


def sing(home: Home, text: str, **kw) -> dict[str, Any]:
    """降级版"唱"：TTS 慢速 + 拖腔。真 SVC 需要她自己的音色模型，不在这一层。"""
    return speak(home, text, kind="sing", rate=0.78, **kw)


def available() -> dict[str, bool]:
    out = {"edge-tts": bool(shutil.which("edge-tts")), "say": bool(shutil.which("say")),
           "espeak": bool(shutil.which("espeak-ng") or shutil.which("espeak"))}
    out["sapi"] = sys.platform == "win32"
    return out


def last(home: Home, n: int = 5) -> list[dict[str, Any]]:
    return home.read_jsonl("logs/voice.jsonl", limit=n)
