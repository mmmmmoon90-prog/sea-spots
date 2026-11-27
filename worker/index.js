// 바다낚시·차박 지도 — 지역 대항 랭킹 서버
// POST /checkin {sido, spot, kind:"visit"|"catch"}  → 점수 반영
// GET  /rank                                         → 오늘·누적 랭킹
import { DurableObject } from "cloudflare:workers";

const SEA = {
  강원: "동해", 경북: "동해", 울산: "동해",
  부산: "남해", 경남: "남해", 전남: "남해",
  인천: "서해", 경기: "서해", 충남: "서해", 전북: "서해",
  제주: "제주", 충북: "민물",
};
const POINT = { visit: 1, catch: 3 };
const DAILY_MAX = 10; // 한 사람이 하루에 남길 수 있는 횟수
const ORIGINS = ["https://mmmmmoon90-prog.github.io", "https://recheegoo.s.gy"];

function kstDay(d = new Date()) {
  return new Date(d.getTime() + 9 * 3600e3).toISOString().slice(0, 10);
}

function cors(req) {
  const o = req.headers.get("Origin") || "";
  return {
    "Access-Control-Allow-Origin": ORIGINS.includes(o) ? o : ORIGINS[0],
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function json(req, body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...cors(req) },
  });
}

async function hashWho(req, salt) {
  const ip = req.headers.get("CF-Connecting-IP") || "";
  const ua = req.headers.get("User-Agent") || "";
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(salt + "|" + ip + "|" + ua));
  return [...new Uint8Array(buf)].slice(0, 12).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class Board extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS pts (day TEXT, sido TEXT, kind TEXT, n INTEGER, PRIMARY KEY (day, sido, kind));
      CREATE TABLE IF NOT EXISTS seen (day TEXT, who TEXT, spot TEXT, kind TEXT, PRIMARY KEY (day, who, spot, kind));
    `);
  }

  checkin(day, who, sido, spot, kind) {
    const used = this.sql.exec("SELECT COUNT(*) AS c FROM seen WHERE day=? AND who=?", day, who).one().c;
    if (used >= DAILY_MAX) return { ok: false, reason: "limit" };
    const dup = this.sql.exec("SELECT 1 FROM seen WHERE day=? AND who=? AND spot=? AND kind=?", day, who, spot, kind).toArray();
    if (dup.length) return { ok: false, reason: "dup" };
    this.sql.exec("INSERT INTO seen VALUES (?,?,?,?)", day, who, spot, kind);
    this.sql.exec(
      "INSERT INTO pts VALUES (?,?,?,1) ON CONFLICT(day, sido, kind) DO UPDATE SET n = n + 1",
      day, sido, kind,
    );
    // 오래된 중복 방지 기록은 지운다
    this.sql.exec("DELETE FROM seen WHERE day < ?", kstDay(new Date(Date.now() - 2 * 864e5)));
    return { ok: true, left: DAILY_MAX - used - 1 };
  }

  rank(day) {
    const agg = (rows) => {
      const by = {};
      for (const r of rows) {
        const b = (by[r.sido] ||= { sido: r.sido, sea: SEA[r.sido] || "", visit: 0, catch: 0, score: 0 });
        b[r.kind] += r.n;
        b.score += r.n * (POINT[r.kind] || 0);
      }
      const regions = Object.values(by).sort((a, b) => b.score - a.score);
      const seas = {};
      for (const r of regions) {
        const s = (seas[r.sea] ||= { sea: r.sea, visit: 0, catch: 0, score: 0 });
        s.visit += r.visit; s.catch += r.catch; s.score += r.score;
      }
      return { regions, seas: Object.values(seas).sort((a, b) => b.score - a.score) };
    };
    const today = agg(this.sql.exec("SELECT sido, kind, n FROM pts WHERE day=?", day).toArray());
    const all = agg(this.sql.exec("SELECT sido, kind, SUM(n) AS n FROM pts GROUP BY sido, kind").toArray());
    return { day, today, all };
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: cors(req) });
    const board = env.BOARD.get(env.BOARD.idFromName("main"));
    const day = kstDay();

    if (url.pathname === "/rank" && req.method === "GET") {
      const r = await board.rank(day);
      const res = json(req, r);
      res.headers.set("Cache-Control", "public, max-age=30");
      return res;
    }

    if (url.pathname === "/checkin" && req.method === "POST") {
      let b;
      try { b = await req.json(); } catch { return json(req, { ok: false, reason: "bad" }, 400); }
      const sido = String(b.sido || "");
      const kind = String(b.kind || "");
      const spot = String(b.spot || "").slice(0, 40);
      if (!SEA[sido] || !POINT[kind] || !spot) return json(req, { ok: false, reason: "bad" }, 400);
      const who = await hashWho(req, day);
      const r = await board.checkin(day, who, sido, spot, kind);
      return json(req, r, r.ok ? 200 : 429);
    }

    if (url.pathname === "/") return json(req, { ok: true, name: "sea-spots rank" });
    return json(req, { ok: false }, 404);
  },
};
