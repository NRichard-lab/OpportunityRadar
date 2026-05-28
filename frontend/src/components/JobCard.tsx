import { Ban, CheckCircle2, ExternalLink, FileText } from "lucide-react";
import type { Job } from "../types/Job";
import { MatchScoreBadge } from "./MatchScoreBadge";
import { formatMoney, notListed } from "../utils/stats";

interface JobCardProps {
  job: Job;
  onMarkApplied: (jobId: string) => void;
  onNotInterested: (jobId: string) => void;
  onUpdateNotes: (jobId: string, notes: string) => void;
  onViewDetails?: (job: Job) => void;
}

export function JobCard({ job, onMarkApplied, onNotInterested, onUpdateNotes, onViewDetails }: JobCardProps) {
  return (
    <article className={`card p-4 ${job.notInterested ? "opacity-55" : ""}`}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <p className="text-sm text-radar-cyan">{job.companyName}</p>
          <h3 className="mt-1 text-lg font-semibold text-white">{job.title}</h3>
          <p className="mt-2 text-sm text-slate-400">{job.descriptionSnippet || "Not listed"}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <span className="badge border-radar-line text-slate-200">{notListed(job.workType)}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.roleType || "UNKNOWN")}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.location)}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.jobPlatform)}</span>
            <MatchScoreBadge score={job.matchScore} />
          </div>
          {job.matchScore !== null ? (
            <p className="mt-2 text-xs text-slate-500">
              This is an estimated resume/job fit score, not a hiring prediction.
            </p>
          ) : null}
        </div>
        <div className="min-w-52 text-sm text-slate-300">
          <p>Pay: {job.payText || `${formatMoney(job.payMin)} - ${formatMoney(job.payMax)}`}</p>
          <p>Pay period: {notListed(job.payPeriod)}</p>
          <p>Posted: {notListed(job.postedDate)}</p>
          <p>Status: {job.applied ? job.applicationStatus : "Not applied"}</p>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <button className="btn" onClick={() => onMarkApplied(job.id)}>
          <CheckCircle2 size={16} />
          {job.applied ? "Applied" : "Mark Applied"}
        </button>
        <button className="btn" onClick={() => onNotInterested(job.id)}>
          <Ban size={16} />
          Not Interested
        </button>
        <button className="btn" onClick={() => onViewDetails?.(job)}>
          <FileText size={16} />
          View Details
        </button>
        <a className="btn" href={job.sourceUrl} target="_blank">
          <ExternalLink size={16} />
          Source
        </a>
      </div>
      <textarea
        className="field mt-4 min-h-20"
        placeholder="Notes"
        value={job.notes}
        onChange={(event) => onUpdateNotes(job.id, event.target.value)}
      />
    </article>
  );
}
