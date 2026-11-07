// DSH 面板的 HTML。单文件、无外部依赖、不引入构建步骤。
//
// 结构：顶部页签（状态 / 设置 / 人格 / 日记），各页独立加载、各自限高。
// 状态页带两张图：心情趋势（三条累积折线）和活动分布（环形）。
// 卡片和列表项可点开详情，弹窗里看完整内容。
//
// 数据全部来自 host 半的路由（/dsh-companion/*），不直接读文件。

export function PANEL_HTML() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉丝</title>
<style>
  :root{
    --bg:#080c17; --bg2:#0d1424;
    --surf:rgba(20,27,48,.72); --surf2:rgba(26,34,58,.9);
    --line:rgba(120,140,180,.18); --line2:rgba(120,140,180,.3);
    --dim:#7b8bab; --dim2:#5b6987;
    --main:#5eead4; --violet:#a78bfa; --warn:#fbbf24; --bad:#fb7185; --sky:#60a5fa;
    --text:#e6edf7;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;color:var(--text);
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(94,234,212,.10), transparent 60%),
      radial-gradient(900px 500px at 90% 0%, rgba(167,139,250,.10), transparent 55%),
      linear-gradient(180deg,var(--bg),var(--bg2));
    background-attachment:fixed;
    font:14px/1.6 "Segoe UI","Microsoft YaHei UI","Microsoft YaHei","PingFang SC",-apple-system,sans-serif;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
  .app{max-width:1240px;margin:0 auto;padding:14px 18px 30px}

  /* 页签 */
  .tabs-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;
    margin-bottom:16px;flex-wrap:wrap}
  .tabs{display:inline-flex;gap:4px;padding:4px;
    background:rgba(148,163,184,.07);border:1px solid var(--line);border-radius:12px}
  .tabs button{background:transparent;border:0;color:var(--dim);border-radius:9px;
    padding:6px 16px;font-size:13px;cursor:pointer;transition:.18s}
  .tabs button:hover{color:var(--text)}
  .tabs button.on{background:linear-gradient(135deg,rgba(94,234,212,.18),rgba(167,139,250,.18));
    color:var(--main);box-shadow:0 0 0 1px rgba(94,234,212,.25) inset}

  .page{display:none;animation:fade .22s ease}
  .page.on{display:block}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-bottom:12px}
  .card{position:relative;background:var(--surf);border:1px solid var(--line);
    border-radius:16px;padding:15px;backdrop-filter:blur(10px);
    transition:border-color .18s, transform .18s, box-shadow .18s}
  .card.clickable{cursor:pointer}
  .card.clickable:hover{border-color:var(--line2);transform:translateY(-1px);
    box-shadow:0 8px 24px -12px rgba(0,0,0,.6)}
  .t{font-size:11px;color:var(--dim);margin-bottom:9px;letter-spacing:.06em;text-transform:uppercase}
  .big{font-size:23px;font-weight:650;line-height:1.2;letter-spacing:-.01em}
  .meta{font-size:11px;color:var(--dim);margin-top:4px}
  .row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .num{font-variant-numeric:tabular-nums;font-size:12px;color:var(--dim)}

  .axis{margin-bottom:11px}
  .axis:last-child{margin-bottom:0}
  .bar{height:7px;border-radius:99px;background:rgba(148,163,184,.14);overflow:hidden;margin-top:6px}
  .bar i{display:block;height:100%;border-radius:99px;transition:width .5s cubic-bezier(.4,0,.2,1)}
  .axis-label{display:flex;justify-content:space-between;align-items:baseline}
  .axis-label span:first-child{font-size:12px}

  .two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  @media (max-width:900px){.two{grid-template-columns:1fr}}
  .list{max-height:340px;overflow:auto;padding-right:5px}
  .list::-webkit-scrollbar{width:6px}
  .list::-webkit-scrollbar-thumb{background:rgba(148,163,184,.28);border-radius:99px}
  ul{list-style:none;margin:0;padding:0}
  li{padding:8px 6px;border-radius:10px;font-size:13px;transition:background .15s;cursor:pointer}
  li:hover{background:rgba(148,163,184,.07)}
  .said{white-space:pre-wrap;word-break:break-word}
  .tag{font-size:10px;padding:1px 7px;border-radius:6px;
    border:1px solid var(--line);color:var(--dim);background:rgba(148,163,184,.06)}

  .form{display:flex;flex-wrap:wrap;gap:14px 18px;align-items:flex-end}
  .field{display:flex;flex-direction:column;gap:4px}
  .field span{font-size:11px;color:var(--dim)}
  .field input[type=text],.field input[type=number]{
    background:rgba(148,163,184,.07);border:1px solid var(--line);border-radius:9px;
    padding:6px 10px;width:100px;color:inherit;font-size:13px;outline:none;transition:.15s}
  .field input:focus{border-color:var(--main);box-shadow:0 0 0 3px rgba(94,234,212,.10)}
  .field input[type=checkbox]{width:17px;height:17px;accent-color:var(--main)}

  textarea{width:100%;min-height:52vh;resize:vertical;
    background:rgba(148,163,184,.05);border:1px solid var(--line);border-radius:12px;
    padding:13px;color:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:12.5px;line-height:1.75;outline:none;transition:.15s}
  textarea:focus{border-color:var(--main);box-shadow:0 0 0 3px rgba(94,234,212,.10)}

  button.act{background:rgba(148,163,184,.06);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:5px 13px;font-size:12px;cursor:pointer;transition:.15s}
  button.act:hover{border-color:var(--line2);background:rgba(148,163,184,.12)}
  button.primary{background:linear-gradient(135deg,var(--main),var(--violet));
    color:#06121a;border-color:transparent;font-weight:650}
  button.primary:hover{filter:brightness(1.08)}

  select{background:rgba(148,163,184,.07);border:1px solid var(--line);border-radius:9px;
    padding:5px 9px;color:inherit;font-size:12px;outline:none}

  .hint{font-size:11px;color:var(--dim);margin-top:9px;line-height:1.65}
  .hint code{background:rgba(148,163,184,.1);padding:1px 5px;border-radius:4px;font-size:11px}
  .ok{color:var(--main)} .bad{color:var(--bad)} .empty{color:var(--dim);font-size:12px;padding:6px}
  .foot{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--dim2)}

  /* ── 图标与卡片标题 ─────────────────────────────────── */
  .kv{display:flex;align-items:center;gap:10px;margin-top:2px}
  .kv .ico{font-size:26px;line-height:1;flex-shrink:0;
    filter:saturate(.92);opacity:.95}
  .t .ti{display:inline-block;margin-right:7px;font-size:13px;line-height:1;
    vertical-align:1px;opacity:.9}
  .t-sub{float:right;font-size:10.5px;color:var(--dim2);font-weight:400;letter-spacing:.2px}

  /* ── 图例：名字 + 条数 + 百分比 ─────────────────────── */
  .legend b{color:var(--text);font-weight:650;margin-left:5px;font-variant-numeric:tabular-nums}
  .legend em{font-style:normal;color:var(--dim2);margin-left:4px;font-size:10px}
  .legend-col{display:grid;grid-template-columns:1fr 1fr;gap:5px 14px;margin-top:8px}
  .legend-col span{display:flex;align-items:center;font-size:11.5px;min-width:0}
  .legend-col span i{width:7px;height:7px;border-radius:2px;margin-right:6px;flex-shrink:0}
  .legend-col span b{margin-left:auto;font-size:11.5px}
  .legend-col span em{min-width:32px;text-align:right}

  /* ── 活跃时段热力 ───────────────────────────────────── */
  .hgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:4px;margin-top:6px}
  .hcell{aspect-ratio:1.65/1;border-radius:5px;position:relative;
    transition:transform .12s, box-shadow .12s}
  .hcell:hover{transform:translateY(-1px);box-shadow:0 3px 10px rgba(0,0,0,.35)}
  .hcell span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font-size:10px;color:rgba(230,244,255,.88);font-weight:600;
    letter-spacing:.2px}
  .haxis{display:grid;grid-template-columns:repeat(4,1fr);margin-top:6px;
    font-size:9.5px;color:var(--dim2);text-align:center}
  .hnote{margin-top:8px;font-size:11px;color:var(--dim)}
  .hnote b{color:var(--main);font-weight:600}

  /* ── 影响来源横条 ───────────────────────────────────── */
  .clist{display:flex;flex-direction:column;gap:7px;margin-top:6px}
  .crow{display:grid;grid-template-columns:minmax(0,1fr) 96px 46px 34px;
    align-items:center;gap:9px;font-size:11.5px}
  .cname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
  .cbar{height:6px;border-radius:3px;background:rgba(148,163,184,.1);overflow:hidden}
  .cbar i{display:block;height:100%;border-radius:3px;min-width:3px;
    transition:width .3s ease}
  .cval{text-align:right;font-variant-numeric:tabular-nums;font-weight:650;font-size:11.5px}
  .cn{text-align:right;color:var(--dim2);font-size:10px}

  /* ── 高级参数表 ─────────────────────────────────────── */
  .pgroup{margin-bottom:7px;border:1px solid var(--line);border-radius:11px;
    background:rgba(148,163,184,.022);overflow:hidden}
  .pgroup > summary{display:flex;align-items:center;justify-content:space-between;
    gap:10px;padding:8px 12px;cursor:pointer;font-size:12.5px;list-style:none;
    color:var(--text);user-select:none;transition:.15s}
  .pgroup > summary::-webkit-details-marker{display:none}
  .pgroup > summary::before{content:"›";display:inline-block;margin-right:7px;
    color:var(--dim2);transition:transform .16s;font-size:14px;line-height:1}
  .pgroup[open] > summary::before{transform:rotate(90deg)}
  .pgroup > summary:hover{background:rgba(148,163,184,.05)}
  .pgroup > summary > span:first-of-type{flex:1}
  .pgroup[open] > summary{border-bottom:1px solid var(--line)}
  .pgroup-body{padding:2px 6px 7px}

  .prow{display:grid;grid-template-columns:minmax(0,1fr) 58px auto;
    align-items:center;gap:12px;padding:5px 7px;border-radius:7px}
  .prow:hover{background:rgba(148,163,184,.05)}
  .prow + .prow{box-shadow:inset 0 1px 0 rgba(148,163,184,.06)}
  .pinfo{min-width:0}
  .plabel{font-size:12.5px;line-height:1.32;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pkey{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:9.5px;
    line-height:1.3;color:var(--dim2);margin-top:1px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .prow.dirty .plabel::after{content:"";display:inline-block;width:5px;height:5px;
    border-radius:50%;background:var(--main);margin-left:6px;vertical-align:2px;opacity:.85}
  .pdef{font-size:11px;color:var(--dim2);text-align:right;white-space:nowrap;
    font-variant-numeric:tabular-nums}
  .pctl{display:flex;align-items:stretch;flex-shrink:0}
  .pctl input{width:74px;background:rgba(148,163,184,.07);border:1px solid var(--line);
    border-right:0;border-radius:7px 0 0 7px;padding:3px 8px;color:inherit;
    font-size:12px;outline:none;font-variant-numeric:tabular-nums;transition:.15s}
  .pctl input::placeholder{color:var(--dim2);opacity:.75}
  .pctl input:focus{border-color:var(--main);background:rgba(94,234,212,.06);
    box-shadow:0 0 0 3px rgba(94,234,212,.09)}
  .pbtn{background:rgba(148,163,184,.07);border:1px solid var(--line);
    border-radius:0 7px 7px 0;color:var(--dim);font-size:11.5px;padding:0 10px;
    cursor:pointer;transition:.15s}
  .pbtn:hover{color:var(--main);border-color:var(--main);background:rgba(94,234,212,.09)}

  /* 图表容器 */
  .chart{width:100%;height:170px;display:block}
  .legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px;color:var(--dim)}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;vertical-align:middle}

  /* 弹窗 */
  .modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;
    padding:24px;background:rgba(4,8,16,.66);backdrop-filter:blur(6px);animation:fade .16s ease}
  .modal[hidden]{display:none}
  .modal-box{width:min(680px,100%);max-height:80vh;display:flex;flex-direction:column;
    background:linear-gradient(180deg,rgba(26,34,58,.98),rgba(16,22,38,.98));
    border:1px solid var(--line2);border-radius:18px;overflow:hidden;
    box-shadow:0 24px 70px -20px rgba(0,0,0,.8)}
  .modal-head{display:flex;justify-content:space-between;align-items:center;
    padding:14px 18px;border-bottom:1px solid var(--line);font-size:13.5px;font-weight:600}
  .modal-body{padding:16px 18px 20px;overflow:auto;font-size:13px;line-height:1.75;white-space:pre-wrap}
  .modal-body::-webkit-scrollbar{width:6px}
  .modal-body::-webkit-scrollbar-thumb{background:rgba(148,163,184,.28);border-radius:99px}
  #modal-close{background:transparent;border:0;color:var(--dim);font-size:20px;cursor:pointer;
    line-height:1;padding:0 4px}
  #modal-close:hover{color:var(--text)}
</style>
</head>
<body>
<div class="app">

  <div class="tabs-bar">
    <div class="tabs">
      <button data-tab="status" class="on">状态</button>
      <button data-tab="settings">设置</button>
      <button data-tab="persona">人格</button>
      <button data-tab="diary">日记</button>
    </div>
    <div class="foot">
      <span id="stamp"></span>
      <button class="act" id="btn">刷新</button>
    </div>
  </div>

  <!-- 状态 -->
  <div class="page on" id="page-status">
    <div class="grid">
      <div class="card clickable" id="card-mood">
        <div class="t">心情</div>
        <div class="kv">
          <span class="ico" id="moodIcon">·</span>
          <span class="big" id="moodLabel">—</span>
        </div>
        <div class="meta" id="moodAt"></div>
      </div>
      <div class="card">
        <div class="t">今日额度</div>
        <div class="kv">
          <span class="ico">💬</span>
          <span class="big"><span id="quota">—</span><span id="quotaCap"> / —</span></span>
        </div>
        <div class="meta" id="quotaMeta"></div>
      </div>
      <div class="card">
        <div class="t">当前时段</div>
        <div class="kv">
          <span class="ico" id="windowIcon">🕐</span>
          <span class="big" id="window">—</span>
        </div>
        <div class="meta" id="windowMeta"></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:12px">
      <div class="t"><span class="ti">📈</span>心情趋势<span class="t-sub" id="moodRange"></span></div>
      <svg class="chart" id="moodChart"></svg>
      <div class="legend">
        <span><i style="background:#5eead4"></i>满足感 <b id="lg-sat">—</b></span>
        <span><i style="background:#a78bfa"></i>亲近感 <b id="lg-clo">—</b></span>
        <span><i style="background:#fbbf24"></i>精力 <b id="lg-ene">—</b></span>
      </div>
    </div>

    <div class="two" style="margin-bottom:12px">
      <div class="card">
        <div class="t"><span class="ti">🎬</span>活动分布<span class="t-sub" id="kindSub"></span></div>
        <svg class="chart" id="kindChart" style="height:156px"></svg>
        <div class="legend legend-col" id="kindLegend"></div>
      </div>
      <div class="card">
        <div class="t"><span class="ti">🎫</span>今日额度使用</div>
        <svg class="chart" id="quotaChart" style="height:156px"></svg>
        <div class="legend legend-col" id="quotaLegend"></div>
      </div>
    </div>

    <div class="two" style="margin-bottom:12px">
      <div class="card">
        <div class="t"><span class="ti">🕐</span>活跃时段<span class="t-sub">按小时聚合</span></div>
        <div id="hourHeat"><div class="empty">读取中…</div></div>
      </div>
      <div class="card">
        <div class="t"><span class="ti">💡</span>什么在影响她<span class="t-sub">按原因合并</span></div>
        <div id="causes"><div class="empty">读取中…</div></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:12px">
      <div class="t"><span class="ti">📊</span>心情三轴</div>
      <div id="axes"></div>
    </div>

    <div class="two">
      <div class="card">
        <div class="t">最近在做什么</div>
        <div class="list" id="life"><div class="empty">读取中…</div></div>
      </div>
      <div class="card">
        <div class="t">最近说过的话</div>
        <div class="list" id="said"><div class="empty">读取中…</div></div>
      </div>
    </div>
  </div>

  <!-- 设置 -->
  <div class="page" id="page-settings">
    <div class="card">
      <div class="t">作息与时段</div>
      <div class="form">
        <label class="field"><span>总开关</span><input type="checkbox" id="s-enabled"></label>
        <label class="field"><span>睡觉</span><input type="text" id="s-sleep-at" placeholder="01:00"></label>
        <label class="field"><span>起床</span><input type="text" id="s-wake" placeholder="08:30"></label>
        <label class="field"><span>活跃开始</span><input type="text" id="s-win-start" placeholder="08:30"></label>
        <label class="field"><span>活跃结束</span><input type="text" id="s-win-end" placeholder="24:00"></label>
        <label class="field"><span>每天叫醒</span><input type="number" id="s-wakes" min="0" max="30" step="1"></label>
        <button class="act primary" id="s-save">保存</button>
      </div>
      <div class="hint" id="s-msg">时段外她不会主动找你；睡觉时间只影响主动行为，不影响你找她。</div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="t">高级参数</div>
      <div class="hint" style="margin:0 0 10px">
        灰字是代码里的默认值。输入框留空保存 = 恢复默认。改完下一个 tick 生效，不用重启。
      </div>
      <div id="adv-list" style="margin-bottom:10px"></div>
      <details class="pgroup" style="margin-top:4px">
        <summary><span>自定义参数（表里没有的键）</span><span class="num">手写</span></summary>
        <div class="pgroup-body" style="padding:10px 8px">
          <div class="form">
            <label class="field"><span>键名</span><input type="text" id="adv-key" placeholder="cost.output_tokens"></label>
            <label class="field"><span>值</span><input type="text" id="adv-val" placeholder="600"></label>
            <button class="act primary" id="adv-save">写入</button>
          </div>
          <div class="hint">只写进 settings.json。表里没列的键不一定有代码在读，写之前先确认。</div>
        </div>
      </details>
      <div class="hint" id="adv-msg"></div>
    </div>
  </div>

  <!-- 人格 -->
  <div class="page" id="page-persona">
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <div>
          <div class="t" style="margin:0" id="p-title">人格</div>
          <div class="meta" id="p-meta">读取中…</div>
        </div>
        <div class="row" style="gap:8px">
          <select id="p-name">
            <option value="soul">SOUL.md · 人格</option>
            <option value="memory">MEMORY.md · 记忆协议</option>
          </select>
          <button class="act" id="p-reload">重读</button>
          <button class="act primary" id="p-save">保存</button>
        </div>
      </div>
      <textarea id="p-text" spellcheck="false" placeholder="读取中…"></textarea>
      <div class="hint" id="p-msg">保存时自动备份原文件。改完下一轮对话生效。</div>
    </div>
  </div>

  <!-- 日记 -->
  <div class="page" id="page-diary">
    <div class="card">
      <div class="t">生活流水（最近 30 条，点开看详情）</div>
      <div class="list" style="max-height:calc(100vh - 210px)" id="diary">
        <div class="empty">读取中…</div>
      </div>
    </div>
  </div>

</div>

<div class="modal" id="modal" hidden>
  <div class="modal-box">
    <div class="modal-head">
      <span id="modal-title">详情</span>
      <button id="modal-close">×</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
(function(){
  var AXES = [
    {k:"satisfaction", n:"满足感", c:"#5eead4"},
    {k:"closeness",   n:"亲近感", c:"#a78bfa"},
    {k:"energy",      n:"精力",   c:"#fbbf24"}
  ];
  var loaded = {status:false, settings:false, persona:false, diary:false};
  var state = {moodHistory: [], lifeRows: [], moodNow: null};



  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }
  // QQ 的 CQ 码（[CQ:image,file=xxx]）是协议层的东西，给人看的界面里
  // 只该出现「图片」「表情」这类词，否则一行里全是路径。
  function cleanCQ(s){
    // 顺序很重要：日志里的 CQ 码是 HTML 实体转义过的（&#91;CQ:image...&#93;），
    // 先把实体还原成方括号，才可能匹配到 [CQ:...]。
    // 又因为整段代码放在模板字符串里，正则的反斜杠必须写成两个，
    // 否则求值时会被当成转义序列吃掉，[CQ:image 就变成字符类了。
    return String(s == null ? "" : s)
      .replace(/&#91;/g, "[")
      .replace(/&#93;/g, "]")
      .replace(/&amp;/g, "&")
      .replace(/\\[CQ:image[^\\]]*\\]/g, "[图片]")
      .replace(/\\[CQ:face[^\\]]*\\]/g, "[表情]")
      .replace(/\\[CQ:record[^\\]]*\\]/g, "[语音]")
      .replace(/\\[CQ:video[^\\]]*\\]/g, "[视频]")
      .replace(/\\[CQ:file[^\\]]*\\]/g, "[文件]")
      .replace(/\\[CQ:[^\\]]*\\]/g, "[消息]");
  }

  function get(url){
    return fetch(url, {credentials:"same-origin", cache:"no-store"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  function unwrap(r){
    if(r && r.ok === false) throw new Error(r.error || "调用失败");
    return (r && Object.prototype.hasOwnProperty.call(r, "data")) ? r.data : r;
  }
  function bar(v, color){
    var pct = Math.max(0, Math.min(100, (Number(v) + 100) / 2));
    return '<div class="bar"><i style="width:' + pct + '%;background:' + color + '"></i></div>';
  }

  // ── 弹窗 ──
  function showModal(title, body){
    document.getElementById("modal-title").textContent = title;
    document.getElementById("modal-body").textContent = body;
    document.getElementById("modal").hidden = false;
  }
  function hideModal(){ document.getElementById("modal").hidden = true; }
  document.getElementById("modal-close").addEventListener("click", hideModal);
  document.getElementById("modal").addEventListener("click", function(e){
    if(e.target.id === "modal") hideModal();
  });
  document.addEventListener("keydown", function(e){
    if(e.key === "Escape") hideModal();
  });

  // ── 环形图：活动分布 / 额度使用 ──
  var KIND_COLORS = {
    bili: '#5eead4', watch: '#a78bfa', other: '#60a5fa',
    chat: '#fbbf24', think: '#f472b6', rest: '#94a3b8'
  };
  var KIND_NAMES = {
    bili: '刷 B 站', watch: '看片', chat: '聊天', think: '思考',
    rest: '休息', other: '其他'
  };

  function donut(svgId, legendId, items, centerText, centerSub){
    var svg = document.getElementById(svgId);
    var legend = document.getElementById(legendId);
    var total = items.reduce(function(s, i){ return s + i.value; }, 0);

    if(!total){
      svg.setAttribute("viewBox", "0 0 140 140");
      svg.innerHTML = '<circle cx="70" cy="70" r="50" fill="none" stroke="rgba(120,140,180,.2)" stroke-width="16"/>';
      centerText = centerText || "无数据";
      svg.innerHTML += '<text x="70" y="76" fill="#7b8bab" font-size="13" text-anchor="middle">' + esc(centerText) + '</text>';
      legend.innerHTML = "";
      return;
    }

    var R = 52, C = 2 * Math.PI * R, offset = 0, parts = [];
    items.forEach(function(it){
      var frac = it.value / total;
      var dash = frac * C;
      parts.push('<circle cx="70" cy="70" r="' + R + '" fill="none" stroke="' + it.color +
        '" stroke-width="16" stroke-dasharray="' + dash.toFixed(2) + ' ' + (C - dash).toFixed(2) +
        '" stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 70 70)"/>');
      offset += dash;
    });

    svg.setAttribute("viewBox", "0 0 140 140");
    svg.innerHTML = parts.join("") +
      (centerText ? '<text x="70" y="' + (centerSub ? 72 : 76) + '" fill="#e6edf7" font-size="19" font-weight="650" text-anchor="middle">' +
       esc(centerText) + '</text>' : "") +
      (centerSub ? '<text x="70" y="86" fill="#7b8bab" font-size="9.5" text-anchor="middle">' +
       esc(centerSub) + '</text>' : "");

    // 图例带条数，只有百分比看不出量级
    legend.innerHTML = items.map(function(it){
      var pct = Math.round(it.value / total * 100);
      return '<span><i style="background:' + it.color + '"></i>' + esc(it.label) +
        '<b>' + it.value + '</b><em>' + pct + '%</em></span>';
    }).join("");
  }

  function renderKindChart(rows){
    var counts = {};
    (rows || []).forEach(function(r){
      var k = String(r.kind || "other");
      counts[k] = (counts[k] || 0) + 1;
    });
    var items = Object.keys(counts).map(function(k){
      return { label: KIND_NAMES[k] || k, value: counts[k], color: KIND_COLORS[k] || "#64748b" };
    }).sort(function(a, b){ return b.value - a.value; });
    var top = items.slice(0, 5);
    var rest = items.slice(5);
    if(rest.length){
      top.push({
        label: "其他 " + rest.length + " 类",
        value: rest.reduce(function(s, i){ return s + i.value; }, 0),
        color: "#475569"
      });
    }
    var total = (rows || []).length;
    var sub = document.getElementById("kindSub");
    if(sub) sub.textContent = total ? total + " 条流水" : "";
    donut("kindChart", "kindLegend", top, String(total), "条");
  }

  function renderQuotaChart(count, cap){
    var used = Number(count) || 0;
    var limit = Number(cap) || 0;
    if(!limit){
      donut("quotaChart", "quotaLegend", [], String(used));
      document.getElementById("quotaLegend").innerHTML = '<span>没设每天上限</span>';
      return;
    }
    var rest = Math.max(0, limit - used);
    donut("quotaChart", "quotaLegend", [
      { label: "已开口", value: used, color: "#5eead4" },
      { label: "还能说", value: rest, color: "rgba(120,140,180,.25)" },
    ], used + "/" + limit, "次");
  }

  // ── 折线图：把 history 的 delta 累加成绝对值 ──
  // ── 活跃时段：24 格热力，看她在哪个钟点最忙 ──
  function renderHourHeat(rows){
    var el = document.getElementById("hourHeat");
    var bins = new Array(24).fill(0);
    (rows || []).forEach(function(r){
      var hh = parseInt(String(r.at || "").slice(11, 13), 10);
      if(!isNaN(hh) && hh >= 0 && hh < 24) bins[hh]++;
    });
    var total = bins.reduce(function(a, b){ return a + b; }, 0);
    if(!total){
      el.innerHTML = '<div class="empty">还没有流水</div>';
      return;
    }
    var max = Math.max.apply(null, bins);
    var cells = bins.map(function(v, i){
      var a = v / max;
      var bg = v
        ? 'rgba(94,234,212,' + (0.14 + a * 0.68).toFixed(2) + ')'
        : 'rgba(148,163,184,.06)';
      return '<div class="hcell" style="background:' + bg + '" title="' + i + ':00 — ' + v + ' 条">' +
        (v ? '<span>' + (v >= 10 ? '' : v) + '</span>' : '') + '</div>';
    }).join("");

    var peak = bins.indexOf(max);
    var label = peak + ":00–" + (peak + 1) + ":00";
    el.innerHTML = '<div class="hgrid">' + cells + '</div>' +
      '<div class="haxis"><span>0–5</span><span>6–11</span><span>12–17</span><span>18–23</span></div>' +
      '<div class="hnote">共 ' + total + ' 条 · 最活跃 <b>' + label + '</b>（' + max + ' 条）</div>';
  }

  // ── 什么在影响她：把每次心情变化按原因合并，看哪件事最推得动 ──
  function renderCauses(history){
    var el = document.getElementById("causes");
    if(!history || !history.length){
      el.innerHTML = '<div class="empty">还没有心情变化记录</div>';
      return;
    }
    var map = {};
    history.forEach(function(h){
      var d = h.delta || {};
      var v = (Number(d.satisfaction) || 0) + (Number(d.closeness) || 0) + (Number(d.energy) || 0);
      if(!v) return;
      var k = String(h.why || "未说明").trim().slice(0, 14);
      if(!map[k]) map[k] = { n: 0, sum: 0 };
      map[k].n++;
      map[k].sum += v;
    });

    var items = Object.keys(map).map(function(k){
      return { name: k, n: map[k].n, sum: map[k].sum };
    }).sort(function(a, b){ return Math.abs(b.sum) - Math.abs(a.sum); }).slice(0, 6);

    if(!items.length){
      el.innerHTML = '<div class="empty">最近没有产生心情变化</div>';
      return;
    }

    var maxAbs = Math.max.apply(null, items.map(function(i){ return Math.abs(i.sum); })) || 1;
    el.innerHTML = '<div class="clist">' + items.map(function(i){
      var w = Math.max(4, Math.abs(i.sum) / maxAbs * 100);
      var pos = i.sum >= 0;
      var color = pos ? "#5eead4" : "#f87171";
      var sign = pos ? "+" : "";
      return '<div class="crow" title="' + esc(i.name + " · " + i.n + " 次 · " + (pos ? "平均推高 " : "平均拉低 ")) + '">' +
        '<div class="cname">' + esc(i.name) + '</div>' +
        '<div class="cbar"><i style="width:' + w.toFixed(1) + '%;background:' + color +
          ';opacity:' + (pos ? '0.85' : '0.7') + '"></i></div>' +
        '<div class="cval" style="color:' + color + '">' + sign + i.sum.toFixed(1) + '</div>' +
        '<div class="cn">' + i.n + ' 次</div>' +
      '</div>';
    }).join("") + '</div>';
  }

  // 折线图：把 history 的 delta 累加成走势
  // Y 轴按数据范围自适应（增量只有 ±5 量级，固定 -100~100 会压成直线）
  function moodChart(history){
    var svg = document.getElementById("moodChart");
    var W = 820, H = 200, L = 48, R = 54, T = 16, B = 24;
    var rangeEl = document.getElementById("moodRange");
    var lg = {satisfaction: "lg-sat", closeness: "lg-clo", energy: "lg-ene"};

    if(!history || history.length < 2){
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.innerHTML = '<text x="' + (W/2) + '" y="' + (H/2) + '" fill="#7b8bab" font-size="12" text-anchor="middle">还没有足够的历史数据</text>';
      if(rangeEl) rangeEl.textContent = "";
      Object.keys(lg).forEach(function(k){
        var e = document.getElementById(lg[k]); if(e) e.textContent = "—";
      });
      return;
    }

    var pts = history.slice(-20);
    var acc = {satisfaction:0, closeness:0, energy:0};
    var series = {satisfaction:[], closeness:[], energy:[]};

    pts.forEach(function(h, i){
      AXES.forEach(function(a){ acc[a.k] += Number((h.delta || {})[a.k] || 0); });
      AXES.forEach(function(a){
        series[a.k].push({x: i, y: Math.max(-100, Math.min(100, acc[a.k]))});
      });
    });

    var all = [];
    AXES.forEach(function(a){ series[a.k].forEach(function(p){ all.push(p.y); }); });
    var lo = Math.min.apply(null, all);
    var hi = Math.max.apply(null, all);
    var span = Math.max(hi - lo, 4);
    var mid0 = (lo + hi) / 2;
    var half = span * 0.85;
    lo = mid0 - half;
    hi = mid0 + half;

    var maxN = pts.length - 1;
    function toXY(p){
      var x = L + (p.x / (maxN || 1)) * (W - L - R);
      var y = (H - B) - ((p.y - lo) / (hi - lo || 1)) * (H - B - T);
      return [x, y];
    }
    function fmt(v){ return (v >= 0 ? "+" : "") + v.toFixed(1); }

    var parts = [];

    // 横向刻度：三档虚线 + 左侧数值，让"涨了多少"有参照
    [hi, mid0, lo].forEach(function(v){
      var y = (H - B) - ((v - lo) / (hi - lo || 1)) * (H - B - T);
      parts.push('<line x1="' + L + '" y1="' + y.toFixed(1) + '" x2="' + (W - R) + '" y2="' + y.toFixed(1) +
        '" stroke="rgba(120,140,180,.14)" stroke-width="1" stroke-dasharray="3 6"/>');
      parts.push('<text x="' + (L - 7) + '" y="' + (y + 3.5).toFixed(1) +
        '" fill="#5d6d8a" font-size="9.5" text-anchor="end">' + fmt(v) + '</text>');
    });

    // 中轴（净变化为 0 的位置）
    if(lo < 0 && hi > 0){
      var y0 = (H - B) - ((0 - lo) / (hi - lo)) * (H - B - T);
      parts.push('<line x1="' + L + '" y1="' + y0.toFixed(1) + '" x2="' + (W - R) + '" y2="' + y0.toFixed(1) +
        '" stroke="rgba(120,140,180,.32)" stroke-width="1"/>');
    }

    AXES.forEach(function(a){
      parts.push('<defs><linearGradient id="grad-' + a.k + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0%" stop-color="' + a.c + '" stop-opacity="0.30"/>' +
        '<stop offset="100%" stop-color="' + a.c + '" stop-opacity="0"/>' +
        '</linearGradient></defs>');
    });

    AXES.forEach(function(a){
      var s = series[a.k];
      var d = s.map(function(p, i){
        var xy = toXY(p);
        return (i ? "L" : "M") + xy[0].toFixed(1) + " " + xy[1].toFixed(1);
      }).join(" ");

      // 只给第一条画面积：三条都填会互相盖住
      if(a.k === "satisfaction"){
        var last = toXY(s[s.length-1]);
        var first = toXY(s[0]);
        var area = d + " L" + last[0].toFixed(1) + " " + (H - B) + " L" + first[0].toFixed(1) + " " + (H - B) + " Z";
        parts.push('<path d="' + area + '" fill="url(#grad-' + a.k + ')"/>');
      }

      parts.push('<path d="' + d + '" fill="none" stroke="' + a.c + '" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"/>');

      var end = toXY(s[s.length-1]);
      parts.push('<circle cx="' + end[0].toFixed(1) + '" cy="' + end[1].toFixed(1) + '" r="3" fill="' + a.c + '"/>');

      // 末端数值：三条线挤在一起时会重叠，这里按固定间隔错开
      var idx = AXES.indexOf(a);
      var ly = Math.min(Math.max(end[1] + (idx - 1) * 9, T + 4), H - B - 2);
      parts.push('<text x="' + Math.min(end[0] + 8, W - R + 6).toFixed(1) + '" y="' + (ly + 3).toFixed(1) +
        '" fill="' + a.c + '" font-size="10" font-weight="600">' + fmt(acc[a.k]) + '</text>');
    });

    // 起点/终点时间
    function shortTime(iso){
      var s = String(iso || "");
      if(s.length < 16) return "";
      return s.slice(5, 10).replace("-", "/") + " " + s.slice(11, 16);
    }
    parts.push('<text x="' + L + '" y="' + (H - 6) + '" fill="#5d6d8a" font-size="9.5">' + esc(shortTime(pts[0].at)) + '</text>');
    parts.push('<text x="' + (W - R) + '" y="' + (H - 6) + '" fill="#5d6d8a" font-size="9.5" text-anchor="end">' + esc(shortTime(pts[pts.length-1].at)) + '</text>');

    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.innerHTML = parts.join("");

    // 图例里带净变化，光看线看不出涨了多少
    AXES.forEach(function(a){
      var e = document.getElementById(lg[a.k]);
      if(e) e.textContent = fmt(acc[a.k]);
    });
    if(rangeEl) rangeEl.textContent = pts.length + " 个点";
  }

  // ── 状态 ──
  function loadStatus(){
    get("/dsh-companion/status").then(unwrap).then(function(d){
      var st = d.status || d;
      var q = st.quota || {};
      document.getElementById("quota").textContent = (q.count != null) ? String(q.count) : "—";
      document.getElementById("quotaMeta").textContent =
        (q.last_reason ? String(q.last_reason).slice(0, 40) : (q.date || ""));
      document.getElementById("window").textContent = st.in_window ? "活跃" : "休息";
      document.getElementById("windowMeta").textContent =
        st.in_sleep ? "睡眠中" : (st.mood ? "心情 " + st.mood : "");
      var wi = document.getElementById("windowIcon");
      if(wi) wi.textContent = st.in_sleep ? "🌙" : (st.in_window ? "☀️" : "🌗");
      document.title = "莉莉丝 · " + (d.home || st.home || "");

      // 额度上限不在 status 里，得去 rhythm 的 wakes 拿
      get("/dsh-companion/rhythm").then(unwrap).then(function(rm){
        var cap = (rm.windows && rm.windows[0] && rm.windows[0].wakes) || 0;
        document.getElementById("quotaCap").textContent = " / " + (cap || "—");
        renderQuotaChart(q.count, cap);
      }).catch(function(){
        renderQuotaChart(q.count, 0);
      });
    }).catch(function(e){
      document.getElementById("quotaMeta").textContent = "状态读取失败：" + e.message;
    });

    get("/dsh-companion/mood").then(unwrap).then(function(d){
      var m = d.mood || d;
      state.moodNow = m;
      state.moodHistory = m.history || [];
      var MOOD_ICON = {
        "雀跃": "🤩", "开心": "😊", "平静": "😐", "难过": "😢",
        "低落": "😔", "兴奋": "😆", "疲惫": "😪"
      };
      document.getElementById("moodIcon").textContent = MOOD_ICON[m.label] || "🙂";
      document.getElementById("moodLabel").textContent = m.label || "—";
      document.getElementById("moodAt").textContent =
        m.last_change ? "最近变化 " + String(m.last_change).replace("T", " ") : "";
      var el = document.getElementById("axes");
      el.innerHTML = AXES.map(function(a){
        var v = Number((m.axes || {})[a.k] || 0);
        return '<div class="axis"><div class="axis-label"><span>' + a.n +
          '</span><span class="num">' + v.toFixed(2) + '</span></div>' + bar(v, a.c) + '</div>';
      }).join("");
      moodChart(state.moodHistory);
      renderCauses(state.moodHistory);
    }).catch(function(e){
      document.getElementById("moodLabel").textContent = "读取失败";
    });

    get("/dsh-companion/life?limit=200").then(unwrap).then(function(rows){
      state.lifeRows = rows || [];
      renderLife(state.lifeRows, "life", 6);
      renderKindChart(state.lifeRows);
      renderHourHeat(state.lifeRows);
    }).catch(function(e){
      document.getElementById("life").innerHTML = '<div class="bad">' + esc(e.message) + '</div>';
    });

    get("/dsh-companion/turns?limit=6").then(unwrap).then(renderSaid)
      .catch(function(e){
        document.getElementById("said").innerHTML = '<div class="bad">' + esc(e.message) + '</div>';
      });
  }

  function renderLife(rows, elId, n){
    var el = document.getElementById(elId);
    if(!rows || !rows.length){ el.innerHTML = '<div class="empty">还没有记录</div>'; return; }
    el.innerHTML = '<ul>' + rows.slice(0, n).map(function(r, i){
      var detail = String(r.detail || "").slice(0, 90);
      return '<li data-life="' + i + '"><div>' + esc(r.title || "") + '</div>' +
             (detail ? '<div class="meta">' + esc(detail) + '</div>' : '') +
             '<div class="meta">' + esc(r.at || "") + ' · ' + esc(r.kind || "") + '</div></li>';
    }).join("") + '</ul>';

    el.querySelectorAll("[data-life]").forEach(function(node){
      node.addEventListener("click", function(){
        var r = rows[Number(node.dataset.life)];
        if(!r) return;
        showModal(r.title || "详情",
          (r.at ? r.at + (r.kind ? " · " + r.kind : "") + "\\n\\n" : "") + (r.detail || "(没有更多内容)"));
      });
    });
  }

  function renderSaid(rows){
    var el = document.getElementById("said");
    if(!rows || !rows.length){ el.innerHTML = '<div class="empty">还没说过话</div>'; return; }
    el.innerHTML = '<ul>' + rows.map(function(r, i){
      var who = r.role === "user" ? "你" : "她";
      var at = String(r.at || "").replace("T", " ").slice(5, 16);
      return '<li data-said="' + i + '"><div class="row"><span class="tag">' + who + '</span>' +
             '<span class="meta">' + esc(at) + '</span></div>' +
             '<div class="said">' + esc(cleanCQ(r.said).slice(0, 240)) + '</div></li>';
    }).join("") + '</ul>';

    el.querySelectorAll("[data-said]").forEach(function(node){
      node.addEventListener("click", function(){
        var r = rows[Number(node.dataset.said)];
        if(!r) return;
        var who = r.role === "user" ? "你" : "她";
        showModal(who + " · " + String(r.at || "").replace("T", " "),
          cleanCQ(r.said) + (r.why ? "\\n\\n（" + r.why + "）" : ""));
      });
    });
  }

  // 心情卡点开：显示完整心情历史
  document.getElementById("card-mood").addEventListener("click", function(){
    var h = state.moodHistory || [];
    if(!h.length){ showModal("心情历史", "还没有记录"); return; }
    var lines = h.slice(-30).reverse().map(function(x){
      var d = x.delta || {};
      var ds = Object.keys(d).map(function(k){
        var name = (AXES.find(function(a){ return a.k === k; }) || {}).n || k;
        var v = Number(d[k]);
        return name + (v > 0 ? "+" : "") + v.toFixed(1);
      }).join(" ");
      return String(x.at || "").replace("T", " ") + "  " + (x.label || "") + "  " + ds +
        (x.why ? "\\n    " + x.why : "");
    });
    showModal("心情历史（最近 30 次）", lines.join("\\n\\n"));
  });

  // ── 设置 ──
  function loadSettings(){
    get("/dsh-companion/rhythm").then(unwrap).then(function(d){
      document.getElementById("s-enabled").checked = !!d.enabled;
      var sl = d.sleep || {};
      document.getElementById("s-sleep-at").value = sl.at || "";
      document.getElementById("s-wake").value = sl.wake || "";
      var w = (d.windows && d.windows[0]) || {};
      document.getElementById("s-win-start").value = w.start || "";
      document.getElementById("s-win-end").value = w.end || "";
      document.getElementById("s-wakes").value = (w.wakes != null ? w.wakes : "");
    }).catch(function(e){
      document.getElementById("s-msg").innerHTML = '<span class="bad">读取失败：' + esc(e.message) + '</span>';
    });
  }

  function saveSettings(){
    var msg = document.getElementById("s-msg");
    var body = {
      enabled: document.getElementById("s-enabled").checked,
      sleep: { at: document.getElementById("s-sleep-at").value.trim(),
               wake: document.getElementById("s-wake").value.trim() },
      windows: [{ start: document.getElementById("s-win-start").value.trim(),
                  end: document.getElementById("s-win-end").value.trim(),
                  wakes: Number(document.getElementById("s-wakes").value) || 0 }]
    };
    msg.textContent = "保存中…";
    fetch("/dsh-companion/rhythm/save", {
      method:"POST", headers:{"content-type":"application/json"},
      credentials:"same-origin", body: JSON.stringify(body)
    }).then(function(r){ return r.json(); }).then(function(d){
      if(d && d.ok === false) throw new Error(d.error || "保存失败");
      msg.innerHTML = '<span class="ok">已保存</span>';
      loadStatus();
    }).catch(function(e){
      msg.innerHTML = '<span class="bad">保存失败：' + esc(e.message) + '</span>';
    });
  }

  // ── 高级参数 ──
  function flatten(obj, prefix, out){
    out = out || [];
    Object.keys(obj || {}).forEach(function(k){
      var v = obj[k];
      var key = prefix ? prefix + "." + k : k;
      if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out);
      else out.push({ key: key, value: v });
    });
    return out;
  }

  // ── 高级参数：一张表，四项对齐 ────────────────────────
  // 名称 / 键名 | 默认值 | 输入框+存。默认值和可写范围都由 host 半给，
  // 前端不再自己维护一份，避免两边对不上。
  function fmtDefault(spec){
    var d = spec.def;
    if(d === null || d === undefined) return "—";
    if(typeof d === "boolean") return d ? "开" : "关";
    if(Array.isArray(d)) return d.length + " 项";
    return String(d);
  }

  function fmtValue(v){
    if(v === null || v === undefined) return "";
    if(typeof v === "boolean") return v ? "true" : "false";
    if(Array.isArray(v)) return v.join("，");
    return String(v);
  }

  function loadAdvanced(){
    var el = document.getElementById("adv-list");
    get("/dsh-companion/params").then(unwrap).then(function(d){
      var items = d.items || [];
      var groups = {};
      items.forEach(function(it){
        var g = it.g || "其他";
        (groups[g] = groups[g] || []).push(it);
      });

      var html = Object.keys(groups).map(function(g){
        var rows = groups[g].map(function(it){
          var changed = it.set && JSON.stringify(it.value) !== JSON.stringify(it.def);
          var dirty = changed ? " dirty" : "";
          return '<div class="prow' + dirty + '" data-def="' + esc(JSON.stringify(it.def)) + '"' +
                 ' title="' + esc(it.key + " → " + it.file) + '">' +
            '<div class="pinfo">' +
              '<div class="plabel">' + esc(it.n) + '</div>' +
              '<div class="pkey">' + esc(it.key) + '</div>' +
            '</div>' +
            '<div class="pdef">默认 ' + esc(fmtDefault(it)) + '</div>' +
            '<div class="pctl">' +
              '<input type="text" data-pkey="' + esc(it.key) + '" value="' + esc(fmtValue(it.value)) + '"' +
                ' placeholder="—">' +
              '<button class="pbtn" data-psave="' + esc(it.key) + '">存</button>' +
            '</div>' +
          '</div>';
        }).join("");

        return '<details class="pgroup">' +
          '<summary><span>' + esc(g) + '</span><span class="num">' + groups[g].length + '</span></summary>' +
          '<div class="pgroup-body">' + rows + '</div>' +
        '</details>';
      }).join("");

      el.innerHTML = html || '<div class="empty">没有可编辑的参数</div>';

      el.querySelectorAll("[data-psave]").forEach(function(btn){
        btn.addEventListener("click", function(){
          var k = btn.dataset.psave;
          var input = el.querySelector('[data-pkey="' + k + '"]');
          saveParam(k, input ? input.value.trim() : "");
        });
      });
    }).catch(function(e){
      el.innerHTML = '<div class="bad">读取失败：' + esc(e.message) + '</div>';
    });
  }

  function saveParam(key, raw){
    var msg = document.getElementById("adv-msg");
    msg.textContent = "写入 " + key + "…";
    fetch("/dsh-companion/params/save", {
      method:"POST", headers:{"content-type":"application/json"},
      credentials:"same-origin",
      body: JSON.stringify({ key: key, value: raw })
    }).then(function(r){ return r.json(); }).then(function(d){
      if(d && d.ok === false) throw new Error(d.error || "写入失败");
      var shown = d.value === null ? "默认" : fmtValue(d.value);
      msg.innerHTML = '<span class="ok">已写入 ' + esc(key) + ' = ' + esc(shown) +
        ' <span style="opacity:.6">（' + esc(d.file || "") + '）</span></span>';
      // 保存后把输入框对齐到落盘值，避免显示和实际不一致
      var input = document.querySelector('[data-pkey="' + key + '"]');
      if(input) input.value = d.value === null ? "" : fmtValue(d.value);
      var row = input && input.closest(".prow");
      if(row){
        var same = d.value === null || JSON.stringify(d.value) === row.dataset.def;
        row.classList.toggle("dirty", !same);
      }
    }).catch(function(e){
      msg.innerHTML = '<span class="bad">写入失败：' + esc(e.message) + '</span>';
    });
  }

  function saveAdvanced(){
    var msg = document.getElementById("adv-msg");
    var key = document.getElementById("adv-key").value.trim();
    var raw = document.getElementById("adv-val").value.trim();
    if(!key){ msg.innerHTML = '<span class="bad">先填键名</span>'; return; }
    var value = raw === "" ? "" : (isNaN(Number(raw)) ? raw : Number(raw));

    msg.textContent = "写入中…";
    fetch("/dsh-companion/settings/save", {
      method:"POST", headers:{"content-type":"application/json"},
      credentials:"same-origin", body: JSON.stringify({ key: key, value: value })
    }).then(function(r){ return r.json(); }).then(function(d){
      if(d && d.ok === false) throw new Error(d.error || "写入失败");
      msg.innerHTML = '<span class="ok">已写入 ' + esc(key) + '</span>';
      loadAdvanced();
    }).catch(function(e){
      msg.innerHTML = '<span class="bad">写入失败：' + esc(e.message) + '</span>';
    });
  }

  // ── 人格 ──
  function currentDocName(){
    var el = document.getElementById("p-name");
    return (el && el.value) || "soul";
  }

  function loadPersona(){
    get("/dsh-companion/doc?name=" + encodeURIComponent(currentDocName())).then(unwrap).then(function(d){
      document.getElementById("p-title").textContent = d.title || "人格";
      document.getElementById("p-text").value = d.text || "";
      document.getElementById("p-meta").textContent = (d.chars || 0) + " 字";
    }).catch(function(e){
      document.getElementById("p-meta").innerHTML = '<span class="bad">读取失败：' + esc(e.message) + '</span>';
    });
  }

  function savePersona(){
    var msg = document.getElementById("p-msg");
    var text = document.getElementById("p-text").value;
    msg.textContent = "保存中…";
    fetch("/dsh-companion/doc/save", {
      method:"POST", headers:{"content-type":"application/json"},
      credentials:"same-origin", body: JSON.stringify({name: currentDocName(), text:text})
    }).then(function(r){ return r.json(); }).then(function(d){
      if(d && d.ok === false) throw new Error(d.error || "保存失败");
      msg.innerHTML = '<span class="ok">已保存 ' + ((d.data && d.data.chars) || text.length) + ' 字</span>';
      document.getElementById("p-meta").textContent = text.length + " 字";
    }).catch(function(e){
      msg.innerHTML = '<span class="bad">保存失败：' + esc(e.message) + '</span>';
    });
  }

  // ── 日记 ──
  function loadDiary(){
    get("/dsh-companion/life?limit=30").then(unwrap).then(function(rows){
      state.lifeRows = rows || [];
      renderLife(state.lifeRows, "diary", 30);
      renderKindChart(state.lifeRows);
    }).catch(function(e){
      document.getElementById("diary").innerHTML = '<div class="bad">' + esc(e.message) + '</div>';
    });
  }

  // ── 页签 ──
  function show(tab){
    document.querySelectorAll(".tabs button").forEach(function(b){
      b.classList.toggle("on", b.dataset.tab === tab);
    });
    document.querySelectorAll(".page").forEach(function(p){
      p.classList.toggle("on", p.id === "page-" + tab);
    });
    if(tab === "status" && !loaded.status){ loaded.status = true; loadStatus(); }
    if(tab === "settings" && !loaded.settings){ loaded.settings = true; loadSettings(); loadAdvanced(); }
    if(tab === "persona" && !loaded.persona){ loaded.persona = true; loadPersona(); }
    if(tab === "diary" && !loaded.diary){ loaded.diary = true; loadDiary(); }
    var t = new Date();
    document.getElementById("stamp").textContent = "更新于 " + t.toTimeString().slice(0,8);
  }

  document.querySelectorAll(".tabs button").forEach(function(b){
    b.addEventListener("click", function(){ show(b.dataset.tab); });
  });

  document.getElementById("s-save").addEventListener("click", saveSettings);
  document.getElementById("adv-save").addEventListener("click", saveAdvanced);
  document.getElementById("p-save").addEventListener("click", savePersona);
  document.getElementById("p-reload").addEventListener("click", loadPersona);
  document.getElementById("p-name").addEventListener("change", loadPersona);
  document.getElementById("btn").addEventListener("click", function(){
    loaded = {status:false, settings:false, persona:false, diary:false};
    hideModal();
    show(document.querySelector(".tabs button.on").dataset.tab);
  });

  show("status");
})();
</script>
</body>
</html>`;
}
