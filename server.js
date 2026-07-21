const users = new Map();
const tokens = new Map();

async function sha256(s) {
  const d = new TextEncoder().encode(s);
  const h = await crypto.subtle.digest("SHA-256", d);
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, "0")).join("");
}

let HTML = "";
try { HTML = Deno.readTextFileSync("./index.html"); } catch { HTML = "<h1>index.html missing</h1>"; }

export default {
  async fetch(req) {
    const url = new URL(req.url);
    const p = url.pathname;
    const hdrs = { "Content-Type": "application/json" };
    const json = (data, s) => new Response(JSON.stringify(data), { status: s || 200, headers: hdrs });
    const html = () => new Response(HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });

    if (p === "/api/auth/register" && req.method === "POST") {
      try {
        const b = await req.json();
        const u = (b.username || "").trim(), pw = (b.password || "").trim();
        if (!u || !pw) return json({ error: "填写用户名和密码" }, 400);
        if (users.has(u)) return json({ error: "用户名已存在" }, 400);
        users.set(u, { password: await sha256(pw), watchlist: [] });
        const tk = crypto.randomUUID(); tokens.set(tk, u);
        return json({ ok: true, token: tk, username: u });
      } catch (e) { return json({ error: "请求格式错误" }, 400); }
    }

    if (p === "/api/auth/login" && req.method === "POST") {
      try {
        const b = await req.json();
        const u = (b.username || "").trim(), pw = (b.password || "").trim();
        if (!users.has(u)) return json({ error: "用户名或密码错误" }, 401);
        if (users.get(u).password !== await sha256(pw)) return json({ error: "用户名或密码错误" }, 401);
        const tk = crypto.randomUUID(); tokens.set(tk, u);
        return json({ ok: true, token: tk, username: u });
      } catch (e) { return json({ error: "请求格式错误" }, 400); }
    }

    if (p === "/api/watchlist/sync") return json({ ok: true, watchlist: [], role: "user" });
    if (p === "/api/signals") return json({ signals: [], time: new Date().toISOString().slice(11, 19) });
    if (p === "/api/trend") return json({ error: "nyi" }, 500);
    if (p === "/api/quote") return json({ error: "nyi" }, 500);

    return html();
  }
};
