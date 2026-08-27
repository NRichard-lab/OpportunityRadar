import { useState } from "react";
import { ArrowRight } from "lucide-react";
import type { Company } from "../types/Company";
import type { Job } from "../types/Job";
import { JobDetailsModal } from "../components/JobDetailsModal";
import { StatCard } from "../components/StatCard";
import { isCurrentJobRecord, newestJobFirst } from "../utils/jobRecords";

interface DashboardProps {
  companies: Company[];
  jobs: Job[];
  loaded: boolean;
  onNavigate: (tab: string) => void;
  onRematch: (jobId: string) => Promise<Job>;
}

export function Dashboard({ companies, jobs, loaded, onNavigate, onRematch }: DashboardProps) {
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  if (!loaded) return <div className="panel p-8 text-center text-slate-400">Loading dashboard data...</div>;
  const currentJobs = jobs.filter(isCurrentJobRecord).sort(newestJobFirst);
  const recentJobs = currentJobs.slice(0, 5);
  const applications = jobs.filter((job) => job.applied).length;

  return <div className="space-y-6">
    <section className="grid gap-4 sm:grid-cols-3" aria-label="Opportunity summary">
      <StatCard label="Companies" value={companies.length} onClick={() => onNavigate("Companies")} />
      <StatCard label="Current Jobs" value={currentJobs.length} onClick={() => onNavigate("Job List")} />
      <StatCard label="Applications" value={applications} onClick={() => onNavigate("Jobs Applied For")} />
    </section>

    <section className="panel overflow-hidden">
      <header className="flex flex-col gap-3 border-b border-radar-line p-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Recent Jobs</h2>
          <p className="mt-1 text-sm text-slate-400">Newest active opportunities currently stored in Opportunity Radar.</p>
        </div>
        <button className="btn shrink-0" type="button" onClick={() => onNavigate("Job List")}>View All Jobs<ArrowRight size={16} /></button>
      </header>
      {recentJobs.length ? <div className="divide-y divide-radar-line">{recentJobs.map((job) => <RecentJob key={job.id} job={job} onOpen={() => setSelectedJob(job)} />)}</div> : <div className="p-8 text-center text-slate-400">No current jobs are available. Run job collection from Utilities.</div>}
    </section>
    <JobDetailsModal job={selectedJob} onClose={() => setSelectedJob(null)} onRematch={onRematch} />
  </div>;
}

function RecentJob({ job, onOpen }: { job: Job; onOpen: () => void }) {
  const details = [
    listedValue(job.location, "Location Not Listed"),
    formatPay(job),
    listedValue(job.workType, "Work Type Not Listed"),
    formatPostedDate(job.postedDate),
  ];
  return <article className="p-4 transition hover:bg-radar-bg/60 sm:px-5">
    <div className="min-w-0">
      <h3><button className="break-words text-left font-semibold text-radar-highlight underline-offset-4 transition hover:text-white hover:underline focus:outline-none focus:ring-2 focus:ring-radar-accent/60" type="button" onClick={onOpen}>{job.title || "Title Not Listed"}</button></h3>
      <p className="mt-1 text-sm font-medium text-radar-highlight">{job.companyName || "Company Not Listed"}</p>
      <p className="mt-2 text-sm text-slate-400">{details.join(" · ")}</p>
    </div>
  </article>;
}

function formatPay(job: Job): string {
  if (isListed(job.payText)) return job.payText;
  if (job.payMin !== null && job.payMax !== null) return `${formatMoney(job.payMin)}-${formatMoney(job.payMax)}`;
  if (job.payMin !== null) return `From ${formatMoney(job.payMin)}`;
  if (job.payMax !== null) return `Up to ${formatMoney(job.payMax)}`;
  return "Pay Not Listed";
}

function formatMoney(value: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);
}

function formatPostedDate(value: string): string {
  if (!isListed(value)) return "Posted Not Listed";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    const relative = value.match(/^\d+\+?\s+days?\s+ago$/i);
    const prefixedDate = value.match(/^on\s+(\d{1,2}\/\d{1,2}\/\d{4})/i);
    if (relative) return `Posted ${relative[0]}`;
    if (prefixedDate) return `Posted ${prefixedDate[1]}`;
    return "Posted Not Listed";
  }
  return `Posted ${parsed.toLocaleDateString([], { month: "short", day: "numeric", year: parsed.getFullYear() === new Date().getFullYear() ? undefined : "numeric" })}`;
}

function listedValue(value: string, fallback: string): string { return isListed(value) ? value : fallback; }
function isListed(value: string): boolean { return Boolean(value && value.trim().toLowerCase() !== "not listed"); }
