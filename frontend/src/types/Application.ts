import type { ApplicationStatus } from "./Job";

export interface Application {
  id: string;
  jobId: string;
  companyId: string;
  companyName: string;
  jobTitle: string;
  dateApplied: string;
  status: ApplicationStatus;
  followUpDate: string;
  resumeVersion: string;
  notes: string;
}
