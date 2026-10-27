// companion-kit 的 DSH 适配器（cordis 插件）。
//
// 这一层刻意很薄：人格、记忆、心情、作息、自主决策全在 Python core 里。
// 这里只做三件事：
//   1. 每 5 分钟问一次 core「现在该干什么」（tick，本身不花 token）
//   2. 有 wake_due 就用 sessionController.prompt 把她叫起来
//   3. 开两个只读路由，方便设置页/调试看状态
//
// 环境变量：
//   COMPANION_HOME     store 目录（默认 ~/.companion）
//   COMPANION_PYTHON   跑 core 的 python（默认 python3；建议指向装了本包的 venv）

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// 包内自带的 Python 源码目录：宿主机没 pip 装过也能跑（只要有 python3.10+）
const PKG_SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src')
const SEP = process.platform === 'win32' ? ';' : ':'

// 默认 5 分钟；COMPANION_TICK_MS 可覆盖（调试用，别在生产设太小）
const TICK_MS = Number(process.env.COMPANION_TICK_MS) || 5 * 60 * 1000
// 投递到哪个会话。DSH 里每个工作区会话有自己的 id，默认值多半不存在，
// 用 COMPANION_DSH_SESSION 指定；投递失败时会打日志提示。
const SESSION = process.env.COMPANION_DSH_SESSION || 'companion'

function home() {
  return process.env.COMPANION_HOME || path.join(os.homedir(), '.companion')
}

function pyBin() {
  // 优先级：显式指定 > 一行安装建的 venv > PATH 里的 python3/python。
  // Windows 上 `python` 常常是应用商店的占位符，所以 venv 要排在前面。
  if (process.env.COMPANION_PYTHON) return process.env.COMPANION_PYTHON
  const venv = process.platform === 'win32'
    ? path.join(home(), 'venv', 'Scripts', 'python.exe')
    : path.join(home(), 'venv', 'bin', 'python')
  try {
    if (fs.existsSync(venv)) return venv
  } catch {}
  return process.platform === 'win32' ? 'python' : 'python3'
}

/** 调一次 CLI，拿 JSON。失败不抛，返回 null —— 插件不能把宿主拖死。 */
function runJson(args, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let out = ''
    let err = ''
    let done = false
    let child
    try {
      child = spawn(pyBin(), ['-m', 'companion', '--json', ...args], {
        env: {
          ...process.env,
          COMPANION_HOME: home(),
          PYTHONPATH: process.env.PYTHONPATH
            ? `${PKG_SRC}${SEP}${process.env.PYTHONPATH}`
            : PKG_SRC,
        },
        windowsHide: true,
      })
    } catch (e) {
      resolve({ ok: false, error: String(e && e.message) })
      return
    }
    const timer = setTimeout(() => {
      if (!done) {
        done = true
        try { child.kill() } catch {}
        resolve({ ok: false, error: 'timeout' })
      }
    }, timeoutMs)
    child.stdout.on('data', (d) => { out += d })
    child.stderr.on('data', (d) => { err += d })
    child.on('error', (e) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve({ ok: false, error: String(e && e.message) })
    })
    child.on('close', (code) => {
      if (done) return
      done = true
      clearTimeout(timer)
      if (code !== 0) {
        resolve({ ok: false, error: err.trim() || `exit ${code}` })
        return
      }
      try {
        resolve({ ok: true, data: JSON.parse(out) })
      } catch (e) {
        resolve({ ok: false, error: 'bad json: ' + out.slice(0, 120) })
      }
    })
  })
}

export default {
  name: 'dsh-companion',
  inject: ['sessionController'],
  apply(ctx) {
    let stopped = false
    let busy = false
    const log = (msg) => {
      try { console.log('[dsh-companion] ' + msg) } catch {}
    }

    /** 一轮：tick → 拉事件 → 有 wake_due 就叫她 */
    async function loop() {
      if (stopped || busy) return
      busy = true
      try {
        const tick = await runJson(['tick'])
        if (!tick.ok) {
          log('tick 失败：' + (tick.error || '未知'))
          return
        }
        const ev = await runJson(['events', '--since', String(ctx._cursor ?? 0)])
        if (!ev.ok) return
        const rows = (ev.data && ev.data.events) || []
        ctx._cursor = (ev.data && ev.data.cursor) || ctx._cursor || 0
        const wake = rows.filter((e) => e && e.kind === 'wake_due')
        if (!wake.length) return

        const e = wake[wake.length - 1]
        const text = `【该你了】${e.reason || '想找他说话'}\n（动作：${e.action}；先 ` +
          '`companion context` 拿一遍本轮上下文，再决定说什么）'
        const controller = ctx.get ? ctx.get('sessionController') : ctx.sessionController
        if (!controller || typeof controller.prompt !== 'function') {
          log('没有 sessionController，跳过投递')
          return
        }
        try {
          await controller.prompt({
            sessionId: SESSION,
            mode: 'queue',
            content: [{ type: 'text', text }],
          })
          log(`投递 wake_due：${e.action}`)
        } catch (err) {
          log(`投递失败（sessionId=${SESSION}）：` + String(err && err.message) +
            ' —— 用 COMPANION_DSH_SESSION 指定你 DSH 里真实存在的会话 id')
        }
      } finally {
        busy = false
      }
    }

    const timer = setInterval(() => { loop().catch(() => {}) }, TICK_MS)
    ctx.effect(() => () => {
      stopped = true
      clearInterval(timer)
    })

    // 只读路由：给设置页 / 调试用。
    // ⚠️ 必须用 ctx.get('webServer') 而不是 ctx.webServer —— cordis 里访问未 inject
    //    的服务会直接抛 "cannot get property ... without inject"，整条插件激活失败。
    //    没网页层的 profile 拿不到它，这时跳过路由，主循环照跑。
    const webServer = (typeof ctx.get === 'function' ? ctx.get('webServer') : null) || null
    if (webServer && typeof webServer.register === 'function') {
      const reg = (p, fn) => ctx.effect(() => webServer.register({
        kind: 'exact',
        path: p,
        handler: async (req, res) => {
          try {
            const body = await fn()
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify(body))
          } catch (e) {
            res.statusCode = 500
            res.end(JSON.stringify({ ok: false, error: String(e && e.message) }))
          }
        },
      }))
      reg('/dsh-companion/context', async () => {
        const r = await runJson(['context', '--budget', '1600'])
        return r.ok ? r.data : r
      })
      reg('/dsh-companion/status', async () => {
        const [c, k] = await Promise.all([runJson(['cost']), runJson(['tick'])])
        return { cost: c.ok ? c.data : c, tick: k.ok ? k.data : k, home: home() }
      })
    }

    log('已加载，store=' + home())
  },
}
