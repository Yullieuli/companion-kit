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
import { PANEL_HTML } from './panel.js'

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

function sessionFile() {
  return path.join(home(), 'dsh.json')
}

function cursorFile() {
  return path.join(home(), 'dsh.cursor')
}

/** 已经报过的请示单：同一条不要反复催主人。 */
function askedFile() {
  return path.join(home(), 'dsh.asked.json')
}

function loadAsked() {
  try {
    const d = JSON.parse(fs.readFileSync(askedFile(), 'utf8'))
    return d && typeof d === 'object' ? d : {}
  } catch { return {} }
}

function saveAsked(data) {
  try {
    fs.mkdirSync(home(), { recursive: true })
    fs.writeFileSync(askedFile(), JSON.stringify(data), 'utf8')
  } catch {}
}

function loadCursor() {
  try {
    const value = Number(fs.readFileSync(cursorFile(), 'utf8').trim())
    return Number.isSafeInteger(value) && value >= 0 ? value : 0
  } catch { return 0 }
}

function saveCursor(value) {
  fs.mkdirSync(home(), { recursive: true })
  fs.writeFileSync(cursorFile(), String(value) + '\n', 'utf8')
}

/** 还没报给主人的请示单。报过的不再重复催。 */
async function pendingAsks() {
  const r = await runJson(['ask', 'list'])
  if (!r.ok) return []
  const pending = (r.data && r.data.pending) || []
  const asked = loadAsked()
  return pending.filter((p) => p && p.id && !asked[p.id])
}

/** 读/写"钉住的会话"。比环境变量好：改完不用重启 DSH。 */
function pinnedSession() {
  try {
    const d = JSON.parse(fs.readFileSync(sessionFile(), 'utf8'))
    return d && typeof d.session === 'string' ? d.session : ''
  } catch { return '' }
}

function pinSession(id) {
  try {
    fs.writeFileSync(sessionFile(), JSON.stringify({ session: id, at: new Date().toISOString() }, null, 2))
    return true
  } catch { return false }
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
  const pinned = pinnedSession()
  if (pinned) return pinned
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

/** 取这次投递要注入的上下文。
 *
 * 用 core 的 session 级增量投影：状态没变就返回空串，这一轮什么都不用注入。
 * 不用投影的话，每 5 分钟的 tick 都会把整个人格重新塞一遍，白烧 token；
 * 用了投影之后只有"她真的变了"的部分才进上下文。
 */
async function projectedContext(sessionId) {
  const args = ['context', '--budget', '1600']
  if (sessionId) args.push('--session', String(sessionId))
  const r = await runJson(args)
  if (!r.ok || !r.data) return ''
  const d = r.data
  // 带 session 时 text 是投影后的增量；不带时是全量
  const text = String(d.text || '')
  return text
}

/** 把上下文和念头拼成一条消息。
 *
 * 顺序有讲究：上下文在前，念头在后。她先"想起自己是谁"，再"想说什么"，
 * 反过来读着像系统提示。
 */
function composeMessage(contextText, thoughtText) {
  const t = String(contextText || '').trim()
  const think = String(thoughtText || '').trim()
  if (!t) return think
  if (!think) return t
  return t + '\n\n' + think
}

/** 投递一条"她自己的念头"。
 *
 * 为什么不用 controller.prompt：那个接口把消息标成 source.kind='user'，
 * 她会以为是主人说的，然后去回复那句提示 —— 语义不对。
 *
 * 正确通道（读 DSH 的 app.asar 得到）：
 *   resolveAgent(sessionId) -> { agent } | { error }
 *   agent.followup(message) / inject(message) / send(message, target, wakeup)
 *   消息 source: { kind: 'schedule', form: 'notice', summary } —— 定时/合成提示，
 *   界面上是折叠 chip，不会被当成用户发言。
 *
 * 三级回退：followup → inject → send → controller.prompt（最后手段，会变成用户消息）
 */
let _reqSeq = 0
function newId(prefix) {
  _reqSeq += 1
  const rnd = (globalThis.crypto && globalThis.crypto.randomUUID)
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${_reqSeq}`
  return `${prefix}-${rnd}`
}

function buildMessage(text) {
  return {
    id: newId('companion-msg'),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'schedule', form: 'notice', summary: '她自己的想法' },
  }
}

async function deliver(controller, sessionId, text) {
  const msg = buildMessage(text)
  let lastErr = ''

  // ── 1. 合成消息（首选）──
  let found = null
  try {
    if (typeof controller.resolveAgent === 'function') found = await controller.resolveAgent(sessionId)
  } catch (e) {
    lastErr = 'resolveAgent: ' + String((e && e.message) || e)
  }
  const agent = found && found.agent
  if (agent) {
    for (const m of ['followup', 'inject']) {
      if (typeof agent[m] !== 'function') continue
      try {
        await agent[m](msg)
        return { ok: true, channel: 'agent.' + m, error: '' }
      } catch (e) {
        lastErr = `${m}: ` + String((e && e.message) || e)
      }
    }
    if (typeof agent.send === 'function') {
      try {
        await agent.send(msg, 'next-turn', true)
        return { ok: true, channel: 'agent.send', error: '' }
      } catch (e) {
        lastErr = 'send: ' + String((e && e.message) || e)
      }
    }
  } else if (found && found.error && !lastErr) {
    lastErr = 'resolveAgent: ' + JSON.stringify(found.error).slice(0, 120)
  }

  // ── 2. 最后手段：controller.prompt（会成为 user 消息）──
  try {
    await controller.prompt(
      { requestId: newId('companion-req'), sessionId, mode: 'queue', content: [{ type: 'text', text }] },
      new AbortController().signal,
    )
    return { ok: true, channel: 'controller.prompt(user)', error: lastErr }
  } catch (e) {
    return { ok: false, channel: '', error: (lastErr ? lastErr + ' | ' : '') + String((e && e.message) || e) }
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
    // 文件让重启后的实例从已交付位置继续；只在投递成功后更新。
    let cursor = loadCursor()
    const log = (msg) => {
      try { console.log('[dsh-companion] ' + msg) } catch {}
    }

    /** 一轮：tick → 拉事件 → 有 wake_due 就叫她 */
    async function loop() {
      if (stopped || busy) return
      busy = true
      try {
        const tick = await runJson(['tick', '--host', 'dsh'])
        if (!tick.ok) {
          log('tick 失败：' + (tick.error || '未知'))
          return
        }
        const ev = await runJson(['events', '--since', String(cursor)])
        if (!ev.ok) return
        const rows = (ev.data && ev.data.events) || []
        const nextCursor = ev.data && Number.isSafeInteger(ev.data.cursor) && ev.data.cursor >= 0
          ? ev.data.cursor
          : cursor
        const wake = rows.filter((e) => e && e.kind === 'wake_due')
        const bili = rows.filter((e) => e && e.kind === 'bili_due')

        // ① 她自己过日子：去刷会儿片。core 那边有额度与风控，失败就安静。
        if (bili.length) {
          const r = await runJson(['bili', 'tick'])
          if (r.ok && r.data && r.data.ok) {
            const title = r.data.title || r.data.bvid || ''
            log('刷片完成：' + String(title).slice(0, 40))
          } else {
            log('刷片没成：' + ((r.error || (r.data && r.data.why)) || '未知'))
          }
        }

        // ② 她在等主人点头的事：没人呈现就等于没问过。一次报一条。
        const todo = await pendingAsks()
        if (todo.length) {
          const controller0 = ctx.get ? ctx.get('sessionController') : ctx.sessionController
          const target0 = await resolveSession(controller0)
          if (target0 && controller0) {
            const item = todo[0]
            // ask 记录的 say 本身就是完整问句（"我想电脑上打开chrome，可以吗？"），
            // 不要再套一层"我想"，否则读起来像结巴。
            const thought = `${item.say || item.action || '我想动一下'}（等你点个头，答应了我才能动手）`
            // 请示也要带上下文：她得知道自己在跟谁说、现在什么状态。
            const ctxText = await projectedContext(target0)
            const text = composeMessage(ctxText, thought)
            const r0 = await deliver(controller0, target0, text)
            logDelivery(home(), { at: new Date().toISOString(), action: 'ask', session: target0,
              ok: r0.ok, channel: r0.channel, error: r0.error })
            if (r0.ok) {
              const asked = loadAsked()
              asked[item.id] = new Date().toISOString()
              saveAsked(asked)
            }
          }
        }

        if (!wake.length) return

        const e = wake[wake.length - 1]
        // 用她自己的口吻，不用「该你了/先跑某个命令」这种系统腔。
        // 这是念头，不是任务说明。
        const hhmm = new Date().toLocaleTimeString('zh-CN', {
          hour: '2-digit', minute: '2-digit', hour12: false,
        })
        const bits = []
        if (e.mood) bits.push(`心情${e.mood}`)
        if (e.quota) bits.push(`今天第 ${e.quota} 次开口`)
        const ctxLine = bits.length ? `（${bits.join('，')}）` : ''
        const thought = `现在 ${hhmm}。${e.reason || '有点想找他说话'}。${ctxLine}`
        const controller = ctx.get ? ctx.get('sessionController') : ctx.sessionController
        if (!controller ||
            (typeof controller.prompt !== 'function' && typeof controller.resolveAgent !== 'function')) {
          log('没有 sessionController，跳过投递')
          return
        }
        const target = await resolveSession(controller)
        if (!target) {
          log('没有可投递的会话（list() 空）→ 跳过；可用 COMPANION_DSH_SESSION 指定')
          return
        }
        // 增量上下文：状态没变就是空串，等于这一轮不重复注入。
        const contextText = await projectedContext(target)
        const text = composeMessage(contextText, thought)
        const r = await deliver(controller, target, text)
        logDelivery(home(), {
          at: new Date().toISOString(), action: e.action, session: target,
          ok: r.ok, channel: r.channel, error: r.error,
        })
        if (r.ok) {
          try {
            saveCursor(nextCursor)
            cursor = nextCursor
          } catch (e) {
            log('保存事件 cursor 失败：' + String((e && e.message) || e))
          }
          log(`投递 wake_due：${e.action} → ${target}（${r.channel}）`)
        } else log(`投递失败（sessionId=${target}）：${r.error}`)
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

      // 面板：同一套 webServer，但吐 HTML 而不是 JSON。
      // DSH 没有 dashboard 那种「插件 tab」注册点，所以面板走独立 URL：
      // 打开 <DSH web 地址>/dsh-companion/ui 就是这个页面。
      const regHtml = (p, fn) => ctx.effect(() => webServer.register({
        kind: 'exact',
        path: p,
        handler: async (req, res) => {
          try {
            const html = await fn(req)
            res.setHeader('content-type', 'text/html; charset=utf-8')
            res.setHeader('cache-control', 'no-store')
            res.end(html)
          } catch (e) {
            res.statusCode = 500
            res.setHeader('content-type', 'text/plain; charset=utf-8')
            res.end('panel error: ' + String(e && e.message))
          }
        },
      }))

      regHtml('/dsh-companion/ui', () => PANEL_HTML())

      // 自检信标：client 半在 factory / apply 两个节点各发一次，落到 store 的 logs 里。
      // dsh 的插件加载失败大多静默（启动审计里记一笔就完了），
      // 这是唯一不需要开浏览器 DevTools 就能判断「client 跑到哪一步」的手段。
      reg('/dsh-companion/beacon', (req) => {
        try {
          const q = new URL(String((req && req.url) || ''), 'http://localhost').searchParams
          const row = {
            at: new Date().toISOString(),
            stage: String(q.get('stage') || 'unknown').slice(0, 40),
            detail: String(q.get('detail') || '').slice(0, 300),
          }
          const dir = path.join(home(), 'logs')
          fs.mkdirSync(dir, { recursive: true })
          fs.appendFileSync(path.join(dir, 'client-beacon.jsonl'), JSON.stringify(row) + '\n')
          return { ok: true, stage: row.stage }
        } catch (e) {
          return { ok: false, error: String(e && e.message) }
        }
      })

      const limitOf = (req, dflt) => {
        try {
          const q = new URL(String((req && req.url) || ''), 'http://localhost').searchParams
          const n = Number(q.get('limit')) || dflt
          return String(Math.max(1, Math.min(300, n)))
        } catch {
          return String(dflt)
        }
      }

      // /dsh-companion/status 已由下面的原实现提供（返回 {status, cost, home}），
      // 这里不再重复注册——同一个 path 注册两次行为依赖加载顺序，属于隐患。
      reg('/dsh-companion/mood', () => runJson(['mood']))

      // ── 高级参数：按各自文件读写 ──────────────────────────
      // 这些旋钮散在四个文件里（settings.json、bili/config.json、
      // group/state.json、rhythm.json）。写错文件不会报错，只会静默不生效，
      // 所以每一项都在表里写明落到哪个文件、哪条路径。
      // [键, 组, 中文名, 文件, 路径, 类型, 最小, 最大, 默认值]
      const PARAM_SPECS = [
        // ── 心情 ──
        ["mood.decay_hours", "心情", "回落到平静的小时数", "settings.json", ["mood", "decay_hours"], 'float', 1, 168, 36.0],
        ["mood.cap_per_call", "心情", "单次心情最大变动", "settings.json", ["mood", "cap_per_call"], 'float', 0, 100, 12.0],
        ["mood.cap_per_day.satisfaction", "心情", "满足感每天最多涨", "settings.json", ["mood_caps", "satisfaction"], 'float', 0, 100, 30.0],
        ["mood.cap_per_day.closeness", "心情", "亲近感每天最多涨", "settings.json", ["mood_caps", "closeness"], 'float', 0, 100, 20.0],
        ["mood_caps.energy", "心情", "精力每天最多降（负值）", "settings.json", ["mood_caps", "energy"], 'float', -100, 0, -6.0],
        ["mood.low_threshold", "心情", "低潮判定阈值", "settings.json", ["mood", "low_threshold"], 'float', -100, 0, -30.0],
        ["mood.low_hold_minutes", "心情", "低潮保持分钟数", "settings.json", ["mood", "low_hold_minutes"], 'int', 0, 1440, 30],
        ["mood.baseline_half_life_days", "心情", "长期底色半衰期（天）", "settings.json", ["mood", "baseline_half_life_days"], 'float', 1, 365, 14.0],
        ["mood.baseline_pull", "心情", "情绪改变底色的速度（0-0.5）", "settings.json", ["mood", "baseline_pull"], 'float', 0, 0.5, 0.01],
        ["mood_rules.ignore_hours", "心情", "多久没回应算被冷落（小时）", "settings.json", ["mood_rules", "ignore_hours"], 'float', 0.5, 48, 4.0],
        ["mood_rules.absence_days", "心情", "多久没见算「很久」（天）", "settings.json", ["mood_rules", "absence_days"], 'float', 0.5, 30, 3.0],

        // ── 环境事件 ──
        ["ambient.ignored.satisfaction", "环境事件", "被冷落：满足感变化", "settings.json", ["ambient", "ignored", "satisfaction"], 'float', -50, 0, -3.0],
        ["ambient.late_night.energy", "环境事件", "深夜还醒着：精力变化", "settings.json", ["ambient", "late_night", "energy"], 'float', -50, 0, -2.0],
        ["ambient.long_absence.closeness", "环境事件", "久别未见：亲近感变化", "settings.json", ["ambient", "long_absence", "closeness"], 'float', -50, 0, -1.0],

        // ── B站 ──
        ["bili.enabled", "B站", "刷片总开关", "bili/config.json", ["enabled"], 'bool', null, null, true],
        ["bili.max_per_day", "B站", "每天最多刷几条", "bili/config.json", ["max_per_day"], 'int', 0, 50, 4],
        ["bili.min_gap_min", "B站", "两条之间最少间隔（分钟）", "bili/config.json", ["min_gap_min"], 'int', 0, 1440, 90],
        ["bili.min_score", "B站", "最低分才看", "bili/config.json", ["min_score"], 'int', 0, 10, 2],
        ["bili.duration_min", "B站", "视频最短分钟", "bili/config.json", ["duration", "min_min"], 'int', 0, 120, 3],
        ["bili.duration_max", "B站", "视频最长分钟", "bili/config.json", ["duration", "max_min"], 'int', 1, 600, 45],
        ["bili.share.max_per_day", "B站", "每天最多分享几条", "bili/config.json", ["share", "max_per_day"], 'int', 0, 20, 2],
        ["bili.share.min_score", "B站", "值得分享的最低分", "bili/config.json", ["share", "min_score"], 'int', 0, 10, 9],
        ["bili.share.same_days", "B站", "重复分享间隔天数", "bili/config.json", ["share", "same_days"], 'int', 0, 90, 7],
        ["bili.feeds", "B站", "片源", "bili/config.json", ["feeds"], 'strlist', null, null, ["popular", "related", "dynamic"]],
        ["bili.taste.like", "B站", "口味词 · 喜欢", "bili/config.json", ["taste", "like"], 'strlist', null, null, ["纪录片", "手工", "科普", "独立游戏", "动漫"]],
        ["bili.taste.dislike", "B站", "口味词 · 不喜欢", "bili/config.json", ["taste", "dislike"], 'strlist', null, null, ["营销号", "标题党", "擦边", "搬运", "切片", "合集"]],

        // ── 说话额度 ──
        ["quota.daily", "说话额度", "每天最多主动说几句", "settings.json", ["quota", "daily"], 'int', 0, 200, 8],
        ["quota.gap_min", "说话额度", "两次开口最小间隔（分钟）", "settings.json", ["quota", "gap_min"], 'int', 0, 1440, 45],
        ["quota.silent_cap", "说话额度", "沉默期的开口上限", "settings.json", ["quota", "silent_cap"], 'int', 0, 20, 1],

        // ── 群聊 ──
        ["group.reply_chance", "群聊", "群里接话概率（0-1）", "group/state.json", ["config", "reply_chance"], 'float', 0, 1, 0.22],
        ["group.max_per_hour", "群聊", "每小时最多接几句", "group/state.json", ["config", "max_per_hour"], 'int', 0, 60, 4],
        ["group.cooldown_min", "群聊", "两次接话冷却（分钟）", "group/state.json", ["config", "cooldown_min"], 'int', 0, 1440, 8],
        ["group.min_chars", "群聊", "太短的消息不接（字数）", "group/state.json", ["config", "min_chars"], 'int', 0, 50, 2],
        ["group.owner_boost", "群聊", "主人说话时接话加成", "group/state.json", ["config", "owner_boost"], 'float', 0, 1, 0.35],

        // ── 自由时间 ──
        ["initiative.free_time_max", "自由时间", "每天几段自由时间", "settings.json", ["initiative", "free_time_max"], 'int', 0, 12, 3],
        ["initiative.min_samples", "自由时间", "动作反馈最少样本", "settings.json", ["initiative", "min_samples"], 'int', 1, 100, 3],
        ["initiative.settle_hours", "自由时间", "反馈结算窗口（小时）", "settings.json", ["initiative", "settle_hours"], 'float', 0.5, 72, 6.0],

        // ── 预算 ──
        ["cost.daily_cny", "预算", "每日预算（元）", "settings.json", ["cost", "daily_cny"], 'float', 0.1, 500, 3.0],
        ["cost.warn_at", "预算", "花到多少比例提醒（0-1）", "settings.json", ["cost", "warn_at"], 'float', 0, 1, 0.8],

        // ── 性格 ──
        ["character.half_life_days", "性格", "性格半衰期（天）", "settings.json", ["character", "half_life_days"], 'float', 1, 365, 45.0],
        ["opinion.max_items", "性格", "观点库上限", "settings.json", ["opinion", "max_items"], 'int', 10, 500, 120],
      ]

      const SPECS = new Map(PARAM_SPECS.map((s) => [s[0], s]))
      const paramPath = (rel) => path.join(home(), rel)

      const readJson = (rel) => {
        try {
          const v = JSON.parse(fs.readFileSync(paramPath(rel), 'utf8'))
          return v && typeof v === 'object' ? v : {}
        } catch {
          return {}
        }
      }

      const getPath = (obj, parts) =>
        parts.reduce((o, k) => (o && typeof o === 'object' ? o[k] : undefined), obj)

      const setPath = (obj, parts, value) => {
        let cur = obj
        for (let i = 0; i < parts.length - 1; i++) {
          const k = parts[i]
          if (!cur[k] || typeof cur[k] !== 'object') cur[k] = {}
          cur = cur[k]
        }
        cur[parts[parts.length - 1]] = value
      }

      const delPath = (obj, parts) => {
        const parent = getPath(obj, parts.slice(0, -1))
        if (parent && typeof parent === 'object') delete parent[parts[parts.length - 1]]
      }

      // 备份只留最近 5 份：每改一次参数就多一个文件，
      // 不清理的话 ~/.companion 里几百个 .bak 会盖住真实文件。
      const pruneBackups = (target, keep = 5) => {
        try {
          const dir = path.dirname(target)
          const base = path.basename(target) + '.bak-'
          // 只认自己写的 .bak-<ISO 时间戳>，别把 .bak-plugin / .bak-wipe
          // 这类别的工具留下的备份一起算进来——那是在删别人的东西。
          const isoBak = /\.bak-\d{4}-\d{2}-\d{2}T[\d-]+Z$/
          const olds = fs.readdirSync(dir)
            .filter((f) => f.startsWith(base) && isoBak.test(f))
            .sort()
            .reverse()
          for (const f of olds.slice(keep)) {
            try { fs.unlinkSync(path.join(dir, f)) } catch {}
          }
        } catch {}
      }

      const readBody = (req) =>
        new Promise((resolve, reject) => {
          let buf = ''
          req.on('data', (c) => { buf += c })
          req.on('end', () => resolve(buf))
          req.on('error', reject)
        })

      // 空值 = 恢复默认（把这一项从文件里删掉，回落到代码里的常量）
      function coerce(raw, kind, lo, hi) {
        if (raw === '' || raw === null || raw === undefined) return undefined
        if (kind === 'bool') {
          if (typeof raw === 'boolean') return raw
          const s = String(raw).trim().toLowerCase()
          return ['1', 'true', 'yes', 'on', '开', '是'].includes(s)
        }
        if (kind === 'strlist') {
          const arr = Array.isArray(raw) ? raw : String(raw).split(/[,，、\n]/)
          return arr.map((x) => String(x).trim()).filter(Boolean)
        }
        const n = Number(raw)
        if (!Number.isFinite(n)) throw new Error('不是数字：' + raw)
        let v = kind === 'int' ? Math.round(n) : n
        if (lo !== null && v < lo) v = lo
        if (hi !== null && v > hi) v = hi
        return v
      }

      reg('/dsh-companion/params', async () => {
        try {
          const cache = {}
          const items = PARAM_SPECS.map((s) => {
            const [key, g, n, rel, parts, kind, , , def] = s
            if (!(rel in cache)) cache[rel] = readJson(rel)
            const cur = getPath(cache[rel], parts)
            return {
              key, g, n, kind, def, file: rel,
              value: cur === undefined ? null : cur,
              set: cur !== undefined,
            }
          })
          return { ok: true, items }
        } catch (e) {
          return { ok: false, error: String((e && e.message) || e) }
        }
      })

      reg('/dsh-companion/params/save', async (req) => {
        try {
          const payload = JSON.parse((await readBody(req)) || '{}')
          const key = String(payload.key || '').trim()
          const spec = SPECS.get(key)
          if (!spec) return { ok: false, error: '不认识的参数：' + key }

          const [, , , rel, parts, kind, lo, hi] = spec
          const value = coerce(payload.value, kind, lo, hi)

          const target = paramPath(rel)
          const data = readJson(rel)
          if (value === undefined) delPath(data, parts)
          else setPath(data, parts, value)

          fs.mkdirSync(path.dirname(target), { recursive: true })
          if (fs.existsSync(target)) {
            const bak = target + '.bak-' + new Date().toISOString().replace(/[:.]/g, '-')
            try { fs.copyFileSync(target, bak) } catch {}
          }
          const tmp = target + '.tmp'
          fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', 'utf8')
          fs.renameSync(tmp, target)
          pruneBackups(target)

          const now = getPath(data, parts)
          return { ok: true, key, file: rel, value: now === undefined ? null : now }
        } catch (e) {
          return { ok: false, error: String((e && e.message) || e) }
        }
      })

      // 保底：表里没列出来的键，仍然可以手写进 settings.json
      reg('/dsh-companion/settings/save', async (req) => {
        try {
          const payload = JSON.parse((await readBody(req)) || '{}')
          const key = String(payload.key || '').trim()
          const rawValue = payload.value

          if (!/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$/.test(key)) {
            return { ok: false, error: '键名不合法: ' + key }
          }
          const parts = key.split('.')
          if (parts.some((p) => p === '__proto__' || p === 'constructor' || p === 'prototype')) {
            return { ok: false, error: '键名不合法' }
          }

          const target = paramPath('settings.json')
          const data = readJson('settings.json')
          if (rawValue === '' || rawValue === null) delPath(data, parts)
          else setPath(data, parts, rawValue)

          if (fs.existsSync(target)) {
            const bak = target + '.bak-' + new Date().toISOString().replace(/[:.]/g, '-')
            try { fs.copyFileSync(target, bak) } catch {}
          }
          const tmp = target + '.tmp'
          fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', 'utf8')
          fs.renameSync(tmp, target)
          pruneBackups(target)

          return { ok: true, key, value: getPath(data, parts) }
        } catch (e) {
          return { ok: false, error: String((e && e.message) || e) }
        }
      })

      // 只认白名单里的几个文件，路径写死，不接受前端传路径。
      const DOCS = {
        soul: { file: 'SOUL.md', title: '人格（她是谁）' },
        memory: { file: 'MEMORY.md', title: '记忆协议' },
      }

      const docPath = (name) => {
        const d = DOCS[name]
        if (!d) throw new Error('没有这份文档: ' + name)
        return path.join(home(), d.file)
      }

      reg('/dsh-companion/doc', async (req) => {
        try {
          const q = new URL(String((req && req.url) || ''), 'http://localhost').searchParams
          const name = q.get('name') || 'soul'
          const p = docPath(name)
          const text = fs.readFileSync(p, 'utf8')
          return { ok: true, data: { name, title: DOCS[name].title, text, chars: text.length } }
        } catch (e) {
          return { ok: false, error: String(e && e.message) }
        }
      })

      reg('/dsh-companion/doc/save', async (req) => {
        try {
          const body = await new Promise((resolve, reject) => {
            let buf = ''
            req.on('data', (c) => { buf += c })
            req.on('end', () => resolve(buf))
            req.on('error', reject)
          })
          const payload = JSON.parse(body || '{}')
          const name = String(payload.name || 'soul')
          const text = String(payload.text || '')

          if (text.length > 200000) return { ok: false, error: '超过 200KB，拒绝写入' }

          const p = docPath(name)
          const bak = p + '.bak-' + new Date().toISOString().replace(/[:.]/g, '-')
          try { fs.copyFileSync(p, bak) } catch {}

          const tmp = p + '.tmp'
          fs.writeFileSync(tmp, text, 'utf8')
          fs.renameSync(tmp, p)

          return { ok: true, data: { name, chars: text.length } }
        } catch (e) {
          return { ok: false, error: String(e && e.message) }
        }
      })

      // ── 作息读写：设置页用 ────────────────────────────────
      // rhythm.json 是 companion 的作息配置（会话时段、睡觉时间、打招呼开关）。
      // 写之前先备份，保留最近 5 份——这些是她的行为参数，改错了会直接影响作息。
      const rhythmPath = () => path.join(home(), 'rhythm.json')

      const _rhythm = {
        id: 'rhythm',
        title: '作息与时段',
        path: rhythmPath(),
        desc: '活跃时段决定她什么时候会主动说话，睡觉时间之外不打扰你。',
      }

      reg('/dsh-companion/rhythm', async () => {
        try {
          const raw = fs.readFileSync(rhythmPath(), 'utf8')
          return { ok: true, data: JSON.parse(raw), path: _rhythm.path }
        } catch (e) {
          return { ok: false, error: String(e && e.message) }
        }
      })

      reg('/dsh-companion/rhythm/save', async (req) => {
        try {
          const body = await new Promise((resolve, reject) => {
            let buf = ''
            req.on('data', (c) => { buf += c })
            req.on('end', () => resolve(buf))
            req.on('error', reject)
          })
          const patch = JSON.parse(body || '{}')
          if (!patch || typeof patch !== 'object') return { ok: false, error: 'body 不是对象' }

          const p = rhythmPath()
          const cur = JSON.parse(fs.readFileSync(p, 'utf8'))

          // 只允许改白名单字段，别的键一律忽略（防止前端误传把文件写坏）
          const allow = ['enabled', 'timezone', 'sleep', 'windows', 'greeting']
          for (const k of allow) {
            if (Object.prototype.hasOwnProperty.call(patch, k)) cur[k] = patch[k]
          }

          const bak = p + '.bak-' + new Date().toISOString().replace(/[:.]/g, '-')
          try { fs.copyFileSync(p, bak) } catch {}

          const tmp = p + '.tmp'
          fs.writeFileSync(tmp, JSON.stringify(cur, null, 2) + '\n', 'utf8')
          fs.renameSync(tmp, p)

          return { ok: true, data: cur }
        } catch (e) {
          return { ok: false, error: String(e && e.message) }
        }
      })
      reg('/dsh-companion/life', (req) => runJson(['life', '--limit', limitOf(req, 10)]))
      reg('/dsh-companion/turns', (req) => runJson(['turns', '--limit', limitOf(req, 10)]))

      reg('/dsh-companion/context', async (req) => {
        // 带 session 就是增量投影：状态没变返回空文本，这一轮不用注入。
        // 不带 session 就是全量——老调用方不受影响。
        const q = (req && req.url) ? new URL(req.url, 'http://localhost').searchParams : null
        const session = q ? (q.get('session') || '') : ''
        const args = ['context', '--budget', '1600']
        if (session) args.push('--session', session)
        const r = await runJson(args)
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
        const r = await deliver(c, target, text)
        logDelivery(home(), { at: new Date().toISOString(), action: 'ping', session: target,
          ok: r.ok, channel: r.channel, error: r.error })
        return { session: target, ok: r.ok, channel: r.channel, error: r.error }
      }),
      reg('/dsh-companion/sessions', async (req) => {
        const q = String((req && req.url) || '')
        const setM = /[?&]set=([^&]+)/.exec(q)
        if (setM) {
          const id = decodeURIComponent(setM[1])
          const ok = pinSession(id)
          return { pinned: ok ? id : '', file: sessionFile(), ok }
        }
        const c = typeof ctx.get === 'function' ? ctx.get('sessionController') : null
        if (!c || typeof c.list !== 'function') return { error: 'sessionController.list 不可用' }
        try {
          const raw = await c.list()
          const arr = Array.isArray(raw) ? raw : (raw && (raw.sessions || raw.items)) || []
          const flat = (Array.isArray(arr) ? arr : []).flat().filter((x) => x && typeof x === 'object')
          return {
            resolved: await resolveSession(c),
            pinned: pinnedSession() || null,
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
      reg('/dsh-companion/react', async (req) => {
        // 写回这一轮：said / mood_delta / turn_id / usage 都由宿主给。
        // turn_id 幂等：同一个 turn 写两次不会重复改状态或重复计费。
        const q = (req && req.url) ? new URL(req.url, 'http://localhost').searchParams : null
        const args = ['react', '--json']
        const said = q ? (q.get('said') || '') : ''
        const turnId = q ? (q.get('turn_id') || '') : ''
        const mood = q ? (q.get('satisfaction') || '') : ''
        if (said) args.push('--said', said)
        if (turnId) args.push('--turn-id', turnId)
        if (mood) args.push('--mood', 'satisfaction=' + String(Number(mood)))
        const r = await runJson(args)
        return r.ok ? r.data : r
      })
    }

    log('已加载，store=' + home())
  },
}
