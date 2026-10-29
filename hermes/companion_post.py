"""hermes 适配层 ④：**这一轮她说了什么**（写回的唯一权威通道）。

为什么单独一个脚本：`pre_llm_call` 拿不到本轮的回复——
它跑在生成**之前**。想从那里捞"她上一句"，只能靠 `conversation_history` 猜，
而 hermes 的历史里混着工具消息、压缩后的片段、还有可能被裁掉的行。
实测（2026-10-29）：一整轮真实对话下来，`turns.jsonl` 是 **0 字节** ——
她说过的话一个字没落账，于是每轮都在失忆。

`post_llm_call` 才对：它在工具循环结束后触发一次，直接给
`assistant_response` —— 就是主人真正看到的那段话。

三条设计取舍：

  · **只认 `assistant_response`**。这是唯一"主人看到的就是这个"的来源，
    不猜、不从历史里拼。
  · **幂等**。同一轮可能被触发多次（重试、压缩重建），按
    (turn_id + 正文) 的哈希去重，重复的不会重复记账、不会重复推心情。
  · **写回失败不能拖垮对话**。钩子返回 `{}` 就完事，
    失败只记日志——她该说话还是要说话。

写回什么（core 的 `react` 一条命令落完）：
  · 心情（mood_rules 只在真的开口后才推）
  · 人物卡（`people.touch` —— 至少知道"他刚来过"）
  · 她说过的话（`said`）→ turns.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_HOME = os.path.expanduser("~/.companion")
LOG = "/tmp/companion_post.log"
STATE = "logs/.post_state.json"

# 部署时和 companion_shared.py 同目录（hermes/scripts/）——
# payload 规范化是三个钩子共用的逻辑，别各抄一份然后改出三个样。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from companion_shared import normalize_payload as _normalize
except ImportError:  # 单文件跑（没部署全）就本地实现，行为一致
    def _normalize(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload
        out = dict(payload)
        extra = out.pop("extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if out.get(k) in (None, ""):
                    out[k] = v
        if isinstance(out.get("tool_input"), dict) and not isinstance(out.get("args"), dict):
            out["args"] = out["tool_input"]
        elif isinstance(out.get("args"), dict) and not isinstance(out.get("tool_input"), dict):
            out["tool_input"] = out["args"]
        return out


def _note(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")


def _field(value, key):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part) -> str:
    """从一个 part 里取出可见文本。

    跟 hermes 的 `agent/message_content.py::_text_from_part` 保持一致：
    图片/音频 part 必须排除（否则 base64 会被当正文写进账本），
    文本键要认全（Responses API 给的是 `output_text`，不是 `text`）。
    """
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if str(_field(part, "type") or "").strip().lower() in _NON_TEXT_PART_TYPES:
        return ""
    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def _text_of(raw) -> str:
    """assistant_response 可能是 str、dict 或 parts 列表。"""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        inner = raw.get("content")
        if inner is not None:
            return _text_of(inner)
        return _text_from_part(raw).strip()
    if isinstance(raw, list):
        parts = []
        for part in raw:
            text = _text_from_part(part)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return str(raw or "").strip()


def _home() -> Path:
    return Path(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)


def _seen(home: Path) -> list[str]:
    try:
        d = json.loads((home / STATE).read_text(encoding="utf-8"))
        return [str(x) for x in d if isinstance(x, str)][-60:]
    except Exception:
        return []


def _remember(home: Path, digest: str) -> None:
    seen = _seen(home)
    seen.append(digest)
    try:
        p = home / STATE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(seen[-60:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _est_tokens(text: str) -> int:
    """按字符构成估 token。

    hermes 的 ``post_llm_call`` payload 里没有用量字段（只有 assistant_response /
    conversation_history / model），所以只能估。中英文的 token 密度差四倍，
    统一按字符数除会系统性偏差，这里分开算：

      · 汉字：约 1 字 1 token
      · 其它（英文/数字/标点）：约 4 字符 1 token

    这是估算，不是真实计费；``companion cost`` 的数字口径以此为准。
    """
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


def _react(home: Path, payload: dict) -> tuple[bool, str]:
    """调 core 的 react 落账。返回 (成功, 原因)。"""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="companion_post_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            done = subprocess.run(
                [sys.executable, "-m", "companion", "--json", "react", "--from", tmp],
                capture_output=True, text=True, timeout=20,
                env={**os.environ, "COMPANION_HOME": str(home),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        if done.returncode != 0:
            return False, (done.stderr or "").strip()[:200] or f"exit {done.returncode}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # hermes 把 assistant_response 等字段塞在 extra 里 —— 不规范化就读不到，
    # 然后静默退出：turns.jsonl 永远是空的（2026-10-29 实测踩过）。
    payload = _normalize(payload)

    said = _text_of(payload.get("assistant_response"))
    if not said or said.startswith("[SILENT]"):
        print("{}")
        return 0

    platform = str(payload.get("platform") or "")
    # cron / 子会话不要写：那些不是跟主人说的话
    session = str(payload.get("session_id") or "")
    if session.startswith("local-"):
        print("{}")
        return 0

    home = _home()
    turn = str(payload.get("turn_id") or "")
    digest = hashlib.sha1(f"{turn}|{said}".encode("utf-8")).hexdigest()[:16]
    if digest in _seen(home):
        _note(f"dup-skip {digest}")
        print("{}")
        return 0

    # hermes 不给用量，按字符估：历史算 cached（单价低），本轮用户消息与回复算 fresh/output。
    history_text = ""
    raw_history = payload.get("conversation_history")
    if isinstance(raw_history, list):
        for m in raw_history:
            if isinstance(m, dict):
                history_text += _text_of(m.get("content")) or ""
    user_text = _text_of(payload.get("user_message"))

    # 她说了什么 + 她刚跟主人说过话（人物卡 last_seen）
    body = {
        "said": said[:1500],
        "why": "hermes post_llm_call 记录",
        "person": "owner",
        "facts": [],
        "usage": {
            "model": str(payload.get("model") or ""),
            "fresh_tokens": _est_tokens(user_text),
            "cached_tokens": _est_tokens(history_text),
            "output_tokens": _est_tokens(said),
        },
    }
    ok, why = _react(home, body)
    if not ok:
        _note(f"react-fail {why}")
        print("{}")
        return 0
    _remember(home, digest)
    _note(f"react-said {len(said)}c platform={platform} session={session[:16]}")
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
