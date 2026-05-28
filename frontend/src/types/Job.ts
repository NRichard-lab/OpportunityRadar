export type WorkType = "Remote" | "Hybrid" | "Onsite" | "Not Listed";
export type RoleType = "IC" | "MGR" | "EXEC" | "UNKNOWN";
export type ApplicationStatus =
  | "Interested"
  | "Applied"
  | "Followed Up"
  | "Interview Scheduled"
  | "Rejected"
  | "Offer"
  | "Archived";

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
  applied: boolean;
  applicationStatus: ApplicationStatus;
  dateApplied: string;
  followUpDate: string;
  notes: string;
  notInterested: boolean;
}
