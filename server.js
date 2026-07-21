// A股分时图看盘 - Deno Deploy 版
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const UA = "Mozilla/5.0";
const kv = await Deno.openKv();
const TOKEN_PREFIX = "token_";
const USER_PREFIX = "user_";

function parseCode(raw) {
  raw = raw.trim().toLowerCase();
  let market, code;
  if (raw.startsWith("sh")) { market = "sh"; code = raw.slice(2); }
  else if (raw.startsWith("sz")) { market = "sz"; code = raw.slice(2); }
  else {
    code = raw;
    if (code.startsWith("6") || code.startsWith("9")) market = "sh";
    else market = "sz";
  }
  return { code, market, sina_code: `${market}${code}` };
}

async function hash(str) {
  const data = new TextEncoder().encode(str);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function getUser(token) {
  if (!token) return null;
  const userEntry = await kv.get([TOKEN_PREFIX, token]);
  if (!userEntry.value) return null;
  const userName = userEntry.value;
  const userData = await kv.get([USER_PREFIX, userName]);
  return userData.value ? { name: userName, ...userData.value } : null;
}

function randomToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function fetchTrend(code, market) {
  const tcode = `${market}${code}`;
  const url = `https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code=${tcode}`;
  const resp = await fetch(url, { headers: { "User-Agent": UA, "Referer": "https://gu.qq.com/" } });
  const text = await resp.text();
  const jsonStr = text.replace("min_data=", "").trim().replace(/;$/, "");
  const data = JSON.parse(jsonStr);
  if (data.code !== 0 || !data.data) return null;
  const stock = data.data[tcode];
  if (!stock) return null;
  const inner = stock.data || stock;
  const mins = inner.data || [];
  const qt = stock.qt || {};
  const qtArr = qt[tcode] || [];
  const preClose = parseFloat(qtArr[4]) || 0;

  const trends = [];
  let prevVol = 0, prevAmt = 0;
  for (const m of mins) {
    const parts = m.split(" ");
    if (parts.length >= 3) {
      const cumVol = parseInt(parts[2]) || 0;
      const cumAmt = parseFloat(parts[3]) || 0;
      let perVol = cumVol - prevVol; if (perVol < 0) perVol = 0;
      let perAmt = cumAmt - prevAmt; if (perAmt < 0) perAmt = 0;
      prevVol = cumVol; prevAmt = cumAmt;
      trends.push({ time: parts[0], price: parseFloat(parts[1]), volume: perVol, amount: perAmt });
    }
  }
  let cumV = 0, cumA = 0;
  for (const t of trends) {
    cumV += t.volume; cumA += t.amount;
    t.avg_price = cumV > 0 ? Math.round(cumA / (cumV * 100) * 100) / 100 : t.price;
  }
  // 计算多空指标
  computeDuokong(trends);
  return { code, preClose, trends };
}

function computeDuokong(trends) {
  const n = trends.length;
  const prices = trends.map(t => t.price);
  const rsv = (idx, w) => {
    if (idx < w - 1) return null;
    const seg = prices.slice(idx - w + 1, idx + 1);
    const hh = Math.max(...seg), ll = Math.min(...seg);
    return hh === ll ? 50 : (prices[idx] - ll) / (hh - ll) * 100;
  };
  const sma = (prev, x, nv, m = 1) => prev == null ? x : (m * x + (nv - m) * prev) / nv;
  let sdPrev = null, skPrev = null, zdPrev = null, zkPrev = null;
  for (let i = 0; i < n; i++) {
    const rs = rsv(i, 5);
    if (rs !== null) { sdPrev = sma(sdPrev, rs, 3); skPrev = sma(skPrev, sdPrev, 3); }
    trends[i].sd = sdPrev !== null ? Math.round(sdPrev * 100) / 100 : null;
    trends[i].sk = skPrev !== null ? Math.round(skPrev * 100) / 100 : null;
    const rm = rsv(i, 20);
    if (rm !== null) { zdPrev = sma(zdPrev, rm, 5); zkPrev = sma(zkPrev, zdPrev, 10); }
    trends[i].zd = zdPrev !== null ? Math.round(zdPrev * 100) / 100 : null;
    trends[i].zk = zkPrev !== null ? Math.round(zkPrev * 100) / 100 : null;
  }
}

async function fetchQuote(sinaCode) {
  try {
    const url = `http://hq.sinajs.cn/list=${sinaCode}`;
    const resp = await fetch(url, { headers: { "User-Agent": UA, "Referer": "https://finance.sina.com.cn/" } });
    const text = await resp.text();
    const match = text.match(/"([^"]*)"/);
    if (!match) return null;
    const fields = match[1].split(",");
    if (fields.length < 32) return null;
    return { name: fields[0], price: parseFloat(fields[3]) || 0, preClose: parseFloat(fields[2]) || 0 };
  } catch { return null; }
}

function detectEvents(trends) {
  const prices = trends.map(t => t.price);
  const volumes = trends.map(t => t.volume);
  const n = prices.length, events = [];
  const chg = (i, m) => i < m ? 0 : (prices[i] - prices[i - m]) / prices[i - m] * 100;
  let lsu = 0, lsd = 0;
  for (let i = 1; i < n; i++) {
    const c3 = chg(i, 3);
    const su = c3 >= 1.8 ? 3 : (c3 >= 1.2 ? 2 : (c3 >= 0.8 ? 1 : 0));
    const sd = c3 <= -1.8 ? 3 : (c3 <= -1.2 ? 2 : (c3 <= -0.8 ? 1 : 0));
    if (su > lsu) {
      const lb = ["","轻度急拉","中度急拉","强烈急拉"], cl = ["","#ff9933","#ff7733","#ff3333"], se = ["","low","medium","high"];
      events.push({t:"surge",l:lb[su],s:se[su],p:prices[i],d:`+${c3.toFixed(1)}%/3min`,c:cl[su],time:trends[i].time});
    }
    lsu = su;
    if (sd > lsd) {
      const lb = ["","轻度急跌","中度急跌","强烈急跌"], cl = ["","#009944","#00aa44","#00cc44"], se = ["","low","medium","high"];
      events.push({t:"plunge",l:lb[sd],s:se[sd],p:prices[i],d:`${c3.toFixed(1)}%/3min`,c:cl[sd],time:trends[i].time});
    }
    lsd = sd;
    if (i >= 5) {
      const hi = Math.max(...prices.slice(0,i)), lo = Math.min(...prices.slice(0,i));
      if (prices[i] > hi) events.push({t:"new_high",l:"新高",s:"low",p:prices[i],d:`¥${prices[i].toFixed(2)}`,c:"#ff4444",time:trends[i].time});
      if (prices[i] < lo) events.push({t:"new_low",l:"新低",s:"low",p:prices[i],d:`¥${prices[i].toFixed(2)}`,c:"#00cc66",time:trends[i].time});
    }
  }
  return events;
}

const INDEX_HTML = await Deno.readTextFile("./index.html");

async function handleRequest(req) {
  const url = new URL(req.url);
  const path = url.pathname;
  const auth = req.headers.get("Authorization")?.replace("Bearer ", "") || "";
  const user = await getUser(auth);

  // 认证接口
  if (path === "/api/auth/register" && req.method === "POST") {
    const body = await req.json();
    const username = (body.username || "").trim(), password = (body.password || "").trim();
    if (!username || !password) return Response.json({error:"用户名和密码不能为空"}, {status:400});
    if (username.length < 2) return Response.json({error:"用户名至少2个字符"}, {status:400});
    if (password.length < 3) return Response.json({error:"密码至少3位"}, {status:400});
    const existing = await kv.get([USER_PREFIX, username]);
    if (existing.value) return Response.json({error:"用户名已存在"}, {status:400});
    const pwHash = await hash(password);
    await kv.set([USER_PREFIX, username], {password:pwHash, watchlist:[], webhook:""});
    const token = randomToken();
    await kv.set([TOKEN_PREFIX, token], username);
    return Response.json({ok:true, token, username});
  }
  if (path === "/api/auth/login" && req.method === "POST") {
    const body = await req.json();
    const username = (body.username || "").trim(), password = (body.password || "").trim();
    const userData = await kv.get([USER_PREFIX, username]);
    if (!userData.value) return Response.json({error:"用户名或密码错误"}, {status:401});
    const pwHash = await hash(password);
    if (userData.value.password !== pwHash) return Response.json({error:"用户名或密码错误"}, {status:401});
    const token = randomToken();
    await kv.set([TOKEN_PREFIX, token], username);
    return Response.json({ok:true, token, username});
  }
  if (path === "/api/auth/logout" && req.method === "POST") {
    if (auth) await kv.delete([TOKEN_PREFIX, auth]);
    return Response.json({ok:true});
  }

  // 需要登录的接口
  if (!user && path.startsWith("/api/") && !path.startsWith("/api/auth/")) {
    return Response.json({error:"请先登录"}, {status:401});
  }

  if (path === "/api/watchlist/sync" && req.method === "POST") {
    const body = await req.json();
    if (body.watchlist) {
      user.watchlist = body.watchlist;
      await kv.set([USER_PREFIX, user.name], user);
    }
    return Response.json({ok:true, watchlist:user.watchlist||[], role:user.name==="admin"?"admin":"user"});
  }

  if (path === "/api/signals") {
    const codes = url.searchParams.get("codes") || "";
    const codeList = codes.split(",").filter(c => c.trim()).slice(0, 10);
    const results = [];
    for (const raw of codeList) {
      try {
        const info = parseCode(raw);
        const trendData = await fetchTrend(info.code, info.market);
        if (!trendData || !trendData.trends || trendData.trends.length < 10) continue;
        const events = detectEvents(trendData.trends);
        const quote = await fetchQuote(info.sina_code);
        results.push({
          code: info.code, name: quote?.name || info.code,
          last_price: trendData.trends[trendData.trends.length-1].price,
          pre_close: trendData.preClose,
          events: events.slice(-50),
        });
      } catch (e) { continue; }
    }
    return Response.json({signals:results, time:new Date().toISOString().slice(11,19)});
  }

  if (path === "/api/trend") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return Response.json({error:"no code"},{status:400});
    const info = parseCode(raw);
    const data = await fetchTrend(info.code, info.market);
    if (!data) return Response.json({error:"fetch failed"},{status:500});
    return Response.json(data);
  }

  if (path === "/api/quote") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return Response.json({error:"no code"},{status:400});
    const info = parseCode(raw);
    const q = await fetchQuote(info.sina_code);
    if (!q) return Response.json({error:"fetch failed"},{status:500});
    return Response.json({code:info.code,...q});
  }

  // 首页
  return new Response(INDEX_HTML, {headers:{"Content-Type":"text/html; charset=utf-8"}});
}

serve(handleRequest, {port:8000});
