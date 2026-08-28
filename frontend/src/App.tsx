import { useEffect, useMemo, useRef, useState } from "react";
import { Building2, ClipboardList, FileCheck2, Gauge, LogOut, Radar, Wrench } from "lucide-react";
import type { Company } from "./types/Company";
import type { ApplicationStatus, Job } from "./types/Job";
import { isResumeProfile, withoutResumeText, type ResumeProfile } from "./types/ResumeProfile";
import { emptyMaintenanceState, type MaintenanceJobsState, type MaintenanceRun } from "./types/Maintenance";
import type { FeatureFlags } from "./types/FeatureFlags";
import type { DataLoadStatus } from "./types/DataLoadState";
import {
  isApplicationOverrides,
  isApplicationPatchResponse,
  isCompanyArray,
  isJobArray,
  isJobMatchMutationResponse,
  isLogoutResponse,
  isMaintenanceJobsState,
  isRecord,
  normalizeSessionPayload,
  type ApplicationOverrides,
  type JobPayload,
  type SessionPayload,
} from "./runtimeSchemas";
import { Dashboard } from "./pages/Dashboard";
import { Companies } from "./pages/Companies";
import { JobList } from "./pages/JobList";
import { JobsAppliedFor } from "./pages/JobsAppliedFor";
import { ResumeMatch } from "./pages/ResumeMatch";
import { Utilities } from "./pages/Utilities";
import { ApiError, API_BASE, APP_BASE, AUTH_REQUIRED_EVENT, apiJson, appPath, userMessage } from "./api";

type Tab = "Dashboard" | "Companies" | "Job List" | "Jobs Applied For" | "Resume Match" | "Utilities";

const tabs: { name: Tab; icon: typeof Gauge }[] = [
  { name: "Dashboard", icon: Gauge },
  { name: "Companies", icon: Building2 },
  { name: "Job List", icon: ClipboardList },
  { name: "Jobs Applied For", icon: FileCheck2 },
  { name: "Resume Match", icon: Radar },
  { name: "Utilities", icon: Wrench },
];

function App() {
  const [session, setSession] = useState<SessionPayload | null>(null);
  const [authError, setAuthError] = useState("");
  const [authAttempt, setAuthAttempt] = useState(0);
  const [legacyStorageWarning, setLegacyStorageWarning] = useState("");
  useEffect(() => {
    try {
      localStorage.removeItem("financial-jobs-radar-applications");
      localStorage.removeItem("financial-jobs-radar-resume");
    } catch {
      setLegacyStorageWarning("Legacy browser storage could not be cleared. Opportunity Radar will not use browser-cached personal data, but you may need to clear this site's storage manually.");
    }
  }, []);
  useEffect(() => {
    const requireAuthentication = () => {
      setAuthError("");
      setSession(null);
      setAuthAttempt((value) => value + 1);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
  }, []);
  useEffect(() => {
    let stopped = false;
    let activeRequest: AbortController | null = null;
    let requestNumber = 0;
    const check = async () => {
      const currentRequest = ++requestNumber;
      activeRequest?.abort();
      const controller = new AbortController();
      activeRequest = controller;
      try {
        const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        const response = await fetch(`${API_BASE}/auth/session?returnTo=${encodeURIComponent(returnTo)}`, { cache: "no-store", credentials: "same-origin", signal: controller.signal });
        const payload: unknown = await response.json().catch(() => ({}));
        if (stopped || currentRequest !== requestNumber) return;
        if (response.status === 401) {
          window.location.assign(loginUrlFromPayload(payload) || "https://blueashdigital.tech/");
          return;
        }
        if (response.status === 403) throw new ApiError("Your account does not have access to Opportunity Radar.", 403);
        if (!response.ok) throw new ApiError("Authentication is temporarily unavailable.", response.status);
        const nextSession = normalizeSessionPayload(payload);
        if (!nextSession) throw new ApiError("Authentication returned an invalid response.");
        if (!stopped) {
          setSession(nextSession);
          setAuthError("");
        }
      } catch (caught) {
        if (!isAbortError(caught) && !stopped && currentRequest === requestNumber) {
          setAuthError(userMessage(caught, "Authentication is temporarily unavailable."));
        }
      } finally {
        if (activeRequest === controller) activeRequest = null;
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), 60_000);
    const onVisibility = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stopped = true; requestNumber += 1; activeRequest?.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [authAttempt]);
  if (authError) return <div className="grid min-h-screen place-items-center px-4 text-center text-red-300"><div className="panel max-w-lg p-6"><h1 className="text-xl font-semibold text-white">Opportunity Radar is unavailable</h1><p className="mt-2">{authError}</p>{legacyStorageWarning ? <p className="mt-3 text-sm text-amber-200">{legacyStorageWarning}</p> : null}<button className="btn mt-5" type="button" onClick={() => { setAuthError(""); setSession(null); setAuthAttempt((value) => value + 1); }}>Retry authentication</button></div></div>;
  if (session === null) return <div className="grid min-h-screen place-items-center px-4 text-center text-slate-400"><div><p>Loading Opportunity Radar...</p>{legacyStorageWarning ? <p className="mt-3 max-w-lg text-sm text-amber-200">{legacyStorageWarning}</p> : null}</div></div>;
  return <OpportunityApp key={session.id} sessionEmail={session.email} features={session.features} initialOperationError={legacyStorageWarning} onLogout={async () => {
    const result = await apiJson<unknown>("/auth/logout", { method: "POST" }, "Opportunity Radar could not sign you out.");
    if (!isLogoutResponse(result)) throw new ApiError("Opportunity Radar could not sign you out. The server returned an invalid response.");
    window.location.assign(result.redirectUrl);
  }} />;
}

function OpportunityApp({ sessionEmail, features, initialOperationError, onLogout }: { sessionEmail: string; features: FeatureFlags; initialOperationError: string; onLogout: () => Promise<void> }) {
  const [activeTab, setActiveTab] = useState<Tab>(() => tabFromLocation());
  const [companies, setCompanies] = useState<Company[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [resume, setResume] = useState<ResumeProfile | null>(null);
  const [selectedCompanyId, setSelectedCompanyId] = useState<string | undefined>();
  const [selectedCompanyName, setSelectedCompanyName] = useState<string | undefined>();
  const [maintenance, setMaintenance] = useState<MaintenanceJobsState>(emptyMaintenanceState);
  const [dataStatus, setDataStatus] = useState<DataLoadStatus>("loading");
  const [dataError, setDataError] = useState("");
  const [dataAttempt, setDataAttempt] = useState(0);
  const [operationError, setOperationError] = useState(initialOperationError);
  const [maintenanceError, setMaintenanceError] = useState("");
  const [pendingApplicationIds, setPendingApplicationIds] = useState<Set<string>>(() => new Set());
  const [signingOut, setSigningOut] = useState(false);
  const dataStatusRef = useRef<DataLoadStatus>("loading");
  const maintenanceRef = useRef<MaintenanceJobsState>(emptyMaintenanceState);
  const previousMaintenanceRuns = useRef<Map<string, MaintenanceRun>>(new Map());

  const updateDataStatus = (status: DataLoadStatus) => {
    dataStatusRef.current = status;
    setDataStatus(status);
  };

  useEffect(() => {
    if (initialOperationError) setOperationError((current) => current || initialOperationError);
  }, [initialOperationError]);

  const fetchCompanies = async (): Promise<Company[]> => {
    const loadedCompanies = await apiJson<unknown>("/companies", {}, "Companies could not be loaded.");
    if (!isCompanyArray(loadedCompanies)) throw new ApiError("Companies could not be loaded. The server returned an invalid response.");
    return loadedCompanies;
  };

  const fetchJobs = async (): Promise<Job[]> => {
    const [loadedJobs, persistedApplications] = await Promise.all([
      apiJson<unknown>("/jobs", {}, "Jobs could not be loaded."),
      apiJson<unknown>("/applications", {}, "Application tracking could not be loaded."),
    ]);
    if (!isJobArray(loadedJobs) || !isApplicationOverrides(persistedApplications)) throw new ApiError("Jobs could not be loaded. The server returned an invalid response.");
    return mergeJobApplications(loadedJobs, persistedApplications);
  };

  const reloadData = async (scope: { companies?: boolean; jobs?: boolean }) => {
    const [loadedCompanies, loadedJobs] = await Promise.all([
      scope.companies ? fetchCompanies() : Promise.resolve(undefined),
      scope.jobs ? fetchJobs() : Promise.resolve(undefined),
    ]);
    if (loadedCompanies) setCompanies(loadedCompanies);
    if (loadedJobs) setJobs(loadedJobs);
  };

  const refreshMaintenanceJobs = async (): Promise<MaintenanceJobsState> => {
    if (!features.utilities) {
      maintenanceRef.current = emptyMaintenanceState;
      previousMaintenanceRuns.current.clear();
      setMaintenance(emptyMaintenanceState);
      return emptyMaintenanceState;
    }
    try {
      const next = await apiJson<unknown>("/maintenance/jobs", {}, "Maintenance status could not be loaded.");
      if (!isMaintenanceJobsState(next)) throw new ApiError("Maintenance status could not be loaded. The server returned an invalid response.");
      const activeById = new Map(next.activeRuns.map((run) => [run.id, run]));
      const finished = [...previousMaintenanceRuns.current.values()].filter((run) => !activeById.has(run.id));
      previousMaintenanceRuns.current = activeById;
      maintenanceRef.current = next;
      setMaintenance(next);
      setMaintenanceError("");
      if (finished.length) {
        const companyChanged = finished.some((run) => ["refresh-missing-company-information", "refresh-company-discovery", "import-data"].includes(run.action));
        const jobsChanged = finished.some((run) => ["refresh-all-job-listings", "reprocess-saved-jobs", "rematch-all-jobs", "import-data"].includes(run.action));
        if (companyChanged || jobsChanged) {
          void reloadAfterMutation(
            { companies: companyChanged, jobs: jobsChanged },
            "Maintenance finished, but updated dashboard data could not be loaded.",
          );
        }
      }
      return next;
    } catch (error) {
      setMaintenanceError(userMessage(error, "Maintenance status could not be loaded."));
      return maintenanceRef.current;
    }
  };

  const reloadAfterMutation = async (scope: { companies?: boolean; jobs?: boolean }, fallbackMessage: string) => {
    const canRestoreReadyState = dataStatusRef.current === "ready";
    if (canRestoreReadyState) updateDataStatus("loading");
    setOperationError("");
    try {
      await reloadData(scope);
      if (canRestoreReadyState) {
        setDataError("");
        updateDataStatus("ready");
      }
    } catch (error) {
      const message = userMessage(error, fallbackMessage);
      setOperationError(message);
      setDataError(message);
      updateDataStatus("error");
    }
  };

  const handleCompanyDeleted = async (_deletedJobIds: string[]) => {
    await reloadAfterMutation({ companies: true, jobs: true }, "The company was deleted, but the latest dashboard data could not be loaded.");
  };

  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    const bootstrap = async () => {
      updateDataStatus("loading");
      setDataError("");
      try {
        const request = { signal: controller.signal };
        const [loadedCompanies, loadedJobs, persistedApplications, persistedResume] = await Promise.all([
          apiJson<unknown>("/companies", request, "Companies could not be loaded."),
          apiJson<unknown>("/jobs", request, "Jobs could not be loaded."),
          apiJson<unknown>("/applications", request, "Application tracking could not be loaded."),
          apiJson<unknown>("/resume", request, "The active resume could not be loaded."),
        ]);
        if (!isCompanyArray(loadedCompanies) || !isJobArray(loadedJobs) || !isApplicationOverrides(persistedApplications)
          || (persistedResume !== null && !isResumeProfile(persistedResume))) {
          throw new ApiError("Dashboard data could not be loaded. The server returned an invalid response.");
        }
        if (stopped) return;
        setCompanies(loadedCompanies);
        setJobs(mergeJobApplications(loadedJobs, persistedApplications));
        setResume(persistedResume ? withoutResumeText(persistedResume) : null);
        updateDataStatus("ready");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (!stopped) {
          setDataError(userMessage(error, "Dashboard data could not be loaded."));
          updateDataStatus("error");
        }
      }
    };
    void bootstrap();
    return () => { stopped = true; controller.abort(); };
  }, [dataAttempt]);

  useEffect(() => {
    if (!features.utilities) {
      maintenanceRef.current = emptyMaintenanceState;
      previousMaintenanceRuns.current.clear();
      setMaintenance(emptyMaintenanceState);
      return;
    }
    let stopped = false;
    let timer = 0;
    const poll = async () => {
      const next = await refreshMaintenanceJobs();
      if (!stopped) timer = window.setTimeout(poll, next.runningCount ? 2500 : 10000);
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [features.utilities]);

  const pageTitle = useMemo(() => {
    if (activeTab === "Job List" && selectedCompanyId) {
      const company = companies.find((item) => item.id === selectedCompanyId);
      return company ? `Jobs at ${company.name}` : "Job List";
    }
    return activeTab;
  }, [activeTab, companies, selectedCompanyId]);
  const visibleDataStatus: DataLoadStatus = dataStatus === "ready" && companies.length === 0 && jobs.length === 0 ? "empty" : dataStatus;

  useEffect(() => {
    document.title = `${pageTitle} | Opportunity Radar`;
  }, [pageTitle]);

  const markApplied = async (jobId: string): Promise<boolean> => {
    const job = jobs.find((item) => item.id === jobId);
    return updateJob(jobId, {
      applied: true,
      applicationStatus: job?.applicationStatus === "Interested" ? "Applied" : job?.applicationStatus || "Applied",
      dateApplied: job?.dateApplied || new Date().toISOString().slice(0, 10),
      notInterested: false,
    });
  };

  const updateJob = async (jobId: string, patch: Partial<Job>): Promise<boolean> => {
    setPendingApplicationIds((current) => new Set(current).add(jobId));
    setOperationError("");
    try {
      const result = await apiJson<unknown>(
        `/applications/${encodeURIComponent(jobId)}`,
        { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) },
        "Application tracking could not be updated.",
      );
      if (!isApplicationPatchResponse(result)) {
        throw new ApiError("Application tracking could not be updated. The server returned an invalid response.");
      }
      setJobs((current) => current.map((job) => job.id === jobId ? { ...job, ...result.application } : job));
      return true;
    } catch (error) {
      setOperationError(userMessage(error, "Application tracking could not be updated."));
      return false;
    } finally {
      setPendingApplicationIds((current) => {
        const next = new Set(current);
        next.delete(jobId);
        return next;
      });
    }
  };

  const updateResume = async (profile: ResumeProfile) => {
    setResume(withoutResumeText(profile));
    await reloadAfterMutation(
      { jobs: true },
      "The resume was uploaded, but updated match data could not be loaded.",
    );
  };

  const rematchJob = async (jobId: string): Promise<Job> => {
    const result = await apiJson<unknown>(
      `/jobs/${encodeURIComponent(jobId)}/match`,
      { method: "POST" },
      "This job could not be matched.",
    );
    if (!isJobMatchMutationResponse(result) || result.jobId !== jobId) {
      throw new ApiError("This job could not be matched. The server returned an invalid response.");
    }
    const existingJob = jobs.find((job) => job.id === jobId);
    if (!existingJob) throw new ApiError("This job could not be matched because it is no longer in the current results.");
    const updatedJob: Job = { ...existingJob, ...result.job };
    setJobs((current) => current.map((job) => job.id === jobId ? updatedJob : job));
    return updatedJob;
  };

  const retryData = () => {
    setOperationError("");
    setDataAttempt((value) => value + 1);
  };

  const signOut = async () => {
    setSigningOut(true);
    setOperationError("");
    try {
      await onLogout();
    } catch (error) {
      setOperationError(userMessage(error, "Opportunity Radar could not sign you out."));
      setSigningOut(false);
    }
  };

  const navigate = (tab: string) => {
    if (tab === "Utilities" && !features.utilities) return;
    setSelectedCompanyId(undefined);
    setSelectedCompanyName(undefined);
    setActiveTab(tab as Tab);
    window.history.pushState(null, "", appPath(TAB_PATHS[tab as Tab]));
  };

  const viewCompanyJobs = (companyId: string) => {
    setSelectedCompanyId(companyId);
    setSelectedCompanyName(undefined);
    setActiveTab("Job List");
    window.history.pushState(null, "", appPath(TAB_PATHS["Job List"]));
  };

  const viewCompanyDetails = (companyName: string) => {
    setSelectedCompanyId(undefined);
    setSelectedCompanyName(companyName);
    setActiveTab("Companies");
    window.history.pushState(null, "", appPath(TAB_PATHS.Companies));
  };

  useEffect(() => {
    const onPopState = () => {
      setSelectedCompanyId(undefined);
      setSelectedCompanyName(undefined);
      setActiveTab(tabFromLocation());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  return (
    <div className="min-h-screen">
      <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-5 lg:flex-row lg:px-6">
        <aside className="panel h-fit p-4 lg:sticky lg:top-5 lg:w-72">
          <div className="flex items-center gap-3 py-1">
            <div className="grid h-11 w-11 place-items-center rounded-lg border border-radar-accent bg-radar-primary text-white shadow-sm">
              <Radar size={24} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-white">Opportunity Radar</h1>
            </div>
          </div>
          <nav className="mt-6 space-y-2">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              const selected = activeTab === tab.name;
              const disabled = tab.name === "Utilities" && !features.utilities;
              return (
                <button
                  className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition ${
                    selected ? "bg-radar-primary text-white" : "text-slate-300 hover:bg-radar-bg hover:text-white"
                  } disabled:cursor-not-allowed disabled:opacity-50`}
                  key={tab.name}
                  type="button"
                  disabled={disabled}
                  title={disabled ? "Utilities are disabled for the initial production release." : undefined}
                  onClick={() => navigate(tab.name)}
                >
                  <Icon size={17} />
                  {tab.name}{disabled ? <span className="ml-auto text-xs">Unavailable</span> : null}
                </button>
              );
            })}
          </nav>
          {features.utilities && maintenance.runningCount ? (
            <button
              className="mt-4 flex w-full items-center gap-2 rounded-md border border-radar-accent/60 bg-radar-primary/20 px-3 py-2 text-left text-sm text-radar-highlight transition hover:border-radar-highlight hover:text-white"
              type="button"
              onClick={() => navigate("Utilities")}
              aria-label={`${maintenance.runningCount} maintenance ${maintenance.runningCount === 1 ? "job" : "jobs"} running. Open Utilities.`}
            >
              <Radar className="animate-pulse" size={16} />
              {maintenance.runningCount} maintenance {maintenance.runningCount === 1 ? "job" : "jobs"} running
            </button>
          ) : null}
          <div className="mt-5 border-t border-radar-line pt-4"><p className="truncate text-xs text-slate-500" title={sessionEmail}>{sessionEmail}</p><button className="btn mt-3 w-full justify-center" type="button" disabled={signingOut} onClick={() => void signOut()}><LogOut size={16} />{signingOut ? "Signing out..." : "Sign Out"}</button></div>
        </aside>

        <main className="min-w-0 flex-1">
          <header className="mb-6">
            <p className="text-sm uppercase tracking-wide text-radar-highlight">Companies, opportunities, jobs, applications</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">{pageTitle}</h2>
          </header>

          {operationError ? <div className="mb-5 flex flex-col gap-3 rounded-md border border-red-900 bg-red-950/40 px-4 py-3 text-sm text-red-200 sm:flex-row sm:items-center sm:justify-between" role="alert"><span>{operationError}</span><button className="btn shrink-0" type="button" onClick={dataStatus === "error" ? retryData : () => setOperationError("")}>{dataStatus === "error" ? "Retry data" : "Dismiss"}</button></div> : null}
          {maintenanceError && features.utilities ? <div className="mb-5 flex flex-col gap-3 rounded-md border border-amber-800 bg-amber-950/30 px-4 py-3 text-sm text-amber-200 sm:flex-row sm:items-center sm:justify-between" role="alert"><span>{maintenanceError}</span><button className="btn shrink-0" type="button" onClick={() => void refreshMaintenanceJobs()}>Retry status</button></div> : null}

          {activeTab === "Dashboard" ? (
            <Dashboard
              companies={companies}
              jobs={jobs}
              status={visibleDataStatus}
              error={dataError}
              onRetry={retryData}
              onNavigate={navigate}
              onRematch={rematchJob}
            />
          ) : null}
          {activeTab === "Companies" ? (
            <Companies
              onViewCompanyJobs={viewCompanyJobs}
              onCompaniesChanged={() => reloadAfterMutation({ companies: true }, "The company was saved, but the latest dashboard data could not be loaded.")}
              onCompanyRefreshed={() => reloadAfterMutation({ companies: true, jobs: true }, "The company was refreshed, but the latest dashboard data could not be loaded.")}
              onCompanyDeleted={handleCompanyDeleted}
              selectedCompanyName={selectedCompanyName}
              refreshEnabled={features.companyRefresh && features.discovery && features.browserJobs}
            />
          ) : null}
          {activeTab === "Job List" ? (
            <JobList
              jobs={jobs}
              dataStatus={visibleDataStatus}
              dataError={dataError}
              onRetry={retryData}
              onRematch={rematchJob}
              selectedCompanyId={selectedCompanyId}
              onMarkApplied={markApplied}
              onNotInterested={(jobId) => updateJob(jobId, { notInterested: true })}
              onUpdateNotes={(jobId, notes) => updateJob(jobId, { notes })}
              pendingApplicationIds={pendingApplicationIds}
              onViewCompany={viewCompanyDetails}
            />
          ) : null}
          {activeTab === "Jobs Applied For" ? (
            <JobsAppliedFor
              jobs={jobs}
              dataStatus={visibleDataStatus}
              dataError={dataError}
              onRetry={retryData}
              pendingApplicationIds={pendingApplicationIds}
              onUpdateStatus={(jobId, applicationStatus: ApplicationStatus) => updateJob(jobId, { applicationStatus })}
              onUpdateFollowUp={(jobId, followUpDate) => updateJob(jobId, { followUpDate })}
              onUpdateNotes={(jobId, notes) => updateJob(jobId, { notes })}
            />
          ) : null}
          {activeTab === "Resume Match" ? (
            <ResumeMatch jobs={jobs} resume={resume} onResumeChange={updateResume} maintenance={maintenance} onMaintenanceRefresh={refreshMaintenanceJobs} onRematch={rematchJob} utilitiesEnabled={features.utilities} dataStatus={visibleDataStatus} dataError={dataError} onRetry={retryData} />
          ) : null}
          {activeTab === "Utilities" ? (
            features.utilities
              ? <Utilities maintenance={maintenance} onMaintenanceRefresh={refreshMaintenanceJobs} features={features} />
              : <section className="panel p-6"><h3 className="text-lg font-semibold text-white">Utilities unavailable</h3><p className="mt-2 text-sm text-slate-400">Utilities are disabled for the initial production release.</p></section>
          ) : null}
        </main>
      </div>
    </div>
  );
}

export default App;

const TAB_PATHS: Record<Tab, string> = {
  Dashboard: "/", Companies: "/companies", "Job List": "/jobs",
  "Jobs Applied For": "/applications", "Resume Match": "/resume-match", Utilities: "/utilities",
};

function tabFromLocation(): Tab {
  const pathname = window.location.pathname;
  const relative = APP_BASE && pathname.startsWith(APP_BASE) ? pathname.slice(APP_BASE.length) || "/" : pathname;
  return (Object.entries(TAB_PATHS).find(([, path]) => path === relative.replace(/\/$/, "") || (path === "/" && relative === "/"))?.[0] as Tab | undefined) ?? "Dashboard";
}

function mergeJobApplications(jobs: JobPayload[], overrides: ApplicationOverrides): Job[] {
  return jobs.map((job) => ({
    ...job,
    applied: false,
    applicationStatus: "Interested",
    dateApplied: "",
    followUpDate: "",
    notes: "",
    notInterested: false,
    matchScore: job.matchScore ?? null,
    ...overrides[job.id],
  }));
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function loginUrlFromPayload(payload: unknown): string {
  if (!isRecord(payload)) return "";
  if (typeof payload.loginUrl === "string") return payload.loginUrl;
  return isRecord(payload.detail) && typeof payload.detail.loginUrl === "string"
    ? payload.detail.loginUrl
    : "";
}
