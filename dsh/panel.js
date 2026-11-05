// DSH 面板的 HTML。单文件、无外部依赖、不引入构建步骤。
//
// 为什么不复用 hermes 那个 dist/index.js：那个是挂在 dashboard 插件框架里的
// IIFE，依赖 SDK 注入的 React；DSH 的 webServer 只负责吐字节，没有那层壳。
// 所以这里写一份独立的、只依赖 fetch 的实现，两种宿主各跑各的。

export function PANEL_HTML() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉丝 · DSH 面板</title>
<style>
  :root{
    --bg:#0b1020; --surf:#141b30; --line:#25314f; --dim:#7b8bab;
    --main:#5eead4; --violet:#a78bfa; --warn:#fbbf24; --bad:#fb7185;
    --text:#e6edf7;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:20px;background:var(--bg);color:var(--text);
       font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1100px;margin:0 auto}
  h1{font-size:16px;font-weight:600;margin:0 0 4px}
  .sub{font-size:11.5px;color:var(--dim);margin-bottom:16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-bottom:14px}
  .card{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:14px}
  .t{font-size:11px;color:var(--dim);margin-bottom:8px;letter-spacing:.03em}
  .big{font-size:22px;font-weight:600;line-height:1.25}
  .bar{height:6px;border-radius:99px;background:rgba(148,163,184,.18);overflow:hidden;margin-top:5px}
  .bar i{display:block;height:100%;border-radius:99px;transition:width .4s}
  .row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .axis{margin-bottom:10px}
  .axis:last-child{margin-bottom:0}
  .num{font-variant-numeric:tabular-nums;font-size:12px;color:var(--dim)}
  ul{list-style:none;margin:0;padding:0}
  li{padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
  li:last-child{border-bottom:none}
  .meta{font-size:11px;color:var(--dim);margin-top:2px}
  .said{white-space:pre-wrap;word-break:break-word}
  .tag{font-size:10px;padding:1px 6px;border-radius:5px;border:1px solid var(--line);color:var(--dim)}
  .err{color:var(--bad);font-size:12px}
  .empty{color:var(--dim);font-size:12px}
  .foot{margin-top:14px;font-size:11px;color:var(--dim);display:flex;justify-content:space-between}
  button{background:transparent;border:1px solid var(--line);color:var(--text);
         border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer}
  button:hover{border-color:var(--main);color:var(--main)}
</style>
</head>
<body>
<div class="wrap">
  <h1>莉莉丝</h1>
  <div class="sub" id="sub">读取中…</div>

  <div class="grid">
    <div class="card">
      <div class="t">心情</div>
      <div class="big" id="moodLabel">—</div>
      <div class="meta" id="moodAt"></div>
    </div>
    <div class="card">
      <div class="t">今日额度</div>
      <div class="big" id="quota">—</div>
      <div class="meta" id="quotaMeta"></div>
    </div>
    <div class="card">
      <div class="t">当前时段</div>
      <div class="big" id="window">—</div>
      <div class="meta" id="windowMeta"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <div class="t">心情三轴</div>
    <div id="axes"></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="t">最近在做什么</div>
      <ul id="life"><li class="empty">读取中…</li></ul>
    </div>
    <div class="card">
      <div class="t">最近说过的话</div>
      <ul id="said"><li class="empty">读取中…</li></ul>
    </div>
  </div>

  <div class="foot">
    <span id="stamp"></span>
    <button id="btn">刷新</button>
  </div>
</div>

<script>
(function(){
  var AXES = [
    {k:"satisfaction", n:"满足感", c:"#5eead4"},
    {k:"closeness",   n:"亲近感", c:"#a78bfa"},
    {k:"energy",      n:"精力",   c:"#fbbf24"}
  ];

  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  function get(url){
    return fetch(url, {credentials:"same-origin", cache:"no-store"})
      .then(function(r){
        if(!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  // runJson 包了一层 {ok, data}；失败时是 {ok:false, error}
  function unwrap(r){
    if(r && r.ok === false) throw new Error(r.error || "调用失败");
    return (r && Object.prototype.hasOwnProperty.call(r, "data")) ? r.data : r;
  }

  function bar(v, color){
    var pct = Math.max(0, Math.min(100, (Number(v) + 100) / 2));
    return '<div class="bar"><i style="width:' + pct + '%;background:' + color + '"></i></div>';
  }

  function renderAxes(axes){
    var el = document.getElementById("axes");
    el.innerHTML = AXES.map(function(a){
      var v = Number((axes || {})[a.k] || 0);
      return '<div class="axis"><div class="row"><span style="font-size:12px">' + a.n +
        '</span><span class="num">' + v.toFixed(2) + '</span></div>' + bar(v, a.c) + '</div>';
    }).join("");
  }

  function renderLife(rows){
    var el = document.getElementById("life");
    if(!rows || !rows.length){ el.innerHTML = '<li class="empty">还没有记录</li>'; return; }
    el.innerHTML = rows.map(function(r){
      var title = esc(r.title || "");
      var detail = esc(String(r.detail || "").slice(0, 90));
      return '<li><div>' + title + '</div>' +
             (detail ? '<div class="meta">' + detail + '</div>' : '') +
             '<div class="meta">' + esc(r.at || "") + ' · ' + esc(r.kind || "") + '</div></li>';
    }).join("");
  }

  function renderSaid(rows){
    var el = document.getElementById("said");
    if(!rows || !rows.length){ el.innerHTML = '<li class="empty">还没说过话</li>'; return; }
    el.innerHTML = rows.map(function(r){
      var who = r.role === "user" ? "你" : "她";
      var at = String(r.at || "").replace("T", " ").slice(5, 16);
      return '<li><div class="row"><span class="tag">' + who + '</span>' +
             '<span class="meta">' + esc(at) + '</span></div>' +
             '<div class="said">' + esc(String(r.said || "").slice(0, 220)) + '</div></li>';
    }).join("");
  }

  function fail(id, msg){
    var el = document.getElementById(id);
    if(el) el.innerHTML = '<li class="err">' + esc(msg) + '</li>';
  }

  function load(){
    var p1 = get("/dsh-companion/status").then(unwrap).then(function(d){
      // 原实现返回 {status, cost, home}，真正的状态在 d.status 里
      var st = d.status || d;
      var q = st.quota || {};
      document.getElementById("quota").textContent =
        (q.count != null) ? String(q.count) : "—";
      document.getElementById("quotaMeta").textContent =
        (q.last_reason ? String(q.last_reason).slice(0, 40) : (q.date || ""));
      document.getElementById("window").textContent = st.in_window ? "活跃" : "休息";
      document.getElementById("windowMeta").textContent =
        st.in_sleep ? "睡眠中" : (st.mood ? "心情 " + st.mood : "");
      document.getElementById("sub").textContent = d.home || st.home || "";
    }).catch(function(e){ document.getElementById("sub").textContent = "状态读取失败：" + e.message; });

    var p2 = get("/dsh-companion/mood").then(unwrap).then(function(d){
      var m = d.mood || d;
      document.getElementById("moodLabel").textContent = m.label || "—";
      document.getElementById("moodAt").textContent =
        m.last_change ? "最近变化 " + String(m.last_change).replace("T", " ") : "";
      renderAxes(m.axes);
    }).catch(function(e){ document.getElementById("moodLabel").textContent = "读取失败"; });

    var p3 = get("/dsh-companion/life?limit=6").then(unwrap)
      .then(renderLife).catch(function(e){ fail("life", e.message); });

    var p4 = get("/dsh-companion/turns?limit=6").then(unwrap)
      .then(renderSaid).catch(function(e){ fail("said", e.message); });

    Promise.all([p1,p2,p3,p4]).then(function(){
      var t = new Date();
      document.getElementById("stamp").textContent =
        "更新于 " + t.toTimeString().slice(0,8) + " · 每 30 秒自动刷新";
    });
  }

  document.getElementById("btn").addEventListener("click", load);
  load();
  setInterval(load, 30000);
})();
</script>
</body>
</html>`;
}
