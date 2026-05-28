import type { Job } from "../types/Job";
import type { ResumeProfile } from "../types/ResumeProfile";

export type MatchRecommendation = "Strong Apply" | "Good Match" | "Stretch Role" | "Poor Fit";

export interface MatchResult {
  score: number;
  matchedKeywords: string[];
  missingKeywords: string[];
  experienceAlignment: string;
  titleAlignment: string;
  summary: string;
  recommendation: MatchRecommendation;
}

const trackedKeywords = [
  "systems",
  "administrator",
  "engineer",
  "manager",
  "analyst",
  "core",
  "banking",
  "credit union",
  "infrastructure",
  "security",
  "windows",
  "server",
  "networking",
  "microsoft 365",
  "sql",
  "vendor",
  "compliance",
  "risk",
  "virtualization",
  "help desk",
  "symitar",
  "jack henry",
  "fiserv",
];

const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9+.#\s-]/g, " ");

export function extractResumeTextFromFileName(fileName: string): string {
  return fileName.replace(/\.[^.]+$/, "").replace(/[-_]/g, " ");
}

export function extractKeywords(text: string): string[] {
  const normalized = normalize(text);
  const found = trackedKeywords.filter((keyword) => normalized.includes(keyword));
  const words = normalized
    .split(/\s+/)
    .filter((word) => word.length > 4 && !["experience", "required", "preferred"].includes(word));
  return Array.from(new Set([...found, ...words.slice(0, 18)]));
}

export function getRecommendation(score: number): MatchRecommendation {
  if (score >= 80) return "Strong Apply";
  if (score >= 60) return "Good Match";
  if (score >= 40) return "Stretch Role";
  return "Poor Fit";
}

export function compareResumeToJob(resume: ResumeProfile | null, job: Job): MatchResult {
  const resumeText = normalize(resume?.rawText || "");
  const titleText = normalize(job.title);
  const jobKeywords = extractKeywords(`${job.title} ${job.description}`);
  const matchedKeywords = jobKeywords.filter((keyword) => resumeText.includes(normalize(keyword)));
  const missingKeywords = jobKeywords.filter((keyword) => !resumeText.includes(normalize(keyword))).slice(0, 10);
  const titleTerms = titleText.split(/\s+/).filter((term) => term.length > 3);
  const matchedTitleTerms = titleTerms.filter((term) => resumeText.includes(term));

  const keywordScore = jobKeywords.length ? (matchedKeywords.length / jobKeywords.length) * 52 : 0;
  const titleScore = titleTerms.length ? (matchedTitleTerms.length / titleTerms.length) * 20 : 0;
  const bankingScore = ["banking", "credit union", "core"].some((term) => resumeText.includes(term)) ? 12 : 0;
  const experienceScore = /\b(3|4|5|6|7|8|9|10)\+?\s+years?\b/.test(resumeText) ? 8 : 0;
  const locationScore = job.workType === "Remote" || resumeText.includes("remote") ? 8 : 4;
  const score = Math.min(100, Math.round(keywordScore + titleScore + bankingScore + experienceScore + locationScore));
  const recommendation = getRecommendation(score);

  return {
    score,
    matchedKeywords,
    missingKeywords,
    experienceAlignment: experienceScore ? "Experience terms detected in resume." : "Experience depth needs manual review.",
    titleAlignment: matchedTitleTerms.length
      ? `${matchedTitleTerms.length} title keyword(s) overlap.`
      : "No direct title keyword overlap detected.",
    summary:
      "This score estimates how well the resume appears to match visible job requirements, title alignment, skills, experience keywords, and banking or credit union relevance.",
    recommendation,
  };
}
