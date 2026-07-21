import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const UA = "Mozilla/5.0";
const users = new Map();
const tokens = new Map();

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

function parseCode(raw) {
  raw = raw.trim().toLowerCase();
  let m, c;
  if (raw.startsWith("sh")) { m = "sh"; c = raw.slice(2); }
  else if (raw.startsWith("sz")) { m = "sz"; c = raw.slice(2); }
  else { c = raw; m = (c.startsWith("6") || c.startsWith("9")) ? "sh" : "sz"; }
  return { code: c, market: m, sina_code: `${m}${c}` };
}

async function sha256(str) {
  const d = new TextEncoder().encode(str);
  const h = await crypto.subtle.digest("SHA-256", d);
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, "0")).join("");
}

function rndToken() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a).map(b => b.toString(16).padStart(2, "0")).join("");
}

function getUser(token) {
  if (!token || !tokens.has(token)) return null;
  const n = tokens.get(token);
  return users.has(n) ? { name: n, ...users.get(n) } : null;
}

async function fetchTrend(code, market) {
  const u = `https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code=${market}${code}`;
  const r = await fetch(u, { headers: { "User-Agent": UA, Referer: "https://gu.qq.com/" } });
  const t = await r.text();
  const j = JSON.parse(t.replace("min_data=", "").trim().replace(/;$/, ""));
  if (j.code !== 0 || !j.data) return null;
  const s = j.data[`${market}${code}`];
  if (!s) return null;
  const inner = s.data || s;
  const mins = inner.data || [];
  const qt = (s.qt || {})[`${market}${code}`] || [];
  const pre = parseFloat(qt[4]) || 0;
  const tr = [];
  let pv = 0, pa = 0;
  for (const m of mins) {
    const p = m.split(" ");
    if (p.length < 3) continue;
    const cv = parseInt(p[2]) || 0, ca = parseFloat(p[3]) || 0;
    let vv = cv - pv; if (vv < 0) vv = 0;
    let aa = ca - pa; if (aa < 0) aa = 0;
    pv = cv; pa = ca;
    tr.push({ time: p[0], price: parseFloat(p[1]), volume: vv, amount: aa });
  }
  let cv2 = 0, ca2 = 0;
  for (const x of tr) { cv2 += x.volume; ca2 += x.amount; x.avg_price = cv2 > 0 ? +(ca2 / (cv2 * 100)).toFixed(2) : x.price; }
  computeDK(tr);
  return { code, preClose: pre, trends: tr };
}

function computeDK(tr) {
  const p = tr.map(t => t.price), n = tr.length;
  const rsv = (i, w) => {
    if (i < w - 1) return null;
    const s = p.slice(i - w + 1, i + 1);
    const hi = Math.max(...s), lo = Math.min(...s);
    return hi === lo ? 50 : (p[i] - lo) / (hi - lo) * 100;
  };
  const sma = (pr, x, nv) => pr == null ? x : (x + (nv - 1) * pr) / nv;
  let sp = null, kp = null, zp = null, wp = null;
  for (let i = 0; i < n; i++) {
    const rs = rsv(i, 5);
    if (rs !== null) { sp = sma(sp, rs, 3); kp = sma(kp, sp, 3); }
    tr[i].sd = sp != null ? +sp.toFixed(2) : null;
    tr[i].sk = kp != null ? +kp.toFixed(2) : null;
    const rm = rsv(i, 20);
    if (rm !== null) { zp = sma(zp, rm, 5); wp = sma(wp, zp, 10); }
    tr[i].zd = zp != null ? +zp.toFixed(2) : null;
    tr[i].zk = wp != null ? +wp.toFixed(2) : null;
  }
}

async function fetchQuote(sc) {
  try {
    const r = await fetch(`http://hq.sinajs.cn/list=${sc}`, { headers: { "User-Agent": UA, Referer: "https://finance.sina.com.cn/" } });
    const t = await r.text();
    const m = t.match(/"([^"]*)"/);
    if (!m) return null;
    const f = m[1].split(",");
    if (f.length < 32) return null;
    return { name: f[0], price: parseFloat(f[3]) || 0, preClose: parseFloat(f[2]) || 0 };
  } catch { return null; }
}

function detectEvents(tr) {
  const p = tr.map(t => t.price), ev = [];
  const chg = (i, m) => i < m ? 0 : (p[i] - p[i - m]) / p[i - m] * 100;
  let lu = 0, ld = 0;
  for (let i = 1; i < p.length; i++) {
    const c = chg(i, 3);
    const su = c >= 1.8 ? 3 : (c >= 1.2 ? 2 : (c >= 0.8 ? 1 : 0));
    const sd = c <= -1.8 ? 3 : (c <= -1.2 ? 2 : (c <= -0.8 ? 1 : 0));
    if (su > lu) ev.push({t:"surge",l:["","轻度急拉","中度急拉","强烈急拉"][su],s:["","low","medium","high"][su],p:p[i],d:`+${c.toFixed(1)}%/3min`,c:["","#ff9933","#ff7733","#ff3333"][su],time:tr[i].time});
    lu = su;
    if (sd > ld) ev.push({t:"plunge",l:["","轻度急跌","中度急跌","强烈急跌"][sd],s:["","low","medium","high"][sd],p:p[i],d:`${c.toFixed(1)}%/3min`,c:["","#009944","#00aa44","#00cc44"][sd],time:tr[i].time});
    ld = sd;
    if (i >= 5) {
      const hi = Math.max(...p.slice(0, i)), lo = Math.min(...p.slice(0, i));
      if (p[i] > hi) ev.push({t:"new_high",l:"新高",s:"low",p:p[i],d:`¥${p[i].toFixed(2)}`,c:"#ff4444",time:tr[i].time});
      if (p[i] < lo) ev.push({t:"new_low",l:"新低",s:"low",p:p[i],d:`¥${p[i].toFixed(2)}`,c:"#00cc66",time:tr[i].time});
    }
  }
  return ev;
}

let INDEX_HTML = "<html><body><h1>Loading...</h1></body></html>";
try {
  INDEX_HTML = await Deno.readTextFile("./index.html");
} catch { /* use default */ }

serve(async (req) => {
  const url = new URL(req.url);
  const path = url.pathname;
  const auth = req.headers.get("Authorization")?.replace("Bearer ", "") || "";
  const user = getUser(auth);

  // Auth
  if (path === "/api/auth/register" && req.method === "POST") {
    try {
      const b = await req.json();
      const u = (b.username || "").trim(), p = (b.password || "").trim();
      if (!u || !p) return json({ error: "用户名和密码不能为空" }, 400);
      if (u.length < 2) return json({ error: "用户名至少2个字符" }, 400);
      if (p.length < 3) return json({ error: "密码至少3位" }, 400);
      if (users.has(u)) return json({ error: "用户名已存在" }, 400);
      users.set(u, { password: await sha256(p), watchlist: [], webhook: "" });
      const tk = rndToken();
      tokens.set(tk, u);
      return json({ ok: true, token: tk, username: u });
    } catch (e) { return json({ error: "请求格式错误" }, 400); }
  }
  if (path === "/api/auth/login" && req.method === "POST") {
    try {
      const b = await req.json();
      const u = (b.username || "").trim(), p = (b.password || "").trim();
      if (!users.has(u)) return json({ error: "用户名或密码错误" }, 401);
      if (users.get(u).password !== await sha256(p)) return json({ error: "用户名或密码错误" }, 401);
      const tk = rndToken();
      tokens.set(tk, u);
      return json({ ok: true, token: tk, username: u });
    } catch (e) { return json({ error: "请求格式错误" }, 400); }
  }
  if (path === "/api/auth/logout" && req.method === "POST") {
    if (auth) tokens.delete(auth);
    return json({ ok: true });
  }

  // Protected
  if (!user && path.startsWith("/api/") && !path.startsWith("/api/auth/")) {
    return json({ error: "请先登录" }, 401);
  }

  if (path === "/api/watchlist/sync" && req.method === "POST") {
    try {
      const b = await req.json();
      if (b.watchlist && user) { user.watchlist = b.watchlist; users.set(user.name, user); }
      return json({ ok: true, watchlist: user?.watchlist || [], role: "user" });
    } catch { return json({ ok: true, watchlist: user?.watchlist || [], role: "user" }); }
  }

  if (path === "/api/signals") {
    const codes = (url.searchParams.get("codes") || "").split(",").filter(c => c.trim()).slice(0, 10);
    const results = [];
    for (const raw of codes) {
      try {
        const info = parseCode(raw);
        const d = await fetchTrend(info.code, info.market);
        if (!d || !d.trends || d.trends.length < 10) continue;
        const ev = detectEvents(d.trends);
        const q = await fetchQuote(info.sina_code);
        results.push({ code: info.code, name: q?.name || info.code, last_price: d.trends[d.trends.length - 1].price, pre_close: d.preClose, events: ev.slice(-50) });
      } catch { continue; }
    }
    return json({ signals: results, time: new Date().toISOString().slice(11, 19) });
  }

  if (path === "/api/trend") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return json({ error: "no code" }, 400);
    const info = parseCode(raw);
    const d = await fetchTrend(info.code, info.market);
    return d ? json(d) : json({ error: "fetch failed" }, 500);
  }

  if (path === "/api/quote") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return json({ error: "no code" }, 400);
    const info = parseCode(raw);
    const q = await fetchQuote(info.sina_code);
    return q ? json({ code: info.code, ...q }) : json({ error: "fetch failed" }, 500);
  }

  return new Response(INDEX_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}, { port: 8000 });
