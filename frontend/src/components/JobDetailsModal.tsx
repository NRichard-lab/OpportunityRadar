import { useEffect, useState } from "react";
import { ExternalLink, LoaderCircle, RefreshCw, X } from "lucide-react";
import type { Job } from "../types/Job";
import { MatchScoreBadge } from "./MatchScoreBadge";

interface JobDetailsModalProps { job: Job | null; onClose: () => void; onRematch?: (jobId: string) => Promise<Job>; }

export function JobDetailsModal({ job, onClose, onRematch }: JobDetailsModalProps) {
  const [displayJob, setDisplayJob] = useState<Job | null>(job);
  const [matching, setMatching] = useState(false);
  const [matchError, setMatchError] = useState("");
  useEffect(() => { setDisplayJob(job); setMatchError(""); }, [job]);
  if (!displayJob) return null;

  const rematch = async () => {
    if (!onRematch) return;
    setMatching(true); setMatchError("");
    try { setDisplayJob(await onRematch(displayJob.id)); }
    catch (error) { setMatchError(error instanceof Error ? error.message : "This job could not be matched."); }
    finally { setMatching(false); }
  };
  const details = displayJob.matchDetails || {};

  return <div className="fixed inset-0 z-40 grid place-items-center bg-black/70 p-3 md:p-4" role="presentation" onClick={onClose}>
    <section className="panel flex max-h-[85vh] min-h-0 w-full max-w-4xl flex-col overflow-hidden p-0" role="dialog" aria-modal="true" aria-labelledby="job-details-title" onClick={(event) => event.stopPropagation()}>
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-radar-line p-5">
        <div className="min-w-0"><p className="text-radar-highlight">{listed(displayJob.companyName)}</p><h2 id="job-details-title" className="break-words text-xl font-semibold text-white">{listed(displayJob.title)}</h2></div>
        <button className="icon-btn shrink-0" type="button" onClick={onClose} title="Close" aria-label="Close job details"><X size={18} /></button>
      </header>
      <div className="min-h-0 overflow-y-auto p-5">
        <div className="grid gap-3 break-words text-sm text-slate-300 md:grid-cols-2">
          <p>Company: {listed(displayJob.companyName)}</p><p>Location: {listed(displayJob.location)}</p><p>Work type: {listed(displayJob.workType)}</p><p>Role type: {listed(displayJob.roleType)}</p>
          <p className="md:col-span-2">Role type reason: {listed(displayJob.roleTypeReason)}</p><p>Source/platform: {listed(displayJob.jobPlatform)}</p><p>Pay period: {listed(displayJob.payPeriod)}</p>
          <p>Pay minimum: {displayJob.payMin ?? "Not Listed"}</p><p>Pay maximum: {displayJob.payMax ?? "Not Listed"}</p><p>Pay: {listed(displayJob.payText)}</p><p>Posted: {listed(displayJob.postedDate)}</p>
          <p className="md:col-span-2">Original job URL: {displayJob.sourceUrl ? <a className="break-all text-radar-highlight hover:text-white hover:underline" href={displayJob.sourceUrl} target="_blank" rel="noreferrer">{displayJob.sourceUrl}</a> : "Not Listed"}</p>
        </div>
        <section className="mt-5 rounded-md border border-radar-line bg-radar-bg p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div><h3 className="text-sm font-semibold text-white">Resume Match</h3><p className="mt-1 text-xs text-slate-400">Last matched: {formatTimestamp(displayJob.matchedAt)}</p></div>
            <div className="flex flex-wrap items-center gap-2"><MatchScoreBadge score={displayJob.matchScore} recommendation={displayJob.matchLabel} status={displayJob.matchStatus} />{onRematch ? <button className="btn" type="button" disabled={matching} onClick={() => void rematch()}>{matching ? <LoaderCircle className="animate-spin" size={16} /> : <RefreshCw size={16} />}{matching ? "Matching..." : "Rematch"}</button> : null}</div>
          </div>
          {details.summary ? <p className="mt-3 text-sm text-slate-300">{details.summary}</p> : null}
          <KeywordList title="Matched keywords" items={details.matchedKeywords} /><KeywordList title="Missing keywords" items={details.missingKeywords} />
          {matchError || displayJob.matchError ? <p className="mt-3 text-sm text-red-300" role="alert">{matchError || displayJob.matchError}</p> : null}
        </section>
        <section className="mt-5 min-h-0"><h3 className="text-sm font-semibold text-white">Job Description</h3><p className="mt-3 max-h-[42vh] overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-radar-line bg-radar-bg p-4 text-sm leading-6 text-slate-300">{listed(displayJob.description)}</p></section>
        {displayJob.sourceUrl ? <div className="mt-5 flex justify-end"><a className="btn btn-primary" href={displayJob.sourceUrl} target="_blank" rel="noreferrer">Open Job Posting<ExternalLink size={16} /></a></div> : null}
      </div>
    </section>
  </div>;
}

function KeywordList({ title, items }: { title: string; items?: string[] }) { if (!items?.length) return null; return <div className="mt-3"><p className="text-xs font-medium text-slate-400">{title}</p><div className="mt-2 flex flex-wrap gap-2">{items.map((item) => <span className="badge border-radar-line" key={item}>{item}</span>)}</div></div>; }
function listed(value: string | null | undefined): string { return value && value.trim() ? value : "Not Listed"; }
function formatTimestamp(value?: string): string { return value ? new Date(value).toLocaleString() : "Not Matched"; }
