import { useEffect, useMemo, useRef, useState } from "react";
import { Building2, ClipboardList, FileCheck2, Gauge, LogOut, Radar, Wrench } from "lucide-react";
import type { Company } from "./types/Company";
import type { ApplicationStatus, Job } from "./types/Job";
import type { ResumeProfile } from "./types/ResumeProfile";
import { emptyMaintenanceState, type MaintenanceJobsState, type MaintenanceRun } from "./types/Maintenance";
import { normalizeFeatureFlags, type FeatureFlags } from "./types/FeatureFlags";
import { Dashboard } from "./pages/Dashboard";
import { Companies } from "./pages/Companies";
import { JobList } from "./pages/JobList";
import { JobsAppliedFor } from "./pages/JobsAppliedFor";
import { ResumeMatch } from "./pages/ResumeMatch";
import { Utilities } from "./pages/Utilities";
import { API_BASE, APP_BASE, appPath } from "./api";

type Tab = "Dashboard" | "Companies" | "Job List" | "Jobs Applied For" | "Resume Match" | "Utilities";

const tabs: { name: Tab; icon: typeof Gauge }[] = [
  { name: "Dashboard", icon: Gauge },
  { name: "Companies", icon: Building2 },
  { name: "Job List", icon: ClipboardList },
  { name: "Jobs Applied For", icon: FileCheck2 },
  { name: "Resume Match", icon: Radar },
  { name: "Utilities", icon: Wrench },
];

type ApplicationOverrides = Record<string, Partial<Job>>;
type Session = { authenticated: true; email: string; displayName: string; role: string; features: FeatureFlags; developmentBypass?: boolean };

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [authError, setAuthError] = useState("");
  useEffect(() => {
    let stopped = false;
    const check = async () => {
      try {
        const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        const response = await fetch(`${API_BASE}/auth/session?returnTo=${encodeURIComponent(returnTo)}`, { cache: "no-store", credentials: "same-origin" });
        const payload = await response.json().catch(() => ({})) as { detail?: string | { message?: string; loginUrl?: string }; loginUrl?: string } & Partial<Session>;
        if (response.status === 401) {
          const destination = typeof payload.detail === "object" ? payload.detail.loginUrl : payload.loginUrl;
          window.location.assign(destination || "https://blueashdigital.tech/");
          return;
        }
        if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : payload.detail?.message || "Authentication is temporarily unavailable.");
        if (!stopped) {
          setSession({ ...payload, features: normalizeFeatureFlags(payload.features) } as Session);
          setAuthError("");
        }
      } catch (caught) {
        if (!stopped) setAuthError(caught instanceof Error ? caught.message : "Authentication is temporarily unavailable.");
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), 60_000);
    const onVisibility = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stopped = true; window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, []);
  if (authError) return <div className="grid min-h-screen place-items-center px-4 text-center text-red-300"><div><h1 className="text-xl font-semibold text-white">Opportunity Radar is unavailable</h1><p className="mt-2">{authError}</p></div></div>;
  if (session === null) return <div className="grid min-h-screen place-items-center text-slate-400">Loading Opportunity Radar...</div>;
  return <OpportunityApp sessionEmail={session.email} features={session.features} onLogout={async () => {
    const response = await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "same-origin" });
    const result = await response.json().catch(() => ({})) as { redirectUrl?: string };
    window.location.assign(result.redirectUrl || "https://blueashdigital.tech/");
  }} />;
}

function OpportunityApp({ sessionEmail, features, onLogout }: { sessionEmail: string; features: FeatureFlags; onLogout: () => Promise<void> }) {
  const [activeTab, setActiveTab] = useState<Tab>(() => tabFromLocation());
  const [companies, setCompanies] = useState<Company[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [applicationOverrides, setApplicationOverrides] = useState<ApplicationOverrides>(() => {
    const stored = localStorage.getItem("financial-jobs-radar-applications");
    return stored ? JSON.parse(stored) : {};
  });
  const [resume, setResume] = useState<ResumeProfile | null>(() => {
    const stored = localStorage.getItem("financial-jobs-radar-resume");
    return stored ? JSON.parse(stored) : null;
  });
  const [selectedCompanyId, setSelectedCompanyId] = useState<string | undefined>();
  const [selectedCompanyName, setSelectedCompanyName] = useState<string | undefined>();
  const [maintenance, setMaintenance] = useState<MaintenanceJobsState>(emptyMaintenanceState);
  const [initialDataLoaded, setInitialDataLoaded] = useState(false);
  const maintenanceRef = useRef<MaintenanceJobsState>(emptyMaintenanceState);
  const previousMaintenanceRuns = useRef<Map<string, MaintenanceRun>>(new Map());

  const reloadCompanies = async () => {
    const loadedCompanies = await loadApiJson<Company[]>("/companies", async () => []);
    setCompanies(loadedCompanies);
  };

  const reloadJobs = async () => {
    const [loadedJobs, persistedApplications] = await Promise.all([
      loadApiJson<Job[]>("/jobs", async () => []),
      loadApiJson<ApplicationOverrides>("/applications", async () => ({})),
    ]);
    setJobs(mergeJobApplications(loadedJobs, { ...persistedApplications, ...applicationOverrides }));
  };

  const reloadData = async (scope: { companies?: boolean; jobs?: boolean }) => {
    if (scope.companies) await reloadCompanies();
    if (scope.jobs) await reloadJobs();
  };

  const refreshMaintenanceJobs = async (): Promise<MaintenanceJobsState> => {
    if (!features.utilities) {
      maintenanceRef.current = emptyMaintenanceState;
      previousMaintenanceRuns.current.clear();
      setMaintenance(emptyMaintenanceState);
      return emptyMaintenanceState;
    }
    try {
      const response = await fetch(`${API_BASE}/maintenance/jobs`, { cache: "no-store" });
      if (!response.ok) return maintenanceRef.current;
      const next = await response.json() as MaintenanceJobsState;
      const activeById = new Map(next.activeRuns.map((run) => [run.id, run]));
      const finished = [...previousMaintenanceRuns.current.values()].filter((run) => !activeById.has(run.id));
      previousMaintenanceRuns.current = activeById;
      maintenanceRef.current = next;
      setMaintenance(next);
      if (finished.length) {
        const companyChanged = finished.some((run) => ["refresh-missing-company-information", "refresh-company-discovery", "import-data"].includes(run.action));
        const jobsChanged = finished.some((run) => ["refresh-all-job-listings", "reprocess-saved-jobs", "rematch-all-jobs", "import-data"].includes(run.action));
        if (companyChanged || jobsChanged) void reloadData({ companies: companyChanged, jobs: jobsChanged });
      }
      return next;
    } catch {
      return maintenanceRef.current;
    }
  };

  const handleCompanyDeleted = async (deletedJobIds: string[]) => {
    const deletedIds = new Set(deletedJobIds);
    setApplicationOverrides((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([jobId]) => !deletedIds.has(jobId)));
      localStorage.setItem("financial-jobs-radar-applications", JSON.stringify(next));
      return next;
    });
    await reloadData({ companies: true, jobs: true });
  };

  useEffect(() => {
    const bootstrap = async () => {
      const browserApplications = applicationOverrides;
      const browserResume = resume;
      const [loadedCompanies, loadedJobs, persistedApplications, persistedResume] = await Promise.all([
        loadApiJson<Company[]>("/companies", async () => []),
        loadApiJson<Job[]>("/jobs", async () => []),
        loadApiJson<ApplicationOverrides>("/applications", async () => ({})),
        loadApiJson<ResumeProfile | null>("/resume", async () => null),
      ]);
      const mergedApplications = { ...persistedApplications, ...browserApplications };
      setCompanies(loadedCompanies);
      setApplicationOverrides(mergedApplications);
      setJobs(mergeJobApplications(loadedJobs, mergedApplications));
      if (Object.keys(browserApplications).length) {
        void apiRequest("/applications/import-browser-overrides", "POST", { overrides: browserApplications });
      }
      if (persistedResume) {
        setResume(persistedResume);
      } else if (browserResume) {
        void apiRequest("/resume", "PUT", browserResume);
      }
      setInitialDataLoaded(true);
    };
    void bootstrap();
  }, []);

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

  useEffect(() => {
    localStorage.setItem("financial-jobs-radar-applications", JSON.stringify(applicationOverrides));
    setJobs((current) => mergeJobApplications(current, applicationOverrides));
  }, [applicationOverrides]);

  const pageTitle = useMemo(() => {
    if (activeTab === "Job List" && selectedCompanyId) {
      const company = companies.find((item) => item.id === selectedCompanyId);
      return company ? `Jobs at ${company.name}` : "Job List";
    }
    return activeTab;
  }, [activeTab, companies, selectedCompanyId]);

  useEffect(() => {
    document.title = `${pageTitle} | Opportunity Radar`;
  }, [pageTitle]);

  const markApplied = (jobId: string) => {
    const job = jobs.find((item) => item.id === jobId);
    updateJob(jobId, {
      applied: true,
      applicationStatus: job?.applicationStatus === "Interested" ? "Applied" : job?.applicationStatus || "Applied",
      dateApplied: job?.dateApplied || new Date().toISOString().slice(0, 10),
      notInterested: false,
    });
  };

  const updateJob = (jobId: string, patch: Partial<Job>) => {
    setApplicationOverrides((current) => ({
      ...current,
      [jobId]: {
        ...current[jobId],
        ...patch,
      },
    }));
    void apiRequest(`/applications/${encodeURIComponent(jobId)}`, "PUT", patch);
  };

  const updateResume = (profile: ResumeProfile | null) => {
    setResume(profile);
    if (profile) void apiRequest("/resume", "PUT", profile).then(reloadJobs);
  };

  const rematchJob = async (jobId: string): Promise<Job> => {
    const response = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}/match`, { method: "POST" });
    if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || "This job could not be matched.");
    const result = await response.json() as { job: Job };
    await reloadJobs();
    return result.job;
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
          <div className="mt-5 border-t border-radar-line pt-4"><p className="truncate text-xs text-slate-500" title={sessionEmail}>{sessionEmail}</p><button className="btn mt-3 w-full justify-center" type="button" onClick={() => void onLogout()}><LogOut size={16} />Sign Out</button></div>
        </aside>

        <main className="min-w-0 flex-1">
          <header className="mb-6">
            <p className="text-sm uppercase tracking-wide text-radar-highlight">Companies, opportunities, jobs, applications</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">{pageTitle}</h2>
          </header>

          {activeTab === "Dashboard" ? (
            <Dashboard
              companies={companies}
              jobs={jobs}
              loaded={initialDataLoaded}
              onNavigate={navigate}
              onRematch={rematchJob}
            />
          ) : null}
          {activeTab === "Companies" ? (
            <Companies
              onViewCompanyJobs={viewCompanyJobs}
              onCompaniesChanged={() => reloadData({ companies: true })}
              onCompanyRefreshed={() => reloadData({ companies: true, jobs: true })}
              onCompanyDeleted={handleCompanyDeleted}
              selectedCompanyName={selectedCompanyName}
              refreshEnabled={features.companyRefresh && features.discovery && features.browserJobs}
            />
          ) : null}
          {activeTab === "Job List" ? (
            <JobList
              jobs={jobs}
              onRematch={rematchJob}
              selectedCompanyId={selectedCompanyId}
              onMarkApplied={markApplied}
              onNotInterested={(jobId) => updateJob(jobId, { notInterested: true })}
              onUpdateNotes={(jobId, notes) => updateJob(jobId, { notes })}
              onViewCompany={viewCompanyDetails}
            />
          ) : null}
          {activeTab === "Jobs Applied For" ? (
            <JobsAppliedFor
              jobs={jobs}
              onUpdateStatus={(jobId, applicationStatus: ApplicationStatus) => updateJob(jobId, { applicationStatus })}
              onUpdateFollowUp={(jobId, followUpDate) => updateJob(jobId, { followUpDate })}
              onUpdateNotes={(jobId, notes) => updateJob(jobId, { notes })}
            />
          ) : null}
          {activeTab === "Resume Match" ? (
            <ResumeMatch jobs={jobs} resume={resume} onResumeChange={updateResume} maintenance={maintenance} onMaintenanceRefresh={refreshMaintenanceJobs} onJobsReload={reloadJobs} onRematch={rematchJob} utilitiesEnabled={features.utilities} />
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

async function loadApiJson<T>(endpoint: string, fallback: () => Promise<T>): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${endpoint}`, { cache: "no-store" });
    if (!response.ok) return fallback();
    return await response.json() as T;
  } catch {
    return fallback();
  }
}

async function apiRequest(endpoint: string, method: "POST" | "PUT", body: unknown): Promise<void> {
  try {
    await fetch(`${API_BASE}${endpoint}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    // Browser storage remains a recovery copy while the API is unavailable.
  }
}

function mergeJobApplications(jobs: Job[], overrides: ApplicationOverrides): Job[] {
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
