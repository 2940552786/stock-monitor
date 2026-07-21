// A股分时图看盘 - Deno Deploy 版
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const UA = "Mozilla/5.0";

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
  return { code, preClose, trends };
}

async function fetchQuote(sinaCode) {
  const url = `http://hq.sinajs.cn/list=${sinaCode}`;
  const resp = await fetch(url, { headers: { "User-Agent": UA, "Referer": "https://finance.sina.com.cn/" } });
  const text = await resp.text();
  const match = text.match(/"([^"]*)"/);
  if (!match) return null;
  const fields = match[1].split(",");
  if (fields.length < 32) return null;
  return {
    name: fields[0],
    price: parseFloat(fields[3]) || 0,
    preClose: parseFloat(fields[2]) || 0,
  };
}

function detectEvents(trends) {
  const prices = trends.map(t => t.price);
  const volumes = trends.map(t => t.volume);
  const n = prices.length;
  const events = [];

  const chg = (i, nmin) => {
    if (i < nmin) return 0;
    return (prices[i] - prices[i - nmin]) / prices[i - nmin] * 100;
  };

  let lastSvUp = 0, lastSvDn = 0;
  for (let i = 1; i < n; i++) {
    const cp = prices[i], pp = prices[i - 1];
    const c3 = chg(i, 3);
    const svUp = c3 >= 1.8 ? 3 : (c3 >= 1.2 ? 2 : (c3 >= 0.8 ? 1 : 0));
    const svDn = c3 <= -1.8 ? 3 : (c3 <= -1.2 ? 2 : (c3 <= -0.8 ? 1 : 0));

    if (svUp > lastSvUp) {
      const labels = ["", "轻度急拉", "中度急拉", "强烈急拉"];
      const colors = ["", "#ff9933", "#ff7733", "#ff3333"];
      const sevs = ["", "low", "medium", "high"];
      events.push({ t: "surge", l: labels[svUp], s: sevs[svUp], p: cp, d: `+${c3.toFixed(1)}%/3min`, c: colors[svUp], idx: i, time: trends[i].time });
    }
    lastSvUp = svUp;

    if (svDn > lastSvDn) {
      const labels = ["", "轻度急跌", "中度急跌", "强烈急跌"];
      const colors = ["", "#009944", "#00aa44", "#00cc44"];
      const sevs = ["", "low", "medium", "high"];
      events.push({ t: "plunge", l: labels[svDn], s: sevs[svDn], p: cp, d: `${c3.toFixed(1)}%/3min`, c: colors[svDn], idx: i, time: trends[i].time });
    }
    lastSvDn = svDn;

    if (i >= 5) {
      const hi = Math.max(...prices.slice(0, i));
      const lo = Math.min(...prices.slice(0, i));
      if (cp > hi) events.push({ t: "new_high", l: "新高", s: "low", p: cp, d: `¥${cp.toFixed(2)}`, c: "#ff4444", idx: i, time: trends[i].time });
      if (cp < lo) events.push({ t: "new_low", l: "新低", s: "low", p: cp, d: `¥${cp.toFixed(2)}`, c: "#00cc66", idx: i, time: trends[i].time });
    }
  }
  return events;
}

async function handleRequest(req) {
  const url = new URL(req.url);
  const path = url.pathname;

  // API: 批量信号检测
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
          code: info.code,
          name: quote?.name || info.code,
          last_price: trendData.trends[trendData.trends.length - 1].price,
          pre_close: trendData.preClose,
          events: events.slice(-50),
        });
      } catch (e) { continue; }
    }
    return new Response(JSON.stringify({ signals: results, time: new Date().toISOString().slice(11, 19) }), {
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  }

  // API: 分时数据
  if (path === "/api/trend") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return new Response(JSON.stringify({ error: "no code" }), { status: 400 });
    const info = parseCode(raw);
    const data = await fetchTrend(info.code, info.market);
    if (!data) return new Response(JSON.stringify({ error: "fetch failed" }), { status: 500 });
    return new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  }

  // API: 实时报价
  if (path === "/api/quote") {
    const raw = url.searchParams.get("code") || "";
    if (!raw) return new Response(JSON.stringify({ error: "no code" }), { status: 400 });
    const info = parseCode(raw);
    const quote = await fetchQuote(info.sina_code);
    if (!quote) return new Response(JSON.stringify({ error: "fetch failed" }), { status: 500 });
    return new Response(JSON.stringify({ code: info.code, ...quote }), {
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  }

  // 静态文件
  try {
    const fileUrl = new URL("../index.html", import.meta.url);
    const html = await Deno.readTextFile("./index.html");
    // 用内联的 index.html
    return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  } catch {
    return new Response("OK", { headers: { "Content-Type": "text/html" } });
  }
}

serve(handleRequest, { port: 8000 });
