import { Hono } from "https://deno.land/x/hono@v3.11.7/mod.ts";
import { cors } from "https://deno.land/x/hono@v3.11.7/middleware/cors/index.ts";

const app = new Hono();
app.use("/*", cors());

const UA = "Mozilla/5.0";
const users = new Map();
const tokens = new Map();

function sha256(s) {
  const d = new TextEncoder().encode(s);
  return crypto.subtle.digest("SHA-256", d).then(h =>
    Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, "0")).join("")
  );
}
function rnd() { return crypto.randomUUID(); }

app.post("/api/auth/register", async (c) => {
  const { username, password } = await c.req.json();
  if (!username || !password) return c.json({ error: "填写用户名和密码" }, 400);
  if (users.has(username)) return c.json({ error: "用户名已存在" }, 400);
  users.set(username, { password: await sha256(password), watchlist: [] });
  const tk = rnd(); tokens.set(tk, username);
  return c.json({ ok: true, token: tk, username });
});

app.post("/api/auth/login", async (c) => {
  const { username, password } = await c.req.json();
  if (!users.has(username)) return c.json({ error: "用户名或密码错误" }, 401);
  if (users.get(username).password !== await sha256(password)) return c.json({ error: "用户名或密码错误" }, 401);
  const tk = rnd(); tokens.set(tk, username);
  return c.json({ ok: true, token: tk, username });
});

app.get("/api/signals", async (c) => {
  return c.json({ signals: [], time: new Date().toISOString().slice(11, 19) });
});

app.get("/api/trend", (c) => c.json({ error: "not implemented" }, 500));
app.get("/api/quote", (c) => c.json({ error: "not implemented" }, 500));
app.post("/api/watchlist/sync", (c) => c.json({ ok: true, watchlist: [] }));

app.get("/", (c) => c.html(Deno.readTextFileSync("./index.html")));

export default app;
