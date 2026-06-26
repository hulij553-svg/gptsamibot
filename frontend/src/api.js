const API_BASE = (import.meta.env.VITE_API_URL || window.location.origin).replace(/\/$/, "");

export function getInitData() {
  const tg = window.Telegram?.WebApp;
  return tg?.initData || import.meta.env.VITE_DEV_INIT_DATA || "dev";
}

export function isTelegramReady() {
  return Boolean(window.Telegram?.WebApp?.initData || import.meta.env.VITE_DEV_INIT_DATA || "dev");
}

export function absoluteUrl(url) {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return url;
  return `${API_BASE}${url.startsWith("/") ? url : `/${url}`}`;
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return { detail: await response.text() };
}

export function errorMessage(value, fallback = "Request failed") {
  if (!value) return fallback;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const lines = value.map((item) => errorMessage(item, "")).filter(Boolean);
    return lines.length ? lines.join("\n") : fallback;
  }
  if (typeof value === "object") {
    if (typeof value.detail === "string") return value.detail;
    if (value.detail) return errorMessage(value.detail, fallback);
    if (typeof value.message === "string") return value.message;
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function throwResponseError(payload, statusText) {
  throw new Error(errorMessage(payload?.detail ?? payload, statusText));
}

async function postForm(endpoint, extra = {}) {
  const form = new FormData();
  form.append("init_data", getInitData());
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== "") form.append(key, value);
  }

  const response = await fetch(`${API_BASE}${endpoint}`, { method: "POST", body: form });
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchConfig() {
  const response = await fetch(`${API_BASE}/api/config`);
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchBalance() {
  return postForm("/api/balance");
}

export async function estimate({ aspect, size_tier, quality, n }) {
  return postForm("/api/estimate", { aspect, size_tier, quality, n });
}

export async function submitGeneration({ prompt, aspect, size_tier, quality, output_format, n, references }) {
  const form = new FormData();
  form.append("init_data", getInitData());
  form.append("prompt", prompt);
  form.append("aspect", aspect);
  form.append("size_tier", size_tier);
  form.append("quality", quality);
  form.append("output_format", output_format);
  form.append("n", n);
  if (references && references.length) {
    for (const file of references) form.append("references", file);
  }

  const response = await fetch(`${API_BASE}/api/generate`, { method: "POST", body: form });
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchJob(jobId) {
  return postForm(`/api/jobs/${jobId}`);
}

export async function cancelJob(jobId) {
  return postForm(`/api/jobs/${jobId}/cancel`);
}

export async function fetchHistory(limit = 20) {
  return postForm("/api/history", { limit });
}

export async function setApiKey(apiKey) {
  return postForm("/api/setkey", { api_key: apiKey });
}

export function connectWebSocket(onMessage) {
  const initData = getInitData();

  const protocol = API_BASE.startsWith("https") ? "wss" : "ws";
  const host = API_BASE.replace(/^https?:\/\//, "");
  const websocket = new WebSocket(`${protocol}://${host}/ws?init_data=${encodeURIComponent(initData)}`);
  websocket.onmessage = (event) => onMessage(JSON.parse(event.data));
  websocket.onerror = (event) => console.error("WebSocket error", event);
  return websocket;
}

export async function downloadUrl(url, filename = "image.png") {
  const response = await fetch(absoluteUrl(url));
  if (!response.ok) {
    throw new Error(`Download failed: ${response.status} ${response.statusText}`);
  }
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}
