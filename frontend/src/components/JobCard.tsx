import { Ban, CheckCircle2, ExternalLink, FileText } from "lucide-react";
import type { Job } from "../types/Job";
import { MatchScoreBadge } from "./MatchScoreBadge";
import { formatMoney, notListed } from "../utils/stats";

interface JobCardProps {
  job: Job;
  onMarkApplied: (jobId: string) => Promise<boolean>;
  onNotInterested: (jobId: string) => Promise<boolean>;
  onUpdateNotes: (jobId: string, notes: string) => Promise<boolean>;
  updating: boolean;
  onViewDetails?: (job: Job) => void;
  onViewCompany?: (companyName: string) => void;
  canAdminister: boolean;
}

export function JobCard({ job, onMarkApplied, onNotInterested, onUpdateNotes, updating, onViewDetails, onViewCompany, canAdminister }: JobCardProps) {
  return (
    <article className={`card p-4 ${job.notInterested ? "opacity-55" : ""}`}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <button
            className="text-left text-sm text-radar-highlight underline-offset-4 hover:text-white hover:underline"
            onClick={() => onViewCompany?.(job.companyName)}
            type="button"
          >
            {job.companyName}
          </button>
          <h3 className="mt-1 text-lg font-semibold text-white">{job.title}</h3>
          <p className="mt-2 text-sm text-slate-400">{job.descriptionSnippet || "Not listed"}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <span className="badge border-radar-line text-slate-200">{notListed(job.workType)}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.roleType || "UNKNOWN")}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.location)}</span>
            <span className="badge border-radar-line text-slate-200">{notListed(job.jobPlatform)}</span>
            <MatchScoreBadge score={job.matchScore} recommendation={job.matchLabel} status={job.matchStatus} />
          </div>
          {job.matchScore !== null ? (
            <p className="mt-2 text-xs text-slate-500">
              This is an estimated resume/job fit score, not a hiring prediction.
            </p>
          ) : null}
        </div>
        <div className="min-w-52 text-sm text-slate-300">
          <p>Pay: {formatPay(job)}</p>
          <p>Pay period: {notListed(job.payPeriod)}</p>
          <p>Posted: {notListed(job.postedDate)}</p>
          <p>Status: {job.applied ? job.applicationStatus : "Not applied"}</p>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {canAdminister ? <button className="btn" type="button" disabled={updating} onClick={() => void onMarkApplied(job.id)}>
          <CheckCircle2 size={16} />
          {job.applied ? "Applied" : "Mark Applied"}
        </button> : null}
        {canAdminister ? <button className="btn" type="button" disabled={updating} onClick={() => void onNotInterested(job.id)}>
          <Ban size={16} />
          Not Interested
        </button> : null}
        <button className="btn" type="button" onClick={() => onViewDetails?.(job)}>
          <FileText size={16} />
          View Details
        </button>
        <a className="btn" href={job.sourceUrl} target="_blank" rel="noreferrer noopener">
          <ExternalLink size={16} />
          Source
        </a>
      </div>
      {canAdminister ? <textarea
        key={`${job.id}-${job.notes}`}
        className="field mt-4 min-h-20"
        placeholder="Notes"
        defaultValue={job.notes}
        disabled={updating}
        onBlur={(event) => {
          const input = event.currentTarget;
          if (input.value === job.notes) return;
          void onUpdateNotes(job.id, input.value).then((saved) => { if (!saved) input.value = job.notes; });
        }}
      /> : null}
      {updating ? <p className="mt-2 text-xs text-slate-400" role="status">Saving application changes...</p> : null}
    </article>
  );
}

function formatPay(job: Job): string {
  if (job.payText?.trim()) return job.payText;
  if (job.payMin !== null && job.payMax !== null) return `${formatMoney(job.payMin)} - ${formatMoney(job.payMax)}`;
  if (job.payMin !== null) return `From ${formatMoney(job.payMin)}`;
  if (job.payMax !== null) return `Up to ${formatMoney(job.payMax)}`;
  return "Not listed";
}
