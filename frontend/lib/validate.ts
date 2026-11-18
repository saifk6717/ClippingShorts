// Same rules as the backend (stream_manager.normalize), so mistakes are shown before anything is sent.
export function validateStreamUrl(url: string, platform: string): string | null {
  const u = url.trim();
  if (!u) return "Enter a link.";
  if (platform === "synthetic") return u.startsWith("synthetic://") ? null : "Synthetic links start with synthetic://";
  let parsed: URL;
  try {
    parsed = new URL(/^https?:\/\//i.test(u) ? u : `https://${u}`);
  } catch {
    return "That is not a valid link.";
  }
  const host = parsed.hostname.toLowerCase();
  if (platform === "twitch") {
    if (!["twitch.tv", "www.twitch.tv", "m.twitch.tv"].includes(host)) return "This is not a Twitch link.";
    if (!/^\/[A-Za-z0-9_]{2,25}\/?$/.test(parsed.pathname)) return "Use a channel link: twitch.tv/<channel>";
    return null;
  }
  if (platform === "youtube") {
    if (!(host === "youtube.com" || host === "youtu.be" || host.endsWith(".youtube.com"))) return "This is not a YouTube link.";
    const ok = parsed.searchParams.get("v") || /^\/(live|shorts)\/[\w-]{11}/.test(parsed.pathname) || host === "youtu.be"
      || /^\/(@[^/]+|channel\/[^/]+|c\/[^/]+)(\/live)?\/?$/.test(parsed.pathname);
    return ok ? null : "Use a watch?v=…, /live/<id> or channel (@name) link.";
  }
  return "Choose a platform.";
}

export function guessPlatform(url: string): "twitch" | "youtube" | null {
  if (/twitch\.tv/i.test(url)) return "twitch";
  if (/youtu\.?be/i.test(url)) return "youtube";
  return null;
}

export function normalizeKey(url: string): string {
  return url.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/+$/, "");
}
