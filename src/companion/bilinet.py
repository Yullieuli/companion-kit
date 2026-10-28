"""bilinet：B 站传输层 —— 全项目**唯一**会联网的模块之一（另一个是 device）。

为什么单独一层：
  · 别的模块要能在脱网环境里单测，所以网络、节流、预算、熔断全部关在这里
  · `bili.py` 只决定「看哪条、该不该分享、怎么记」，给它注入一个 net 就能测
  · 这层的失败必须显式：412 就熔断，额度用完就停手，**绝不"再试一次"**

四条硬约束（都是踩过坑换来的）：
  1. 每个请求都带 WBI 签名（w_rid/wts）。不签名的写接口更容易进影子审核。
  2. 全局最小间隔 600ms，跨进程用文件锁 —— 不然两个宿主同时刷，风控立刻来。
  3. 读 3000/天、写 30/天、两次写之间 ≥120s。写额度是**共享**的，包括评论和私信。
  4. 撞 412/429 立刻停手，按 5→10→20→30 分钟阶梯冷静。停手期间所有请求直接抛错。

凭证只从本机读，仓库里永远没有 cookie。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import date
from pathlib import Path
from typing import Any

try:  # Windows 没有 fcntl：退化成无锁（单进程场景够用）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"

# WBI 的字符重排表（官方前端混淆参数，不是加密）
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

# attr 里这几个位表示「不公开」：2**38 实测就是「仅自己可见 / 审核中」
ATTR_HIDDEN_BITS = (1 << 38,)

DEFAULT_LIMITS: dict[str, Any] = {
    "interval_ms": 600,
    "read_per_day": 3000,
    "write_per_day": 30,
    "write_gap_ms": 120_000,
    "pause_steps_min": (5, 10, 20, 30),
}

CRED_CANDIDATES = (
    "~/.bilibili-cli/credential.json",
    "~/.bili-ai/credentials.json",
    "~/.config/bilibili-cli/credential.json",
)

HINTS = {
    -101: "账号未登录：cookie 失效",
    -102: "账号被封停",
    -111: "csrf 校验失败：bili_jct 与 SESSDATA 不是同一个号",
    -400: "请求错误",
    -404: "无此项",
    -509: "请求过于频繁",
    12002: "评论区已关闭",
    12015: "需要评论验证码（被风控盯上了，停手）",
    12016: "内容含敏感信息",
    12035: "被 UP 主列入评论黑名单",
    12051: "重复评论被判刷屏",
}


class BiliError(RuntimeError):
    def __init__(self, code: int | str, message: str, url: str = ""):
        self.code = code
        self.message = message
        self.url = url
        super().__init__(f"[{code}] {message}")


class RiskPaused(RuntimeError):
    """风控熔断中，主动停手。"""


class BudgetExceeded(RuntimeError):
    """今日读写额度用完。"""


class NoCredential(RuntimeError):
    """没有可用凭证 —— 这不是错误，是「今天不刷」的理由。"""


def check(obj: dict[str, Any], what: str = "接口") -> dict[str, Any]:
    code = obj.get("code")
    if code not in (0, None):
        hint = HINTS.get(code) or HINTS.get(str(code)) or ""
        raise BiliError(code, f"{what}失败：{obj.get('message') or ''} {hint}".strip())
    return obj


def inflate(raw: bytes) -> str:
    """B 站弹幕 XML 常是裸 deflate，urllib 不会自动解。"""
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS | 32, zlib.MAX_WBITS):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8", "ignore")
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def is_hidden_attr(attr: Any) -> bool:
    try:
        a = int(attr or 0)
    except Exception:
        return False
    return any(a & bit for bit in ATTR_HIDDEN_BITS)


def _clean(v: Any) -> str:
    return "".join(ch for ch in str(v) if ch not in "!'()*")


def _squash(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


@contextlib.contextmanager
def _file_lock(path: Path):
    """跨进程互斥（POSIX）。Windows 下退化成无锁，靠调用约定。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Net:
    """一个 store 目录一份网络状态。所有方法都可能抛 BiliError/RiskPaused/BudgetExceeded。"""

    def __init__(self, root: str | Path, *, limits: dict[str, Any] | None = None,
                 timeout: int = 20, cred: dict[str, str] | None = None):
        self.root = Path(root).expanduser()
        self.lim = {**DEFAULT_LIMITS, **(limits or {})}
        self.timeout = timeout
        self._cred = cred
        self._mixin: str = ""

    # ── 路径 ────────────────────────────────────────────────
    @property
    def dir(self) -> Path:
        d = self.root / "bili"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def shots(self) -> Path:
        d = self.root / "shots" / "bili"
        for sub in ("covers", "frames"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    # ── 凭证 ────────────────────────────────────────────────
    def cred(self) -> dict[str, str]:
        if self._cred:
            return self._cred
        cands = [self.root / "bili" / "credential.json"]
        cands += [Path(p).expanduser() for p in CRED_CANDIDATES]
        for p in cands:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sess = d.get("sessdata") or d.get("SESSDATA") or d.get("sessData")
            jct = d.get("bili_jct") or d.get("BILI_JCT") or d.get("biliJct")
            if not (sess and jct):
                continue
            self._cred = {
                "sessdata": str(sess),
                "bili_jct": str(jct),
                "buvid3": str(d.get("buvid3") or d.get("BUVID3") or ""),
                "dedeuserid": str(d.get("dedeuserid") or d.get("DedeUserID") or ""),
                "path": str(p),
            }
            return self._cred
        raise NoCredential("找不到 B 站凭证（放 ~/.companion/bili/credential.json 或 ~/.bilibili-cli/credential.json）")

    def cookie(self) -> str:
        c = self.cred()
        parts = [f"SESSDATA={c['sessdata']}", f"bili_jct={c['bili_jct']}"]
        if c.get("buvid3"):
            parts.append(f"buvid3={c['buvid3']}")
        return "; ".join(parts)

    # ── 节流 / 预算 / 熔断 ───────────────────────────────────
    @property
    def _traffic_path(self) -> Path:
        return self.dir / "traffic.json"

    def _read_traffic(self) -> dict[str, Any]:
        try:
            d = json.loads(self._traffic_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write_traffic(self, st: dict[str, Any]) -> None:
        p = self._traffic_path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def traffic(self) -> dict[str, Any]:
        """今天的状态；跨天自动清零（但不重置 strikes，风控记性比日期长）。"""
        st = self._read_traffic()
        today = date.today().isoformat()
        if st.get("day") != today:
            return {"day": today, "read": 0, "write": 0, "last": 0,
                    "pause_until": 0, "strikes": int(st.get("strikes") or 0),
                    "last_write": 0}
        return st

    def paused_for(self) -> int:
        """还在冷静期就返回剩余秒数，否则 0。"""
        left = float(self.traffic().get("pause_until") or 0) - time.time()
        return max(0, int(left))

    def guard(self, kind: str = "read", wait: bool = True) -> None:
        """请求前过闸：熔断 / 额度 / 间隔。任何一项不过就抛异常。"""
        with _file_lock(self.dir / "traffic.lock"):
            now = time.time()
            st = self.traffic()
            if float(st.get("pause_until") or 0) > now:
                raise RiskPaused(f"风控冷静期，还剩 {int(float(st['pause_until']) - now)} 秒")
            if kind == "read" and int(st.get("read") or 0) >= self.lim["read_per_day"]:
                raise BudgetExceeded(f"今日读额度用完（{self.lim['read_per_day']}）")
            if kind == "write":
                if int(st.get("write") or 0) >= self.lim["write_per_day"]:
                    raise BudgetExceeded(f"今日写额度用完（{self.lim['write_per_day']}）")
                gap = self.lim["write_gap_ms"] / 1000 - (now - float(st.get("last_write") or 0))
                if gap > 0:
                    if not wait:
                        raise BudgetExceeded(f"写冷却中，还需等 {int(gap)} 秒")
                    time.sleep(min(gap, 180))
            gap = self.lim["interval_ms"] / 1000 - (now - float(st.get("last") or 0))
            if gap > 0 and wait:
                time.sleep(min(gap, 5))
            st["day"] = date.today().isoformat()
            st["last"] = time.time()
            st[kind] = int(st.get(kind) or 0) + 1
            if kind == "write":
                st["last_write"] = st["last"]
            self._write_traffic(st)

    def note_status(self, status: int) -> None:
        """412 = 被风控盯上。记一次，阶梯冷静。"""
        if status not in (412, 429):
            return
        with _file_lock(self.dir / "traffic.lock"):
            st = self.traffic()
            strikes = int(st.get("strikes") or 0) + 1
            steps = self.lim["pause_steps_min"]
            st["strikes"] = strikes
            st["pause_until"] = time.time() + steps[min(strikes - 1, len(steps) - 1)] * 60
            self._write_traffic(st)

    # ── WBI 签名 ────────────────────────────────────────────
    def mixin(self) -> str:
        if self._mixin:
            return self._mixin
        p = self.dir / "wbi.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - float(d.get("at") or 0) < 6 * 3600 and d.get("mixin"):
                self._mixin = str(d["mixin"])
                return self._mixin
        except Exception:
            pass
        nav = self.get_json("https://api.bilibili.com/x/web-interface/nav", kind="read")
        img = ((nav or {}).get("data") or {}).get("wbi_img") or {}
        iu, su = str(img.get("img_url") or ""), str(img.get("sub_url") or "")
        pick = lambda u: u[u.rfind("/") + 1: u.rfind(".")]  # noqa: E731
        raw = pick(iu) + pick(su)
        mixin = "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]
        if not mixin:
            raise BiliError(-1, "拿不到 WBI 密钥（nav 未登录？）")
        p.write_text(json.dumps({"mixin": mixin, "at": time.time()}), encoding="utf-8")
        self._mixin = mixin
        return mixin

    def signed(self, params: dict[str, Any]) -> str:
        """返回可直接拼到 URL 上的 'a=1&b=2&wts=..&w_rid=..'。"""
        mixin = self.mixin()
        p = {k: v for k, v in params.items() if v is not None}
        p["wts"] = int(time.time())
        qs = "&".join(
            f"{urllib.parse.quote(str(k))}={urllib.parse.quote(_clean(v))}"
            for k, v in sorted(p.items())
        )
        return f"{qs}&w_rid={hashlib.md5((qs + mixin).encode()).hexdigest()}"

    # ── 请求 ────────────────────────────────────────────────
    def _open(self, req: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            self.note_status(e.code)
            if e.code == 412:
                raise RiskPaused("B 站返回 412（风控），已熔断") from e
            raise BiliError(f"HTTP{e.code}", str(e.reason or ""), req.full_url) from e
        except urllib.error.URLError as e:
            raise BiliError(-1, f"连不上 B 站：{e.reason}", req.full_url) from e

    def get_bytes(self, url: str, *, referer: str = REFERER, kind: str = "read",
                  cookie: bool = True, wait: bool = True) -> bytes:
        self.guard(kind, wait=wait)
        h = {"user-agent": UA, "referer": referer, "accept-language": "zh-CN,zh;q=0.9"}
        if cookie:
            h["cookie"] = self.cookie()
        return self._open(urllib.request.Request(url, headers=h))

    def get_json(self, url: str, params: dict[str, Any] | None = None, *,
                 signed: bool = False, referer: str = REFERER, kind: str = "read",
                 cookie: bool = True, allow_html: bool = False,
                 wait: bool = True) -> dict[str, Any]:
        if params:
            url = f"{url}?{self.signed(params) if signed else urllib.parse.urlencode(params)}"
        raw = self.get_bytes(url, referer=referer, kind=kind, cookie=cookie, wait=wait)
        text = raw.decode("utf-8", "ignore")
        try:
            return json.loads(text)
        except Exception:
            if allow_html:
                return {"code": -1, "message": "non-JSON", "raw": text[:2000]}
            raise BiliError(-1, "响应不是 JSON", url)

    def post_form(self, url: str, params: dict[str, Any], *, signed: bool = True,
                  referer: str = REFERER, kind: str = "write") -> dict[str, Any]:
        final = f"{url}?{self.signed(params)}" if signed else url
        body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        self.guard(kind)
        h = {
            "user-agent": UA, "referer": referer, "origin": REFERER,
            "content-type": "application/x-www-form-urlencoded",
            "cookie": self.cookie(), "accept-language": "zh-CN,zh;q=0.9",
        }
        raw = self._open(urllib.request.Request(final, data=body, headers=h, method="POST"))
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            raise BiliError(-1, "写接口响应不是 JSON（可能被网关拦了）", final)

    # ── 登录态与视频 ─────────────────────────────────────────
    def nav(self) -> dict[str, Any]:
        return check(self.get_json("https://api.bilibili.com/x/web-interface/nav"), "登录态")

    def me(self) -> dict[str, Any]:
        """{mid, uname, vip, money}；没登录就是 isLogin=false。"""
        d = (self.nav().get("data") or {})
        return {"mid": d.get("mid"), "uname": d.get("uname"),
                "is_login": bool(d.get("isLogin")), "level": d.get("level_info", {}).get("current_level")}

    def view(self, bvid: str) -> dict[str, Any]:
        return check(self.get_json("https://api.bilibili.com/x/web-interface/view",
                                  {"bvid": bvid}), "视频信息")["data"]

    def replies(self, aid: int, sort: int = 2, ps: int = 20, *, cookie: bool = True) -> list[dict[str, Any]]:
        d = self.get_json("https://api.bilibili.com/x/v2/reply",
                          {"type": 1, "oid": aid, "pn": 1, "ps": ps, "sort": sort},
                          cookie=cookie)
        return ((d.get("data") or {}).get("replies") or []) if d.get("code") == 0 else []

    # ── 候选池：五个入口，都是只读 ────────────────────────────
    def popular(self, ps: int = 20) -> list[dict[str, Any]]:
        d = self.get_json("https://api.bilibili.com/x/web-interface/popular", {"ps": ps, "pn": 1})
        return list(((d.get("data") or {}).get("list")) or [])

    def ranking(self) -> list[dict[str, Any]]:
        d = self.get_json("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"})
        return list(((d.get("data") or {}).get("list")) or [])

    def related(self, bvid: str) -> list[dict[str, Any]]:
        d = self.get_json("https://api.bilibili.com/x/web-interface/archive/related", {"bvid": bvid})
        return list(d.get("data") or [])

    def search(self, keyword: str, ps: int = 20) -> list[dict[str, Any]]:
        d = self.get_json("https://api.bilibili.com/x/web-interface/wbi/search/type",
                          {"search_type": "video", "keyword": keyword, "page": 1, "page_size": ps},
                          signed=True)
        return list(((d.get("data") or {}).get("result")) or [])

    def dynamics(self) -> list[dict[str, Any]]:
        """关注的人最近发了什么。结构跟其它入口不一样，调用方要过 normalize。"""
        d = self.get_json("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                          {"type": "video", "page": 1})
        items = ((d.get("data") or {}).get("items")) or []
        out: list[dict[str, Any]] = []
        for it in items:
            major = (it.get("modules") or {}).get("module_dynamic", {}).get("major") or {}
            arc = major.get("archive") or {}
            if arc.get("bvid"):
                out.append({
                    "bvid": arc.get("bvid"),
                    "title": arc.get("title"),
                    "cover": arc.get("cover"),
                    "duration_text": arc.get("duration_text"),
                    "owner": {"name": ((it.get("modules") or {}).get("module_author") or {}).get("name")},
                    "pubdate": ((it.get("modules") or {}).get("module_author") or {}).get("pub_ts"),
                    "stat": {"view": _view_count(arc.get("stat", {}).get("play"))},
                    "_src": "dynamic",
                })
        return out

    # ── 深读素材 ────────────────────────────────────────────
    def subtitles(self, bvid: str, cid: int) -> tuple[str, str, list[dict[str, Any]]]:
        """返回 (纯文本, 来源, 带时间戳条目)。必须走 wbi 主端点，否则 subtitle_url 缺 auth_key。"""
        d = self.get_json("https://api.bilibili.com/x/player/wbi/v2",
                          {"bvid": bvid, "cid": cid}, signed=True,
                          referer=f"https://www.bilibili.com/video/{bvid}/", allow_html=True)
        subs = [s for s in (((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
                if s.get("subtitle_url")]
        if not subs:
            return "", "none", []
        pick = (next((s for s in subs if str(s.get("lan", "")).startswith("ai-zh")), None)
                or next((s for s in subs if "zh" in str(s.get("lan", ""))), None)
                or subs[0])
        url = str(pick["subtitle_url"])
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        try:
            body = json.loads(self.get_bytes(url, referer=f"https://www.bilibili.com/video/{bvid}/")
                              .decode("utf-8", "ignore"))
        except Exception as e:
            return "", f"subtitle_error:{type(e).__name__}", []
        items = [{"t": float(it.get("from") or 0), "text": _squash(it.get("content"))}
                 for it in (body.get("body") or [])]
        return "".join(i["text"] for i in items), f"subtitle:{pick.get('lan')}", items

    def danmaku(self, cid: int, *, bucket: int = 30, top: int = 5) -> list[dict[str, Any]]:
        """弹幕按时间桶统计密度 —— 「观众在哪炸了」比弹幕全文有用得多。"""
        try:
            raw = self.get_bytes(f"https://comment.bilibili.com/{cid}.xml")
        except Exception:
            return []
        rows = re.findall(r'<d p="([^"]*)">([^<]*)</d>', inflate(raw))
        buckets: dict[int, list[str]] = {}
        for p, text in rows:
            try:
                t = float(str(p).split(",")[0])
            except Exception:
                continue
            buckets.setdefault(int(t // bucket) * bucket, []).append(_squash(text))
        ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
        out = [{"at": f"{s // 60:02d}:{s % 60:02d}", "count": len(ts), "samples": ts[:4]}
               for s, ts in ranked]
        return sorted(out, key=lambda x: x["at"])

    def comments(self, aid: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = sorted(self.replies(aid, sort=2, ps=max(20, limit * 2)),
                      key=lambda x: int(x.get("like") or 0), reverse=True)[:limit]
        return [{
            "author": _squash((r.get("member") or {}).get("uname"))[:16],
            "text": _squash((r.get("content") or {}).get("message"))[:160],
            "like": int(r.get("like") or 0),
            "hidden": is_hidden_attr(r.get("attr")),
        } for r in rows]

    def cover(self, bvid: str, pic_url: str) -> str:
        if not pic_url:
            return ""
        p = self.shots / "covers" / f"{bvid}.jpg"
        if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
            return str(p)
        try:
            p.write_bytes(self.get_bytes(pic_url, referer=f"https://www.bilibili.com/video/{bvid}/"))
            return str(p)
        except Exception:
            return ""

    def play_url(self, bvid: str, cid: int) -> str:
        d = self.get_json("https://api.bilibili.com/x/player/playurl",
                          {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 16, "fourk": 0},
                          referer=f"https://www.bilibili.com/video/{bvid}/")
        data = d.get("data") or {}
        vids = (data.get("dash") or {}).get("video") or []
        if vids:
            return str(vids[0].get("baseUrl") or vids[0].get("base_url") or "")
        durl = data.get("durl") or []
        return str(durl[0].get("url") or "") if durl else ""

    def frames(self, bvid: str, cid: int, duration: int, grid: int = 3) -> str:
        """九宫格抽帧：整片均匀取 grid² 帧拼一张图，交给她用 vision 看。"""
        ff = shutil.which("ffmpeg")
        if not ff:
            raise BiliError(-1, "没装 ffmpeg（抽帧要用）")
        url = self.play_url(bvid, cid)
        if not url:
            raise BiliError(-1, "拿不到视频流地址")
        out = self.shots / "frames" / f"{bvid}-{grid}x{grid}.jpg"
        dur = max(10, int(duration or 60))
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-headers", f"Referer: https://www.bilibili.com/video/{bvid}/\r\n"
                        f"User-Agent: {UA}\r\n",
            "-i", url, "-t", str(dur),
            "-vf", f"fps={(grid * grid)}/{dur},scale=480:-1,tile={grid}x{grid}",
            "-frames:v", "1", "-q:v", "4", "-y", str(out),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
        except subprocess.CalledProcessError as e:
            raise BiliError(-1, f"抽帧失败：{(e.stderr or b'')[:200].decode('utf-8', 'ignore')}")
        except subprocess.TimeoutExpired:
            raise BiliError(-1, "抽帧超时（180s）")
        return str(out) if out.exists() else ""

    # ── 写接口（都要过写额度）──────────────────────────────────
    def relation(self, mid: int) -> dict[str, Any]:
        d = check(self.get_json("https://api.bilibili.com/x/relation", {"fid": mid}), "关注关系")
        data = d.get("data") or {}
        return {"attribute": int(data.get("attribute") or -1),
                "be_attribute": (data.get("be_relation") or {}).get("attribute")}

    def dev_id(self) -> str:
        """设备的稳定标识。变了会让私信被判为新设备，直接发不出去。"""
        p = self.dir / "dev_id"
        try:
            v = p.read_text(encoding="utf-8").strip()
            if len(v) >= 20:
                return v
        except Exception:
            pass
        v = "WEB-" + str(uuid.uuid4()).upper()
        p.write_text(v, encoding="utf-8")
        try:
            p.chmod(0o600)
        except Exception:
            pass
        return v

    def send_dm(self, to_mid: int, text: str) -> dict[str, Any]:
        """私信主人。前提是互关 —— 这也是唯一不会进影子审核的通道。"""
        rel = self.relation(to_mid)
        if rel["attribute"] != 6:
            raise BiliError(-1, f"和 mid {to_mid} 不是互关（attribute={rel['attribute']}）→ 私信发不出去")
        me = str((self.nav().get("data") or {}).get("mid"))
        params = {
            "msg[sender_uid]": me,
            "msg[receiver_id]": str(to_mid),
            "msg[receiver_type]": "1",
            "msg[msg_type]": "1",
            "msg[msg_status]": "0",
            "msg[timestamp]": str(int(time.time())),
            "msg[dev_id]": self.dev_id(),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": self.cred()["bili_jct"],
        }
        out = check(self.post_form("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
                                   params, signed=False,
                                   referer="https://message.bilibili.com/"), "发私信")
        return {"to": to_mid, "code": out.get("code"), "len": len(text)}

    def send_comment(self, bvid: str, aid: int, text: str) -> dict[str, Any]:
        """评论区留言。**大概率只有自己看得见**（attr 2**38），所以只当补充渠道。"""
        msg = text.strip()
        if len(msg) > 1000:
            raise BiliError(-1, "评论超过 1000 字")
        params = {"type": 1, "oid": str(aid), "message": msg, "plat": "1",
                  "csrf": self.cred()["bili_jct"]}
        out = check(self.post_form("https://api.bilibili.com/x/v2/reply/add", params,
                                   referer=f"https://www.bilibili.com/video/{bvid}/"), "发评论")
        data = out.get("data") or {}
        rpid = str(data.get("rpid_str") or data.get("rpid") or "")
        return {"aid": aid, "rpid": rpid, "url": f"https://www.bilibili.com/video/{bvid}/#reply{rpid}"}

    def verify_comment(self, aid: int, rpid: str) -> dict[str, Any]:
        """三层对照：自己看得见？陌生人看得见？attr 有没有隐藏位。"""
        rpid = str(rpid)
        mine = next((x for x in self.replies(aid, sort=0, ps=30) if str(x.get("rpid")) == rpid), None)
        anon = next((x for x in self.replies(aid, sort=0, ps=30, cookie=False)
                     if str(x.get("rpid")) == rpid), None)
        attr = int((mine or {}).get("attr") or 0)
        hidden = is_hidden_attr(attr)
        if mine and anon and not hidden:
            verdict = "public"
        elif mine and (hidden or not anon):
            verdict = "self_only"
        elif not mine:
            verdict = "missing"
        else:
            verdict = "unknown"
        return {"rpid": rpid, "seen_by_me": bool(mine), "seen_anonymous": bool(anon),
                "attr": attr, "hidden_bit": hidden, "verdict": verdict,
                "meaning": {
                    "public": "真公开：陌生人也能刷到",
                    "self_only": "仅自己可见（影子审核）→ 改走私信",
                    "missing": "查不到这条（可能已被删/被吞）",
                    "unknown": "判断不了",
                }[verdict]}


def _view_count(v: Any) -> int:
    """动态接口的播放量是 '1.2万' 这种字符串。"""
    s = str(v or "0")
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        return int(float(s))
    except Exception:
        return 0
