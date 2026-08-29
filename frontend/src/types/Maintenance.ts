export type MaintenanceStatus = "Queued" | "Running" | "Cancelling" | "Completed" | "Cancelled" | "Failed" | "Skipped";

export const COMPANY_INFO_REFRESH_ACTION = "refresh-missing-company-information" as const;

export type CompanyInfoRefreshOutcome = "updated" | "unchanged" | "no_information_found" | "failed";

export interface CompanyInfoRefreshResult {
  companyId: string;
  companyName: string;
  outcome: CompanyInfoRefreshOutcome;
  foundFields: string[];
  updatedFields: string[];
  message: string;
}

export interface CompanyInfoRefreshSummary {
  totalCompaniesNeedingReview: number;
  processedCount: number;
  updatedCount: number;
  noInformationFoundCount: number;
  failedCount: number;
  unchangedCount: number;
  duplicateRecordsSkipped: number;
  companyResults: CompanyInfoRefreshResult[];
}

export interface MaintenanceSchedule {
  jobKey: string;
  enabled: boolean;
  frequency: "daily";
  runTime: string;
  timezone: string;
  lastScheduledDate: string;
  updatedAt: string;
}

export interface MaintenanceRun {
  id: string;
  run_id: string;
  action: string;
  job_key: string;
  triggerType: "manual" | "scheduled";
  trigger_type: "manual" | "scheduled";
  taskName: string;
  task_name: string;
  progressVerb: string;
  progressUnit: string;
  status: MaintenanceStatus;
  running: boolean;
  current: number;
  total: number;
  progress: number | null;
  progressText: string;
  currentCompany: string;
  currentMessage: string;
  message: string;
  summary: Record<string, unknown>;
  resultSummary: Record<string, unknown>;
  error: string;
  startedAt: string;
  completedAt: string;
  runtimeSeconds: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface MaintenanceJobState {
  jobKey: string;
  job_key: string;
  taskName: string;
  description: string;
  supportsScheduling: boolean;
  schedule: MaintenanceSchedule | null;
  running: boolean;
  activeRunId: string | null;
  active_run_id: string | null;
  activeRun: MaintenanceRun | null;
  latestRun: MaintenanceRun | null;
  lastRun: MaintenanceRun | null;
  lastRuntimeSeconds: number | null;
  averageRuntimeSeconds: number | null;
  lastResult: "Success" | "Failed" | "Running" | "Never Run";
}

export interface MaintenanceJobsState {
  jobs: MaintenanceJobState[];
  activeRuns: MaintenanceRun[];
  runningCount: number;
  running_count: number;
}

export const emptyMaintenanceState: MaintenanceJobsState = {
  jobs: [],
  activeRuns: [],
  runningCount: 0,
  running_count: 0,
};
