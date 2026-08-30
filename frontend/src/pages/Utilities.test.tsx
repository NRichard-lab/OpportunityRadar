import { renderToStaticMarkup } from "react-dom/server";
import { Building2 } from "lucide-react";
import { describe, expect, it } from "vitest";

import { isCompanyInfoRefreshSummary } from "../runtimeSchemas";
import {
  COMPANY_INFO_REFRESH_ACTION,
  type CompanyInfoRefreshSummary,
  type MaintenanceJobState,
  type MaintenanceRun,
} from "../types/Maintenance";
import {
  CompanyInfoFinalReport,
  CompanyInfoLogModal,
  filterCompanyInfoResults,
  HistoryModal,
  MaintenanceCard,
  ProgressPanel,
  Utilities,
} from "./Utilities";

const companySummary: CompanyInfoRefreshSummary = {
  totalCompaniesNeedingReview: 3,
  processedCount: 3,
  updatedCount: 1,
  noInformationFoundCount: 1,
  failedCount: 1,
  unchangedCount: 0,
  duplicateRecordsSkipped: 2,
  companyResults: [
    {
      companyId: "company-updated",
      companyName: "Updated Bank",
      outcome: "updated",
      foundFields: ["officialWebsite", "careersPageUrl"],
      updatedFields: ["careersPageUrl"],
      message: "Found the official website and saved its confirmed careers page.",
    },
    {
      companyId: "company-empty",
      companyName: "Quiet Credit Union",
      outcome: "no_information_found",
      foundFields: [],
      updatedFields: [],
      message: "No additional official company information was confirmed.",
    },
    {
      companyId: "company-failed",
      companyName: "Unavailable Financial",
      outcome: "failed",
      foundFields: [],
      updatedFields: [],
      message: "The official website timed out; existing information was retained.",
    },
  ],
};

function maintenanceRun(overrides: Partial<MaintenanceRun> = {}): MaintenanceRun {
  return {
    id: "maintenance-company-info",
    run_id: "maintenance-company-info",
    action: COMPANY_INFO_REFRESH_ACTION,
    job_key: COMPANY_INFO_REFRESH_ACTION,
    triggerType: "manual",
    trigger_type: "manual",
    taskName: "Refresh Missing Company Information",
    task_name: "Refresh Missing Company Information",
    progressVerb: "Checking",
    progressUnit: "companies",
    status: "Running",
    running: true,
    current: 3,
    total: 3,
    progress: 100,
    progressText: "Checking 3 of 3 companies",
    currentCompany: "Unavailable Financial",
    currentMessage: "Checking 3 of 3 companies",
    message: "Checking 3 of 3 companies",
    summary: { ...companySummary },
    resultSummary: { ...companySummary },
    error: "",
    startedAt: "2026-08-28T12:00:00-06:00",
    completedAt: "",
    runtimeSeconds: 12,
    createdAt: "2026-08-28T12:00:00-06:00",
    updatedAt: "2026-08-28T12:00:12-06:00",
    ...overrides,
  };
}

function maintenanceJob(run: MaintenanceRun | null = null): MaintenanceJobState {
  return {
    jobKey: COMPANY_INFO_REFRESH_ACTION,
    job_key: COMPANY_INFO_REFRESH_ACTION,
    taskName: "Refresh Missing Company Information",
    description: "Finds missing public company details.",
    supportsScheduling: false,
    schedule: null,
    running: Boolean(run?.running),
    activeRunId: run?.running ? run.id : null,
    active_run_id: run?.running ? run.id : null,
    activeRun: run?.running ? run : null,
    latestRun: run,
    lastRun: run,
    lastRuntimeSeconds: run?.runtimeSeconds ?? null,
    averageRuntimeSeconds: null,
    lastResult: run?.running ? "Running" : run?.status === "Completed" ? "Success" : "Never Run",
  };
}

function text(markup: string): string {
  return markup.replace(/<[^>]+>/g, " ").replace(/&[^;]+;/g, " ").replace(/\s+/g, " ").trim();
}

describe("Refresh Missing Company Info utility", () => {
  it("uses the exact simple action label", () => {
    const markup = renderToStaticMarkup(<MaintenanceCard
      action={{ key: COMPANY_INFO_REFRESH_ACTION, confirmation: "Confirm", icon: Building2 }}
      job={maintenanceJob()}
      enabled
      schedulesEnabled={false}
      onRun={() => undefined}
      onToggle={() => undefined}
      onEditSchedule={() => undefined}
      onHistory={() => undefined}
    />);

    expect(text(markup)).toContain("Refresh Missing Company Info");
    expect(text(markup)).not.toContain("Run Now");
  });

  it("shows active counters, current company, and the latest company result", () => {
    const markup = renderToStaticMarkup(<ProgressPanel run={maintenanceRun()} onCancel={async () => undefined} />);
    const content = text(markup);

    expect(content).toContain("Total needing review 3");
    expect(content).toContain("Processed 3");
    expect(content).toContain("Updated 1");
    expect(content).toContain("No information found 1");
    expect(content).toContain("Failed 1");
    expect(content).toContain("Current company: Unavailable Financial");
    expect(content).toContain("Latest company result");
    expect(content).toContain("Found the official website and saved its confirmed careers page.");
    expect(content).toContain("No additional official company information was confirmed.");
    expect(content).toContain("The official website timed out; existing information was retained.");
    expect(content).toContain("Cancel");
    expect(text(renderToStaticMarkup(<ProgressPanel run={maintenanceRun()} cancelling onCancel={async () => undefined} />))).toContain("Cancelling...");
  });

  it("keeps the completed summary visible without rendering company results inline", () => {
    const completed = maintenanceRun({
      status: "Completed",
      running: false,
      completedAt: "2026-08-28T12:00:15-06:00",
      runtimeSeconds: 15,
      currentMessage: "Refresh complete: 1 updated, 1 unchanged, 1 with no information, and 1 failed.",
      message: "Refresh complete: 1 updated, 1 unchanged, 1 with no information, and 1 failed.",
    });
    const markup = renderToStaticMarkup(<CompanyInfoFinalReport run={completed} />);
    const content = text(markup);

    expect(content).toContain("Refresh Missing Company Info Result");
    expect(content).toContain("Refresh complete: 1 updated");
    expect(content).toContain("Duplicates skipped 2");
    expect(content).toContain("View Log");
    expect(content).not.toContain("Updated Bank");
    expect(content).not.toContain("Quiet Credit Union");
    expect(content).not.toContain("Unavailable Financial");
  });

  it("shows the run details, existing statuses, and company results in the log modal", () => {
    const completed = maintenanceRun({
      status: "Completed",
      running: false,
      completedAt: "2026-08-28T12:00:15-06:00",
      runtimeSeconds: 15,
    });
    const markup = renderToStaticMarkup(<CompanyInfoLogModal run={completed} summary={companySummary} onClose={() => undefined} />);
    const content = text(markup);

    expect(content).toContain("Company Information Refresh Log");
    expect(content).toContain("Runtime 15s");
    expect(content).toContain("Processed 3");
    expect(content).toContain("Updated 1");
    expect(content).toContain("Failed 1");
    expect(content).toContain("No information found 1");
    expect(content).toContain("All Results (3)");
    expect(content).toContain("Updated (1)");
    expect(content).toContain("Failed (1)");
    expect(content).toContain("No Information Found (1)");
    expect(content).not.toContain("Unchanged (0)");
    expect(content).toContain("Updated Bank Updated");
    expect(content).toContain("Quiet Credit Union No information found");
    expect(content).toContain("Unavailable Financial Failed");
    expect(content).toContain("Updated: Careers Page URL");
    expect(markup).toContain('placeholder="Search company..."');
    expect(markup).toContain("overflow-y-auto");
    expect(content).toContain("Close");
  });

  it("filters the already-loaded log results by status and company name", () => {
    expect(filterCompanyInfoResults(companySummary.companyResults, "failed", "unavailable").map((result) => result.companyName)).toEqual(["Unavailable Financial"]);
    expect(filterCompanyInfoResults(companySummary.companyResults, "updated", "credit union")).toEqual([]);
    expect(filterCompanyInfoResults(companySummary.companyResults, "all", "quiet").map((result) => result.companyName)).toEqual(["Quiet Credit Union"]);
  });

  it("keeps the latest completed report visible on the Utilities refresh page", () => {
    const completed = maintenanceRun({ status: "Completed", running: false, completedAt: "2026-08-28T12:00:15-06:00" });
    const job = maintenanceJob(completed);
    const maintenance = { jobs: [job], activeRuns: [], runningCount: 0, running_count: 0 };
    const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { location: { search: "", href: "https://radar.example/utilities" }, history: { replaceState: () => undefined } },
    });
    try {
      const markup = renderToStaticMarkup(<Utilities
        maintenance={maintenance}
        onMaintenanceRefresh={async () => maintenance}
        features={{ utilities: true, schedules: true, companyRefresh: true, discovery: true, browserJobs: true }}
      />);
      const content = text(markup);
      expect(content).toContain("Refresh Missing Company Info");
      expect(content).toContain("Refresh Missing Company Info Result");
      expect(content).toContain("View Log");
      expect(content).not.toContain("Updated Bank");
    } finally {
      if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
      else Reflect.deleteProperty(globalThis, "window");
    }
  });

  it("does not show View Log when a completed run has no detailed results", () => {
    const completed = maintenanceRun({
      status: "Completed",
      running: false,
      completedAt: "2026-08-28T12:00:15-06:00",
      summary: { ...companySummary, companyResults: [] },
      resultSummary: { ...companySummary, companyResults: [] },
    });

    const content = text(renderToStaticMarkup(<CompanyInfoFinalReport run={completed} />));
    expect(content).not.toContain("View Log");
    expect(content).toContain("No detailed company results were recorded for this run.");
  });

  it("includes the same final results in maintenance history", () => {
    const completed = maintenanceRun({ status: "Completed", running: false, completedAt: "2026-08-28T12:00:15-06:00" });
    const markup = renderToStaticMarkup(<HistoryModal
      job={maintenanceJob(completed)}
      runs={[completed]}
      loading={false}
      error=""
      onClose={() => undefined}
    />);

    expect(text(markup)).toContain("Updated Bank");
    expect(text(markup)).toContain("No additional official company information was confirmed.");
  });

  it("rejects malformed company refresh progress instead of displaying untrusted counters", () => {
    expect(isCompanyInfoRefreshSummary(companySummary)).toBe(true);
    expect(isCompanyInfoRefreshSummary({ ...companySummary, failedCount: -1 })).toBe(false);
    expect(isCompanyInfoRefreshSummary({ ...companySummary, companyResults: [{ ...companySummary.companyResults[0], outcome: "maybe" }] })).toBe(false);
  });
});
