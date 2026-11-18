export function formatDate(value?: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function formatRelative(value?: string | null): string {
  if (!value) return "Never";
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 10) return "Just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  return formatDate(value);
}

export function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
}

export function formatBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split(/[_\s]+/)
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(" ");
}

const ENTITY_ACRONYMS = new Set(["MRN", "SSN", "URL", "IP", "ID"]);

export function entityLabel(type: string): string {
  return type
    .split("_")
    .map((part) => (ENTITY_ACRONYMS.has(part) ? part : part ? part[0] + part.slice(1).toLowerCase() : part))
    .join(" ");
}

export function maskValue(value: string): string {
  if (value.length <= 2) return "•".repeat(value.length);
  return value[0] + "•".repeat(Math.min(value.length - 2, 10)) + value[value.length - 1];
}

// Uploaded files are kept in memory for the session so the review screen can show the original
// next to the redacted output. They are never stored by the server after processing.
const localOriginals = new Map<string, File>();

export function rememberOriginal(jobId: string, file: File) {
  localOriginals.set(jobId, file);
  if (localOriginals.size > 12) {
    const oldest = localOriginals.keys().next().value;
    if (oldest) localOriginals.delete(oldest);
  }
}

export function recallOriginal(jobId: string): File | undefined {
  return localOriginals.get(jobId);
}
