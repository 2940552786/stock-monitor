// 最简版 - 测试登录
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const users = new Map();
const tokens = new Map();

async function sha256(s) {
  const d = new TextEncoder().encode(s);
  const h = await crypto.subtle.digest("SHA-256", d);
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, "0")).join("");
}

let HTML = "";
try { HTML = Deno.readTextFileSync("./index.html"); } catch { HTML = "<h1>index.html not found</h1>"; }

serve(async (req) => {
  const url = new URL(req.url);
  const p = url.pathname;
  
  try {
    // CORS for all
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
      }});
    }

    if (p === "/api/auth/register" && req.method === "POST") {
      const b = await req.json();
      const u = (b.username || "").trim(), pw = (b.password || "").trim();
      if (!u || !pw) return json({error:"填写用户名和密码"},400);
      if (users.has(u)) return json({error:"用户名已存在"},400);
      users.set(u, {password: await sha256(pw), watchlist:[]});
      const tk = crypto.randomUUID();
      tokens.set(tk, u);
      return json({ok:true, token:tk, username:u});
    }

    if (p === "/api/auth/login" && req.method === "POST") {
      const b = await req.json();
      const u = (b.username || "").trim(), pw = (b.password || "").trim();
      if (!users.has(u)) return json({error:"用户名或密码错误"},401);
      if (users.get(u).password !== await sha256(pw)) return json({error:"用户名或密码错误"},401);
      const tk = crypto.randomUUID();
      tokens.set(tk, u);
      return json({ok:true, token:tk, username:u});
    }

    if (p === "/api/watchlist/sync") return json({ok:true, watchlist:[], role:"user"});
    if (p === "/api/signals") return json({signals:[], time:new Date().toISOString().slice(11,19)});
    if (p === "/api/trend") return json({error:"nyi"},500);
    if (p === "/api/quote") return json({error:"nyi"},500);

    // Serve HTML for all other paths
    return new Response(HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  } catch (e) {
    return json({error: "服务器内部错误: " + (e.message || e)}, 500);
  }
});

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}
