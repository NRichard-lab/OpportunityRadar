export type WorkType = "Remote" | "Hybrid" | "Onsite" | "Not Listed" | (string & {});
export type RoleType = "IC" | "MGR" | "EXEC" | "UNKNOWN" | (string & {});
export type ApplicationStatus =
  | "Interested"
  | "Applied"
  | "Followed Up"
  | "Interview Scheduled"
  | "Rejected"
  | "Offer"
  | "Archived"
  | (string & {});

export interface Job {
  id: string;
  companyId: string;
  companyName: string;
  title: string;
  location: string;
  workType: WorkType;
  payMin: number | null;
  payMax: number | null;
  payText: string;
  payPeriod: string;
  payCurrency: string;
  postedDate: string;
  sourceUrl: string;
  jobPlatform: string;
  description: string;
  descriptionSnippet: string;
  collectedAt: string;
  status: string;
  roleType: RoleType;
  roleTypeReason: string;
  rawData: Record<string, unknown>;
  matchScore: number | null;
  matchStatus?: "Matched" | "Not Matched" | "Needs Rematch" | "Match Failed";
  matchLabel?: string;
  matchedAt?: string;
  matchAlgorithmVersion?: string;
  matchDetails?: {
    matchedKeywords?: string[];
    missingKeywords?: string[];
    experienceAlignment?: string;
    titleAlignment?: string;
    summary?: string;
  };
  matchError?: string;
  needsRematch?: boolean;
  applied: boolean;
  applicationStatus: ApplicationStatus;
  dateApplied: string;
  followUpDate: string;
  notes: string;
  notInterested: boolean;
}
