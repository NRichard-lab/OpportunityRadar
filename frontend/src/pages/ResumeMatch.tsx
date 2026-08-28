import { useMemo, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import type { Job } from "../types/Job";
import type { DataLoadStatus } from "../types/DataLoadState";
import type { MaintenanceJobsState } from "../types/Maintenance";
import type { ResumeProfile } from "../types/ResumeProfile";
import { JobDetailsModal } from "../components/JobDetailsModal";
import { MatchScoreBadge } from "../components/MatchScoreBadge";
import { ResumeUpload } from "../components/ResumeUpload";
import { DataStatePanel } from "../components/DataStatePanel";
import { isCurrentJobRecord } from "../utils/jobRecords";
import { isUtilityRunResponse } from "../runtimeSchemas";
import { ApiError, apiJson, userMessage } from "../api";

interface ResumeMatchProps {
  jobs: Job[]; resume: ResumeProfile | null; onResumeChange: (resume: ResumeProfile) => Promise<void>;
  maintenance: MaintenanceJobsState; onMaintenanceRefresh: () => Promise<MaintenanceJobsState>;
  onRematch: (jobId: string) => Promise<Job>;
  utilitiesEnabled: boolean;
  dataStatus: DataLoadStatus;
  dataError: string;
  onRetry: () => void;
}

export function ResumeMatch({ jobs, resume, onResumeChange, maintenance, onMaintenanceRefresh, onRematch, utilitiesEnabled, dataStatus, dataError, onRetry }: ResumeMatchProps) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("All statuses");
  const [sort, setSort] = useState("Highest match");
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const matchJob = maintenance.jobs.find((item) => item.jobKey === "rematch-all-jobs");
  const run = matchJob?.activeRun || matchJob?.latestRun || null;
  const resumeNeedsReupload = Boolean(resume && (
    resume.notes?.toLowerCase().includes("parsing is stubbed") || resume.yearsExperienceSummary?.toLowerCase().startsWith("todo: parse")
  ));

  const visibleJobs = useMemo(() => jobs.filter(isCurrentJobRecord).filter((job) => {
    const searchable = `${job.companyName} ${job.title}`.toLowerCase();
    return searchable.includes(query.toLowerCase()) && (status === "All statuses" || (job.matchStatus || "Not Matched") === status);
  }).sort((left, right) => {
    if (sort === "Company") return left.companyName.localeCompare(right.companyName) || left.title.localeCompare(right.title);
    if (sort === "Lowest match") return (left.matchScore ?? 101) - (right.matchScore ?? 101);
    return (right.matchScore ?? -1) - (left.matchScore ?? -1) || left.companyName.localeCompare(right.companyName);
  }), [jobs, query, sort, status]);

  const startRematch = async () => {
    if (!utilitiesEnabled) {
      setError("Bulk rematching is disabled for the initial production release.");
      return;
    }
    setStarting(true); setError("");
    try {
      const startedRun = await apiJson<unknown>("/maintenance/jobs/rematch-all-jobs/run", { method: "POST" }, "Rematch All could not be started.");
      if (!isUtilityRunResponse(startedRun) || startedRun.action !== "rematch-all-jobs" || startedRun.job_key !== "rematch-all-jobs") {
        throw new ApiError("Rematch All could not be started. The server returned an invalid response.");
      }
      await onMaintenanceRefresh();
    } catch (startError) { setError(userMessage(startError, "Rematch All could not be started.")); }
    finally { setStarting(false); }
  };

  const bulkRunning = Boolean(matchJob?.running);
  const summary = run?.summary || {};
  const processed = Number(summary.jobsProcessed ?? run?.current ?? 0);
  const failed = Number(summary.jobsFailed ?? 0);
  const remaining = Number(summary.jobsRemaining ?? Math.max(0, (run?.total || 0) - processed));

  if (dataStatus === "loading" || dataStatus === "error") {
    return <DataStatePanel status={dataStatus} error={dataError} loadingLabel="Loading resume and match data..." onRetry={onRetry} />;
  }

  return <div className="space-y-5">
    <ResumeUpload resume={resume} onResumeChange={onResumeChange} />
    <section className="panel p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div><h2 className="text-lg font-semibold text-white">Match Current Jobs</h2><p className="mt-1 max-w-2xl text-sm text-slate-400">Use the active resume to calculate and save a consistent fit score for every current job. Existing scores remain available until a resume or job change requires rematching.</p></div>
        <button className="btn btn-primary shrink-0" type="button" title={!utilitiesEnabled ? "Bulk rematching is disabled for the initial production release." : undefined} disabled={!utilitiesEnabled || !resume || resumeNeedsReupload || bulkRunning || starting} onClick={() => void startRematch()}>{bulkRunning || starting ? <LoaderCircle className="animate-spin" size={16} /> : <RefreshCw size={16} />}{bulkRunning ? "Matching..." : utilitiesEnabled ? "Rematch All Jobs" : "Rematch unavailable"}</button>
      </div>
      {!resume ? <p className="mt-4 text-sm text-amber-200">Upload an active resume before matching jobs.</p> : null}
      {resumeNeedsReupload ? <p className="mt-4 text-sm text-amber-200">Re-upload this resume so Opportunity Radar can read the actual PDF or DOCX contents before matching jobs.</p> : null}
      {!utilitiesEnabled ? <p className="mt-4 text-sm text-slate-400">Bulk rematching is disabled for the initial production release.</p> : null}
      {error ? <p className="mt-4 text-sm text-red-300" role="alert">{error}</p> : null}
      {run ? <div className="mt-4 rounded-md border border-radar-line bg-radar-bg p-4" aria-live="polite">
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-slate-300"><span>Total: {run.total || processed}</span><span>Completed: {processed}</span><span>Failed: {failed}</span><span>Remaining: {remaining}</span><span>Elapsed: {formatDuration(run.runtimeSeconds)}</span>{run.total ? <span>{run.progress ?? 0}% complete</span> : null}</div>
        {run.currentCompany ? <p className="mt-2 text-sm text-slate-400">Current job: {run.currentCompany}</p> : null}
        {run.currentMessage ? <p className="mt-2 text-sm text-slate-300">{run.currentMessage}</p> : null}
        {run.total ? <div className="mt-3 h-2 overflow-hidden rounded-full bg-radar-panel"><div className="h-full bg-radar-accent transition-all" style={{ width: `${run.progress ?? 0}%` }} /></div> : null}
      </div> : null}
    </section>

    <section className="panel overflow-hidden">
      <header className="grid gap-3 border-b border-radar-line p-4 md:grid-cols-[1fr_220px_200px]">
        <input className="field" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search company or job title" aria-label="Search matched jobs" />
        <select className="field" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter match status"><option>All statuses</option><option>Matched</option><option>Needs Rematch</option><option>Not Matched</option><option>Match Failed</option></select>
        <select className="field" value={sort} onChange={(event) => setSort(event.target.value)} aria-label="Sort matched jobs"><option>Highest match</option><option>Lowest match</option><option>Company</option></select>
      </header>
      <div className="divide-y divide-radar-line">
        {visibleJobs.map((job) => <button className="grid w-full gap-3 p-4 text-left transition hover:bg-radar-bg/60 md:grid-cols-[1fr_auto] md:items-center" type="button" key={job.id} onClick={() => setSelectedJob(job)}>
          <span className="min-w-0"><span className="block break-words font-semibold text-radar-highlight">{job.title}</span><span className="mt-1 block text-sm text-slate-400">{job.companyName}</span><span className="mt-1 block text-xs text-slate-500">Last matched: {formatTimestamp(job.matchedAt)}</span></span>
          <MatchScoreBadge score={job.matchScore} recommendation={job.matchLabel} status={job.matchStatus} />
        </button>)}
        {!visibleJobs.length ? <p className="p-8 text-center text-slate-400">No current jobs match these filters.</p> : null}
      </div>
    </section>
    <JobDetailsModal job={selectedJob} onClose={() => setSelectedJob(null)} onRematch={onRematch} />
  </div>;
}

function formatTimestamp(value?: string): string { return value ? new Date(value).toLocaleString() : "Not Matched"; }
function formatDuration(seconds: number | null): string { if (seconds === null) return "Not available"; const total = Math.max(0, Math.round(seconds)); return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${total % 60}s`; }
