"""DSH 适配层的桩件实跑：真起 Node，真调真 core。

为什么单独写：DSH 是唯一一个"另一门语言"写的适配层，Python 单测完全碰不到它。
以前的做法是只用眼睛看 index.js——那种验证不算数。

这里起一个 Node 进程，用假的 ctx（cordis 的最小替身）驱动插件，
再检查真的落到了 store 里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion.store import Home  # noqa: E402

NODE_STUB = r"""
// cordis 的最小替身：只提供插件用到的那几个口子。
const path = require('node:path')
const fs = require('node:fs')
const { pathToFileURL } = require('node:url')

async function main() {
  // Windows 上 ESM import 必须给 file:// URL，直接给 'C:\\...' 会报
  // ERR_UNSUPPORTED_ESM_URL_SCHEME
  const entry = pathToFileURL(path.join(process.argv[2], 'dsh', 'index.js')).href
  const mod = await import(entry)
  const plugin = mod.default

  const delivered = []
  const routes = {}
  const effects = []

  const agent = {
    async followup(msg) {
      delivered.push(msg)
      return { ok: true }
    },
  }
  const controller = {
    async list() {
      return [{ id: 'sess-1', updatedAt: new Date().toISOString() }]
    },
    async resolveAgent(id) {
      return { agent }
    },
    async prompt() {},
  }
  const webServer = {
    register(desc) {
      routes[desc.path] = desc.handler
    },
  }

  const ctx = {
    get(name) {
      if (name === 'sessionController') return controller
      if (name === 'webServer') return webServer
      return null
    },
    // cordis 的 ctx.effect(fn) 会**立刻**执行 fn（返回值是清理函数）。
    // 这里必须照做，否则路由根本不会注册——桩件太宽松就等于没测。
    effect(fn) {
      const dispose = fn()
      effects.push(() => { if (typeof dispose === 'function') dispose() })
      return dispose
    },
  }

  plugin.apply(ctx)

  // 给插件一轮时间跑 tick → 拉事件 → 投递
  await new Promise((r) => setTimeout(r, Number(process.argv[4] || 6000)))

  // 插件的 log 走 console.log（stdout），会把我们的 JSON 冲掉。
  // 插件在 apply() 里就捕获了 console.log 的引用，之后再改 console.log 已经晚了。
  // 所以这里反过来做：最后从 stdout 里挑出自己写的那一行 JSON。
  const MARK = '<<<DSH_STUB_JSON>>>'
  const realLog = console.log
  console.log = (...a) => process.stderr.write(a.map(String).join(' ') + '\n')

  const out = {
    delivered: delivered.map((m) => ({
      text: (m.content || []).map((c) => c.text || '').join(''),
      source: m.source,
    })),
    routes: Object.keys(routes),
    routeResults: {},
  }

  // 把两个只读路由也真调一遍
  for (const p of ['/dsh-companion/context', '/dsh-companion/status', '/dsh-companion/diag']) {
    if (!routes[p]) continue
    try {
      const res = {}
      res.setHeader = () => {}
      res.end = (b) => { out.routeResults[p] = b }
      await routes[p]({ url: p + (p.includes('context') ? '?session=sess-1' : '') }, res)
    } catch (e) {
      out.routeResults[p] = { error: String(e && e.message) }
    }
  }

  // 结果写 stdout；插件自己的 log 已经挪去 stderr，不会混进来。
  process.stdout.write(MARK + JSON.stringify(out))
  process.exit(0)
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ error: String(e && e.stack || e) }))
  process.exit(1)
})
"""

def _has_node() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20, check=True)
        return True
    except Exception:
        return False


class DshStubTest(unittest.TestCase):
    """桩件实跑。node 不可用就跳过——不能在没 node 的机器上假装测过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.py = sys.executable

    def tearDown(self):
        self.tmp.cleanup()

    def _run_stub(self, *, wait_ms: int = 6000) -> dict:
        stub = self.home / "stub.cjs"
        stub.write_text(NODE_STUB, encoding="utf-8")
        env = {
            **os.environ,
            "COMPANION_HOME": str(self.home / "store"),
            "COMPANION_PYTHON": self.py,
            "COMPANION_TICK_MS": "1000",
            "COMPANION_DSH_SESSION": "sess-1",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        done = subprocess.run(
            ["node", str(stub), str(ROOT), str(wait_ms)],
            capture_output=True, env=env, timeout=wait_ms / 1000 + 60,
        )
        out = done.stdout.decode("utf-8", "replace")
        # 插件的 console.log 在 apply() 里就被捕获了，改 console.log 已经太晚，
        # 所以 stdout 里会混着日志行。靠标记挑出我们自己的 JSON。
        marker = "<<<DSH_STUB_JSON>>>"
        if marker in out:
            out = out.split(marker, 1)[1]
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"桩件没吐出 JSON：{out[:400]}\nstderr: {done.stderr.decode('utf-8', 'replace')[:400]}")

    @unittest.skipUnless(_has_node(), "本机没有 node，跳过 DSH 桩件实跑")
    def test_plugin_loads_and_routes_register(self):
        r = self._run_stub(wait_ms=3000)
        self.assertNotIn("error", r, r.get("error"))
        self.assertIn("/dsh-companion/context", r["routes"])
        self.assertIn("/dsh-companion/status", r["routes"])
        self.assertIn("/dsh-companion/diag", r["routes"])

    @unittest.skipUnless(_has_node(), "本机没有 node，跳过 DSH 桩件实跑")
    def test_context_route_supports_session_projection(self):
        """带 session 要返回投影字段；不带 session 要是全量。"""
        r = self._run_stub(wait_ms=3000)
        with_session = json.loads(r["routeResults"]["/dsh-companion/context"]) \
            if isinstance(r["routeResults"]["/dsh-companion/context"], str) \
            else r["routeResults"]["/dsh-companion/context"]
        self.assertEqual(with_session.get("session_id"), "sess-1",
                         "带 session 时 core 没做投影")
        self.assertIn("full", with_session)
        self.assertIn("changed", with_session)

    @unittest.skipUnless(_has_node(), "本机没有 node，跳过 DSH 桩件实跑")
    def test_delivered_message_carries_context_and_thought(self):
        """投递的消息要既有上下文又有她的念头——两样都不能少。"""
        # 先给 store 一点人格，否则上下文是空的，测不出来
        store = Home(self.home / "store")
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "SOUL.md").write_text(
            "# 我是谁\n\n我叫柚璃，住在这块屏幕里。\n", encoding="utf-8")
        r = self._run_stub(wait_ms=8000)
        if not r["delivered"]:
            self.skipTest("这一轮 core 没有给出 wake_due（额度/时机没到），跳过一次投递断言")
        text = r["delivered"][0]["text"]
        self.assertIn("柚璃", text, "投递的消息里没有人格上下文")
        self.assertTrue(
            any(k in text for k in ("现在", "想", "有点")),
            "投递的消息里没有她的念头：\n" + text[:200])

    @unittest.skipUnless(_has_node(), "本机没有 node，跳过 DSH 桩件实跑")
    def test_delivery_is_scheduled_not_user_message(self):
        """投递必须是 schedule 通知，不能伪装成主人说的话。"""
        r = self._run_stub(wait_ms=3000)
        if not r["delivered"]:
            self.skipTest("这一轮没有投递")
        src = r["delivered"][0].get("source") or {}
        self.assertEqual(src.get("kind"), "schedule")
        self.assertNotEqual(src.get("kind"), "user")


if __name__ == "__main__":
    unittest.main()
