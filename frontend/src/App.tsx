import { useEffect, useMemo, useState } from "react";
import { Building2, ClipboardList, FileCheck2, Gauge, Radar } from "lucide-react";
import { sampleCompanies } from "./data/sampleCompanies";
import { sampleJobs } from "./data/sampleJobs";
import type { Company } from "./types/Company";
import type { ApplicationStatus, Job } from "./types/Job";
import type { ResumeProfile } from "./types/ResumeProfile";
import { Dashboard } from "./pages/Dashboard";
import { Companies } from "./pages/Companies";
import { JobList } from "./pages/JobList";
import { JobsAppliedFor } from "./pages/JobsAppliedFor";
import { ResumeMatch } from "./pages/ResumeMatch";

type Tab = "Dashboard" | "Companies" | "Job List" | "Jobs Applied For" | "Resume Match";

const tabs: { name: Tab; icon: typeof Gauge }[] = [
  { name: "Dashboard", icon: Gauge },
  { name: "Companies", icon: Building2 },
  { name: "Job List", icon: ClipboardList },
  { name: "Jobs Applied For", icon: FileCheck2 },
  { name: "Resume Match", icon: Radar },
];

type ApplicationOverrides = Record<string, Partial<Job>>;

function App() {
  const [activeTab, setActiveTab] = useState<Tab>("Dashboard");
  const [companies, setCompanies] = useState<Company[]>(sampleCompanies);
  const [jobs, setJobs] = useState<Job[]>(sampleJobs);
  const [applicationOverrides, setApplicationOverrides] = useState<ApplicationOverrides>(() => {
    const stored = localStorage.getItem("financial-jobs-radar-applications");
    return stored ? JSON.parse(stored) : {};
  });
  const [resume, setResume] = useState<ResumeProfile | null>(() => {
    const stored = localStorage.getItem("financial-jobs-radar-resume");
    return stored ? JSON.parse(stored) : null;
  });
  const [selectedCompanyId, setSelectedCompanyId] = useState<string | undefined>();

  const reloadCompanies = async () => {
    const loadedCompanies = await loadJson<Company[]>("/data/companies.json", sampleCompanies);
    setCompanies(loadedCompanies);
  };

  useEffect(() => {
    reloadCompanies();
    loadJson<Job[]>("/data/jobs.json", sampleJobs).then((loadedJobs) => {
      setJobs(mergeJobApplications(loadedJobs, applicationOverrides));
    });
  }, []);

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

  const markApplied = (jobId: string) => {
    const job = jobs.find((item) => item.id === jobId);
    setApplicationOverrides((current) => ({
      ...current,
      [jobId]: {
        ...current[jobId],
        applied: true,
        applicationStatus: job?.applicationStatus === "Interested" ? "Applied" : job?.applicationStatus || "Applied",
        dateApplied: job?.dateApplied || new Date().toISOString().slice(0, 10),
        notInterested: false,
      },
    }));
  };

  const updateJob = (jobId: string, patch: Partial<Job>) => {
    setApplicationOverrides((current) => ({
      ...current,
      [jobId]: {
        ...current[jobId],
        ...patch,
      },
    }));
  };

  const navigate = (tab: string) => {
    setSelectedCompanyId(undefined);
    setActiveTab(tab as Tab);
  };

  const viewCompanyJobs = (companyId: string) => {
    setSelectedCompanyId(companyId);
    setActiveTab("Job List");
  };

  return (
    <div className="min-h-screen">
      <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-5 lg:flex-row lg:px-6">
        <aside className="panel h-fit p-4 lg:sticky lg:top-5 lg:w-72">
          <div className="flex items-center gap-3">
            <div className="grid h-11 w-11 place-items-center rounded-lg border border-radar-cyan bg-radar-cyan/12 text-radar-cyan">
              <Radar size={24} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-white">Financial Jobs Radar</h1>
              <p className="text-sm text-slate-400">Local-first tracking dashboard</p>
            </div>
          </div>
          <nav className="mt-6 space-y-2">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              const selected = activeTab === tab.name;
              return (
                <button
                  className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition ${
                    selected ? "bg-radar-cyan text-slate-950" : "text-slate-300 hover:bg-radar-bg hover:text-white"
                  }`}
                  key={tab.name}
                  onClick={() => navigate(tab.name)}
                >
                  <Icon size={17} />
                  {tab.name}
                </button>
              );
            })}
          </nav>
          <div className="mt-6 rounded-md border border-radar-line bg-radar-bg p-3 text-sm text-slate-400">
            Version 1 shell: sample jobs, imported company path, local notes, and estimated match scoring.
          </div>
          <div className="mt-3 rounded-md border border-radar-line bg-radar-bg p-3 text-sm text-slate-400">
            Company data is loaded from the latest local JSON export. Job listings are loaded from the latest jobs
            snapshot. Run enrichment or job collection manually to refresh generated data.
            <button
              className="btn mt-3 w-full cursor-help"
              disabled
              title="Run the enrichment command from PowerShell to refresh company data."
            >
              Run Enrichment Manually
            </button>
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <header className="mb-6">
            <p className="text-sm uppercase tracking-wide text-radar-cyan">Banks, credit unions, jobs, applications</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">{pageTitle}</h2>
          </header>

          {activeTab === "Dashboard" ? (
            <Dashboard
              companies={companies}
              jobs={jobs}
              resume={resume}
              onResumeChange={setResume}
              onNavigate={navigate}
              onCompaniesReload={reloadCompanies}
            />
          ) : null}
          {activeTab === "Companies" ? (
            <Companies companies={companies} jobs={jobs} onViewCompanyJobs={viewCompanyJobs} />
          ) : null}
          {activeTab === "Job List" ? (
            <JobList
              jobs={jobs}
              resume={resume}
              selectedCompanyId={selectedCompanyId}
              onMarkApplied={markApplied}
              onNotInterested={(jobId) => updateJob(jobId, { notInterested: true })}
              onUpdateNotes={(jobId, notes) => updateJob(jobId, { notes })}
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
            <ResumeMatch jobs={jobs} resume={resume} onResumeChange={setResume} />
          ) : null}
        </main>
      </div>
    </div>
  );
}

export default App;

async function loadJson<T>(url: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) return fallback;
    const data = (await response.json()) as T;
    return Array.isArray(data) && data.length === 0 ? fallback : data;
  } catch {
    return fallback;
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
