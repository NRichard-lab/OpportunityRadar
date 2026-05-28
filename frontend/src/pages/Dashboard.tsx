import { useState } from "react";
import { ArrowRight, ExternalLink, Search } from "lucide-react";
import type { Company } from "../types/Company";
import type { Job } from "../types/Job";
import type { ResumeProfile } from "../types/ResumeProfile";
import { ResumeUpload } from "../components/ResumeUpload";
import { StatCard } from "../components/StatCard";
import { MatchScoreBadge } from "../components/MatchScoreBadge";
import { buildStats, formatMoney, notListed } from "../utils/stats";
import { compareResumeToJob } from "../utils/resumeMatch";

interface DashboardProps {
  companies: Company[];
  jobs: Job[];
  resume: ResumeProfile | null;
  onResumeChange: (resume: ResumeProfile) => void;
  onNavigate: (tab: string) => void;
  onCompaniesReload: () => Promise<void>;
}

export function Dashboard({ companies, jobs, resume, onResumeChange, onNavigate, onCompaniesReload }: DashboardProps) {
  const stats = buildStats(companies, jobs);
  const comparisonJob = jobs[0];
  const comparison = comparisonJob ? compareResumeToJob(resume, comparisonJob) : null;
  const [discoveryLimit, setDiscoveryLimit] = useState(10);
  const [discoveryCompany, setDiscoveryCompany] = useState("");
  const [useBrowserDiscovery, setUseBrowserDiscovery] = useState(true);
  const [dryRun, setDryRun] = useState(false);
  const [discoveryStatus, setDiscoveryStatus] = useState("Ready");
  const [lastRunSummary, setLastRunSummary] = useState("");
  const [isDiscoveryRunning, setIsDiscoveryRunning] = useState(false);

  const runDiscovery = async () => {
    setIsDiscoveryRunning(true);
    setDiscoveryStatus("Running discovery...");
    setLastRunSummary("");
    try {
      const response = await fetch("http://127.0.0.1:8000/api/fill-missing-job-boards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          limit: discoveryLimit,
          company: discoveryCompany.trim() || null,
          useBrowserDiscovery,
          dryRun,
        }),
      });
      const result = await response.json();
      if (!response.ok || result.status === "failed") {
        throw new Error(result.message || "Discovery failed.");
      }
      setDiscoveryStatus("Completed");
      setLastRunSummary(formatDiscoverySummary(result));
      await onCompaniesReload();
    } catch (error) {
      setDiscoveryStatus("Failed");
      setLastRunSummary(error instanceof Error ? error.message : "Discovery failed.");
    } finally {
      setIsDiscoveryRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Companies tracked" value={stats.totalCompanies} />
        <StatCard label="Jobs found" value={stats.totalJobs} />
        <StatCard label="Jobs applied for" value={stats.jobsAppliedFor} />
        <StatCard label="Response rate" value={stats.responseRate === null ? "Not listed" : `${stats.responseRate}%`} />
      </section>
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="IC jobs" value={stats.byRoleType.find((item) => item.label === "IC")?.count ?? 0} />
        <StatCard label="MGR jobs" value={stats.byRoleType.find((item) => item.label === "MGR")?.count ?? 0} />
        <StatCard label="EXEC jobs" value={stats.byRoleType.find((item) => item.label === "EXEC")?.count ?? 0} />
        <StatCard label="UNKNOWN jobs" value={stats.byRoleType.find((item) => item.label === "UNKNOWN")?.count ?? 0} />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr_1.2fr]">
        <ResumeUpload resume={resume} onResumeChange={onResumeChange} />
        <div className="card p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-white">Resume Fit Score</p>
              <p className="mt-1 text-sm text-slate-400">
                This is an estimated resume/job fit score, not a hiring prediction.
              </p>
            </div>
            {comparison ? (
              <MatchScoreBadge score={comparison.score} recommendation={comparison.recommendation} />
            ) : null}
          </div>
          {comparison && comparisonJob ? (
            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <div>
                <p className="text-xs uppercase tracking-wide text-slate-500">Compared job</p>
                <p className="mt-1 font-medium text-white">{comparisonJob.title}</p>
                <p className="text-sm text-slate-400">{comparisonJob.companyName}</p>
              </div>
              <div>
                <p className="text-xs uppercase tracking-wide text-slate-500">Recommended action</p>
                <p className="mt-1 font-medium text-white">{comparison.recommendation}</p>
              </div>
              <KeywordList title="Matched skills" items={comparison.matchedKeywords} />
              <KeywordList title="Missing skills" items={comparison.missingKeywords} />
              <div className="md:col-span-2 grid gap-3 md:grid-cols-2">
                <Info label="Experience alignment" value={comparison.experienceAlignment} />
                <Info label="Title alignment" value={comparison.titleAlignment} />
              </div>
            </div>
          ) : (
            <p className="mt-4 text-sm text-slate-400">Add job data to score resume fit.</p>
          )}
        </div>
      </section>

      <section className="panel p-5">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <h2 className="text-lg font-semibold text-white">Job Board Discovery</h2>
            <p className="mt-1 max-w-3xl text-sm text-slate-400">
              This runs local browser discovery for companies missing a Job Board URL. Start with a small limit.
            </p>
          </div>
          <button className="btn btn-primary" onClick={runDiscovery} disabled={isDiscoveryRunning}>
            <Search size={16} />
            {isDiscoveryRunning ? "Running..." : "Find Missing Job Board URLs"}
          </button>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="text-sm text-slate-300">
            Limit
            <input
              className="field mt-1"
              min={0}
              type="number"
              value={discoveryLimit}
              onChange={(event) => setDiscoveryLimit(Number(event.target.value))}
            />
          </label>
          <label className="text-sm text-slate-300">
            Company
            <input
              className="field mt-1"
              placeholder="Optional"
              value={discoveryCompany}
              onChange={(event) => setDiscoveryCompany(event.target.value)}
            />
          </label>
          <label className="flex items-center gap-2 rounded-md border border-radar-line bg-radar-bg px-3 py-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={useBrowserDiscovery}
              onChange={(event) => setUseBrowserDiscovery(event.target.checked)}
            />
            Use Browser Discovery
          </label>
          <label className="flex items-center gap-2 rounded-md border border-radar-line bg-radar-bg px-3 py-2 text-sm text-slate-300">
            <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />
            Dry Run
          </label>
        </div>
        <div className="mt-4 rounded-md bg-radar-bg p-3 text-sm text-slate-300">
          <p>Status: {discoveryStatus}</p>
          <p className="mt-1 text-slate-400">
            {lastRunSummary || "Last run summary will appear here. Data updated. Refresh the page to see the latest company data if live reload does not appear immediately."}
          </p>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-2">
        <div className="panel p-5">
          <Header title="Companies" action="Full Companies tab" onClick={() => onNavigate("Companies")} />
          <p className="mb-4 text-sm text-slate-400">{companies.length} companies currently in the database.</p>
          <div className="space-y-3">
            {companies.slice(0, 5).map((company) => (
              <div className="flex items-center justify-between gap-3 rounded-md bg-radar-bg p-3" key={company.id}>
                <div>
                  <p className="font-medium text-white">{company.name}</p>
                  <p className="text-sm text-slate-400">
                    {notListed(company.state)} - {notListed(company.jobPlatform)}
                  </p>
                </div>
                <span className="badge border-radar-line text-slate-200">{company.searchStatus}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="panel p-5">
          <Header title="Current jobs" action="Full Job List tab" onClick={() => onNavigate("Job List")} />
          <div className="space-y-3">
            {jobs.slice(0, 10).map((job) => (
              <div className="rounded-md bg-radar-bg p-3" key={job.id}>
                <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                  <div>
                    <p className="font-medium text-white">{job.title}</p>
                    <p className="text-sm text-radar-cyan">{job.companyName}</p>
                  </div>
                  <a className="text-sm text-slate-300 hover:text-radar-cyan" href={job.sourceUrl} target="_blank">
                    Source <ExternalLink className="inline" size={13} />
                  </a>
                </div>
                <p className="mt-2 text-sm text-slate-400">
                  {notListed(job.workType)} - {notListed(job.location)} - {job.payText || "Not listed"} - Posted{" "}
                  {notListed(job.postedDate)}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Average minimum pay" value={formatMoney(stats.averageMinPay)} />
        <StatCard label="Average maximum pay" value={formatMoney(stats.averageMaxPay)} />
        <StatCard label="Jobs with pay listed" value={stats.jobsWithPay} />
        <StatCard label="Jobs missing pay" value={stats.jobsMissingPay} />
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {stats.averagePayByRoleType.map((item) => (
          <StatCard key={item.label} label={`Avg pay ${item.label}`} value={formatMoney(item.count)} />
        ))}
      </section>

      <section className="grid gap-6 xl:grid-cols-3">
        <ListPanel title="Jobs by work type" items={stats.byWorkType} />
        <ListPanel title="Top job platforms" items={stats.platforms} />
        <ListPanel title="Top title keywords" items={stats.titleKeywords} footer={`${stats.jobsAddedLast7Days} jobs added in the last 7 days`} />
      </section>
    </div>
  );
}

function formatDiscoverySummary(result: Record<string, unknown>) {
  const values = [
    `Reviewed: ${result.rowsReviewed ?? "Not listed"}`,
    `Skipped: ${result.rowsSkipped ?? "Not listed"}`,
    `Attempted: ${result.missingRowsAttempted ?? "Not listed"}`,
    `Found: ${result.jobBoardUrlsFound ?? "Not listed"}`,
    `Not found: ${result.notFound ?? "Not listed"}`,
    `Errors: ${result.errors ?? "Not listed"}`,
  ];
  return values.join(" | ");
}

function Header({ title, action, onClick }: { title: string; action: string; onClick: () => void }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="text-lg font-semibold text-white">{title}</h2>
      <button className="btn" onClick={onClick}>
        {action}
        <ArrowRight size={15} />
      </button>
    </div>
  );
}

function KeywordList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-slate-500">{title}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {items.length ? items.map((item) => <span className="badge border-radar-line text-slate-200" key={item}>{item}</span>) : <span className="text-sm text-slate-400">Not listed</span>}
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-radar-bg p-3">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-sm text-slate-300">{value}</p>
    </div>
  );
}

function ListPanel({ title, items, footer }: { title: string; items: { label: string; count: number }[]; footer?: string }) {
  return (
    <div className="card p-5">
      <h3 className="font-semibold text-white">{title}</h3>
      <div className="mt-4 space-y-3">
        {items.length ? items.map((item) => (
          <div className="flex items-center justify-between text-sm" key={item.label}>
            <span className="text-slate-300">{item.label}</span>
            <span className="font-medium text-white">{item.count}</span>
          </div>
        )) : <p className="text-sm text-slate-400">Not listed</p>}
      </div>
      {footer ? <p className="mt-4 text-sm text-slate-400">{footer}</p> : null}
    </div>
  );
}
