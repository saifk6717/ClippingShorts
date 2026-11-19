export type Platform = "twitch" | "youtube" | "synthetic";

export interface LiveState {
  status?: string;
  error?: string;
  session?: string;
  title?: string;
  t?: number;
  started_at?: number;
  live_since?: number;
  reconnects?: number;
  updated?: number;
  audio?: { db?: number; peak_db?: number; z?: number; jump_pct?: number; baseline_db?: number | null; voiced?: number; excitement?: number; levels?: number[] };
  chat?: { msgs_per_sec?: number; unique_users?: number; unique_60s?: number; unique_per_sec?: number; baseline_msgs_per_sec?: number; connected?: boolean; error?: string; total?: number; top_user_share?: number; reaction_ratio?: number };
  transcription?: { provider?: string; done_until?: number; latency?: number; ok?: boolean; error?: string; sent?: number; dropped?: number; lag?: number };
  detector?: { last_spike?: { t: number; score: number; at: number } | null; scores?: number[]; candidates?: number; open?: number };
  buffer?: { segments?: number; seconds?: number; bytes?: number; pins?: number };
  worker?: { rss_mb?: number; cpu_pct?: number; procs?: number; pid?: number; uptime?: number };
}

export interface Stream {
  id: string;
  url: string;
  platform: Platform;
  channel: string;
  creator_name: string;
  enabled: boolean;
  desired_state: "running" | "stopped";
  status: string;
  settings: Record<string, number | string>;
  last_error: string;
  created_at: number;
  updated_at: number;
  live: LiveState;
  highlight_count?: number;
  recent_highlights?: Highlight[];
}

export interface Highlight {
  id: string;
  stream_id: string;
  detected_at: number;
  event_t: number;
  trigger: string;
  local_score: number;
  transcript_score: number;
  reaction_score: number;
  confidence: number;
  category: string;
  title: string;
  reason: string;
  status: string;
}

export interface EditParams {
  start: number;
  end: number;
  aspect: "9:16" | "16:9" | "1:1";
  fit: "crop" | "blur";
  crop_x: number | null;
  crop_y: number | null;
  zoom: number;
  smart_crop: boolean;
  quality: "high" | "fast";
  captions: { enabled: boolean; style: "bounce" | "hype" | "clean"; position: number; font_scale: number; words: Word[] | null; highlight_words?: string[] };
  hook: { enabled: boolean; text: string; position: number };
  audio: { volume_db: number; normalize: boolean; mute: boolean };
  title: string;
  description: string;
  hashtags: string[];
}

export interface Edit {
  id: string;
  clip_id: string;
  version: number;
  created_at: number;
  params: EditParams & { resolved_crop_x?: number };
  status: "draft" | "queued" | "deferred" | "rendering" | "done" | "failed";
  progress: number;
  duration: number;
  render_seconds: number;
  error: string;
  file_url: string | null;
  thumbnail_url: string | null;
}

export interface Upload {
  id: string;
  edit_id: string;
  clip_id: string;
  channel_id: string;
  title: string;
  privacy: string;
  publish_at: number;
  status: "queued" | "uploading" | "done" | "failed";
  progress: number;
  youtube_video_id: string;
  attempts: number;
  error: string;
  error_short?: string;
  created_at: number;
}

export interface Clip {
  id: string;
  stream_id: string | null;
  highlight_id: string;
  platform: Platform;
  creator_name: string;
  source_url: string;
  created_at: number;
  detected_at: number;
  duration: number;
  width: number;
  height: number;
  suggested_start: number;
  suggested_end: number;
  category: string;
  confidence: number;
  title: string;
  hook: string;
  description: string;
  hashtags: string[];
  reason: string;
  status: string;
  review_status: "pending" | "approved" | "rejected";
  words_status: string;
  keep: boolean;
  expires_at: number;
  thumbnail_url: string | null;
  preview_url: string | null;
  render_status: string;
  short_url: string | null;
  short_thumbnail_url: string | null;
  youtube_url: string | null;
  upload_status: string;
  failed_upload_id: string | null;
  upload_error: string;
  edits_count: number;
  edits?: Edit[];
  uploads?: Upload[];
  default_params?: EditParams;
  sprite?: { every: number; cols: number; tile_w: number; rows: number; sheet_h: number } | null;
  sprite_url?: string | null;
  highlight?: Highlight | null;
}

export interface Word {
  w: string;
  s: number;
  e: number;
}

export interface Paged<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}
