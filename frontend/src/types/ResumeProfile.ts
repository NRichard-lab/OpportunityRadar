export interface ResumeProfile {
  id: string;
  fileName: string;
  uploadedAt: string;
  rawText: string;
  extractedText?: string;
  version?: string;
  skills: string[];
  titles: string[];
  yearsExperienceSummary: string;
  notes: string;
}
