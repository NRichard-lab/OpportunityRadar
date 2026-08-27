import type { Job } from "../types/Job";

const rejectedTitles = new Set([
  "remote work", "skip to content", "careers", "search open positions", "search jobs",
  "view open positions", "apply now", "join our team", "home", "menu", "privacy",
  "terms", "accessibility", "login", "sign in", "benefits", "culture", "locations",
  "equal opportunity", "talent community", "view details",
  "view details (opens an external site)", "opens an external site", "learn more", "apply",
]);

const rejectedTitleParts = [
  "view details", "opens an external site", "apply now", "search jobs", "careers",
  "remote work", "benefits", "culture", "locations", "talent community",
];

export function isValidJobRecord(job: Job): boolean {
  const title = (job.title || "").trim().toLowerCase();
  if (!job.id || !job.title || !job.sourceUrl || title.length < 4) return false;
  if (rejectedTitles.has(title)) return false;
  return !rejectedTitleParts.some((part) => title.includes(part));
}

export function isCurrentJobRecord(job: Job): boolean {
  return isValidJobRecord(job) && (job.status || "").trim().toLowerCase() === "open";
}

export function newestJobFirst(left: Job, right: Job): number {
  const rightDate = parseJobDate(right.postedDate) || parseJobDate(right.collectedAt);
  const leftDate = parseJobDate(left.postedDate) || parseJobDate(left.collectedAt);
  return rightDate - leftDate || left.companyName.localeCompare(right.companyName) || left.title.localeCompare(right.title);
}

function parseJobDate(value: string): number {
  if (!value) return 0;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}
