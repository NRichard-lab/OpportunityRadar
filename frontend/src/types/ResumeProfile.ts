export interface ResumeProfile {
  id: string;
  fileName: string;
  uploadedAt: string;
  rawText?: string;
  extractedText?: string;
  version: string;
  skills: string[];
  titles: string[];
  yearsExperienceSummary: string;
  notes: string;
}

export function withoutResumeText(profile: ResumeProfile): ResumeProfile {
  return {
    id: profile.id,
    version: profile.version,
    fileName: profile.fileName,
    uploadedAt: profile.uploadedAt,
    skills: [...profile.skills],
    titles: [...profile.titles],
    yearsExperienceSummary: profile.yearsExperienceSummary,
    notes: profile.notes,
  };
}

export function isResumeProfile(value: unknown): value is ResumeProfile {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const profile = value as Record<string, unknown>;
  return typeof profile.id === "string"
    && typeof profile.fileName === "string"
    && typeof profile.uploadedAt === "string"
    && typeof profile.version === "string"
    && Array.isArray(profile.skills)
    && profile.skills.every((item) => typeof item === "string")
    && Array.isArray(profile.titles)
    && profile.titles.every((item) => typeof item === "string")
    && typeof profile.yearsExperienceSummary === "string"
    && typeof profile.notes === "string"
    && (profile.rawText === undefined || typeof profile.rawText === "string")
    && (profile.extractedText === undefined || typeof profile.extractedText === "string");
}
