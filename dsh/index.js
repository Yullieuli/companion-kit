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

function logDelivery(homeDir, row) {
  try {
    const dir = path.join(homeDir, 'logs')
    fs.mkdirSync(dir, { recursive: true })
    fs.appendFileSync(path.join(dir, 'delivery.jsonl'), JSON.stringify(row) + '\n')
  } catch {}
}

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


/** 解析要投递的会话：
 *  1) COMPANION_DSH_SESSION 显式指定
 *  2) sessionController.list() 里挑一个（优先最近的）
 *  list() 在 DSH 里是异步的，返回结构还可能是 array / {sessions} / map，所以全都兜住。
 */
async function resolveSession(controller) {
  if (process.env.COMPANION_DSH_SESSION) return process.env.COMPANION_DSH_SESSION
  try {
    if (!controller || typeof controller.list !== 'function') return ''
    const raw = await controller.list()
    let arr = []
    if (Array.isArray(raw)) arr = raw
    else if (raw && Array.isArray(raw.sessions)) arr = raw.sessions
    else if (raw && Array.isArray(raw.items)) arr = raw.items
    else if (raw && typeof raw === 'object') arr = Object.values(raw)
    arr = arr.flat().filter((x) => x && typeof x === 'object')
    const idOf = (x) => x.id || x.sessionId || x.key || x.session_id
    const scored = arr
      .filter((x) => idOf(x))
      .sort((a, b) => {
        const ta = Date.parse(a.updatedAt || a.lastActiveAt || a.createdAt || 0) || 0
        const tb = Date.parse(b.updatedAt || b.lastActiveAt || b.createdAt || 0) || 0
        return tb - ta
      })
    return scored.length ? String(idOf(scored[0])) : ''
  } catch {
    return ''
  }
}

/** 调 sessionController.prompt。
 *
 * 真实签名（从 DSH 的 app.asar 里挖出来的，别猜）：
 *   prompt(request: SessionPromptRequest, signal: AbortSignal)
 *   SessionPromptRequest { requestId, sessionId, mode: 'queue'|'steer', content: PromptContentPart[] }
 * 第一行就是 signal.throwIfAborted()，所以 signal 必须是真的 AbortSignal。
 */
let _reqSeq = 0
function newRequestId() {
  _reqSeq += 1
  return `companion-${Date.now()}-${_reqSeq}`
}

async function promptSession(controller, sessionId, text) {
  const ac = new AbortController()
  const request = {
    requestId: newRequestId(),
    sessionId,
    mode: 'queue',
    content: [{ type: 'text', text }],
  }
  try {
    await controller.prompt(request, ac.signal)
    return { ok: true, shape: 'request+signal', error: '' }
  } catch (e) {
    return { ok: false, shape: '', error: String((e && e.message) || e) }
  }
}

export default {
  name: 'dsh-companion',
  inject: ['sessionController'],
  apply(ctx) {
    let stopped = false
    let busy = false
    // ⚠️ 游标必须放本地。挂在 ctx 上会在严格代理下抛
    //    "cannot get property _cursor without inject"，异常又被 setInterval 的 catch 吞掉。
    let cursor = 0
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
        const ev = await runJson(['events', '--since', String(cursor)])
        if (!ev.ok) return
        const rows = (ev.data && ev.data.events) || []
        cursor = (ev.data && ev.data.cursor) || cursor
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
        const target = await resolveSession(controller)
        if (!target) {
          log('没有可投递的会话（list() 空）→ 跳过；可用 COMPANION_DSH_SESSION 指定')
          return
        }
        const r = await promptSession(controller, target, text)
        logDelivery(home(), {
          at: new Date().toISOString(), action: e.action, session: target,
          ok: r.ok, shape: r.shape, error: r.error,
        })
        if (r.ok) log(`投递 wake_due：${e.action} → session ${target}（${r.shape}）`)
        else log(`投递失败（sessionId=${target}）：${r.error}`)
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
            const body = await fn(req)
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
      reg('/dsh-companion/ping', async (req) => {
        const c = typeof ctx.get === 'function' ? ctx.get('sessionController') : null
        if (!c || typeof c.prompt !== 'function') return { ok: false, error: '没有 sessionController.prompt' }
        const q = String((req && req.url) || '')
        const m = /[?&]session=([^&]+)/.exec(q)
        const target = m ? decodeURIComponent(m[1]) : await resolveSession(c)
        if (!target) return { ok: false, error: '解析不到会话 id' }
        const text = '【通道测试】这是 companion-kit 的一次投递测试，看到就说明通了。'
        const r = await promptSession(c, target, text)
        logDelivery(home(), { at: new Date().toISOString(), action: 'ping', session: target,
          ok: r.ok, shape: r.shape, error: r.error })
        return { session: target, ...r }
      }),
      reg('/dsh-companion/sessions', async () => {
        const c = typeof ctx.get === 'function' ? ctx.get('sessionController') : null
        if (!c || typeof c.list !== 'function') return { error: 'sessionController.list 不可用' }
        try {
          const raw = await c.list()
          const arr = Array.isArray(raw) ? raw : (raw && (raw.sessions || raw.items)) || []
          const flat = (Array.isArray(arr) ? arr : []).flat().filter((x) => x && typeof x === 'object')
          return {
            resolved: await resolveSession(c),
            type: Array.isArray(raw) ? 'array' : typeof raw,
            count: flat.length,
            sample: flat.slice(0, 3).map((x) => {
              const out = {}
              for (const k of ['id', 'sessionId', 'key', 'title', 'name', 'workspace', 'cwd',
                               'agent', 'agentId', 'updatedAt', 'createdAt']) {
                if (x[k] !== undefined) out[k] = typeof x[k] === 'string' ? x[k].slice(0, 80) : x[k]
              }
              return out
            }),
          }
        } catch (e) {
          return { error: String(e && e.message) }
        }
      }),
      reg('/dsh-companion/diag', async () => ({
        session: SESSION,
        tickMs: TICK_MS,
        hasWebServer: !!webServer,
        controller: (() => {
          const c = typeof ctx.get === 'function' ? ctx.get('sessionController') : null
          if (!c) return null
          const keys = new Set()
          let o = c
          while (o && o !== Object.prototype) {
            Object.getOwnPropertyNames(o).forEach((k) => keys.add(k))
            o = Object.getPrototypeOf(o)
          }
          return { keys: [...keys].slice(0, 60), prompt: typeof c.prompt }
        })(),
        node: process.version,
      })),
      reg('/dsh-companion/status', async () => {
        // 只读：不能在这里调 tick，否则"看一眼"就扣一次开口额度
        const [st, c] = await Promise.all([runJson(['status']), runJson(['cost'])])
        return { status: st.ok ? st.data : st, cost: c.ok ? c.data : c, home: home() }
      })
    }

    log('已加载，store=' + home())
  },
}
