export type MaintenanceStatus = "Queued" | "Running" | "Cancelling" | "Completed" | "Cancelled" | "Failed" | "Skipped";

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
  taskName: string;
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
  error: string;
  startedAt: string;
  completedAt: string;
  runtimeSeconds: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface MaintenanceJobState {
  jobKey: string;
  taskName: string;
  description: string;
  supportsScheduling: boolean;
  schedule: MaintenanceSchedule | null;
  running: boolean;
  activeRunId: string | null;
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
}

export const emptyMaintenanceState: MaintenanceJobsState = {
  jobs: [],
  activeRuns: [],
  runningCount: 0,
};
