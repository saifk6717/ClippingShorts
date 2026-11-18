// Streams every /api request to the backend container. Bodies are passed through as streams (video Range
// responses and Server-Sent Events are never buffered), cookies and Range headers are forwarded both ways.
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND = process.env.BACKEND_URL || "http://backend:8000";
const FORWARD_REQ = ["cookie", "range", "content-type", "x-clippingshorts", "authorization", "accept", "if-range", "user-agent"];
const DROP_RES = new Set(["connection", "keep-alive", "transfer-encoding", "content-encoding", "set-cookie"]);

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${BACKEND}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of FORWARD_REQ) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const fwd = req.headers.get("x-forwarded-for");
  headers.set("x-forwarded-for", fwd || "unknown");
  const hasBody = !["GET", "HEAD"].includes(req.method);
  let res: Response;
  try {
    res = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // @ts-expect-error duplex is required by Node's fetch for streaming request bodies
      duplex: hasBody ? "half" : undefined,
      redirect: "manual",
      signal: req.signal,
      cache: "no-store",
    });
  } catch {
    return Response.json({ detail: "The ClippingShorts backend is not reachable." }, { status: 502 });
  }
  const out = new Headers();
  res.headers.forEach((v, k) => {
    if (!DROP_RES.has(k.toLowerCase())) out.set(k, v);
  });
  for (const c of res.headers.getSetCookie()) out.append("set-cookie", c);
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE, proxy as HEAD };
