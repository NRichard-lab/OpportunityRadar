import { BriefcaseBusiness, ExternalLink, Pencil, RefreshCw, Trash2 } from "lucide-react";
import type { Company } from "../types/Company";
import { notListed } from "../utils/stats";

interface CompanyCardProps {
  company: Company;
  appliedCount: number;
  jobCount: number;
  onViewJobs: (companyId: string) => void;
  onEdit: (company: Company) => void;
  onDelete: (company: Company) => void;
  onRefresh: (company: Company) => void;
  refreshEnabled: boolean;
  isRefreshing?: boolean;
  refreshResult?: CompanyRefreshResult;
  forceOpen?: boolean;
}

export interface CompanyRefreshResult {
  status: "completed" | "partial" | "failed";
  companyName: string;
  companyMetadataChanged: boolean;
  totalJobsDiscovered: number;
  newJobs: number;
  updatedJobs: number;
  removedOrClosedJobs: number;
  warnings: string[];
  errors: string[];
}

export function CompanyCard({ company, appliedCount, jobCount, onViewJobs, onEdit, onDelete, onRefresh, refreshEnabled, isRefreshing, refreshResult, forceOpen }: CompanyCardProps) {
  return (
    <details className="card p-4" open={forceOpen || undefined}>
      <summary className="cursor-pointer list-none">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <p className="font-semibold text-white">{company.name}</p>
            <p className="text-sm text-slate-400">
              {notListed(company.city)}, {notListed(company.state)} - {notListed(company.jobPlatform)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="badge border-radar-line text-slate-200">
              <span className={`h-2 w-2 rounded-full ${jobCount > 0 ? "bg-emerald-400" : "bg-red-500"}`} aria-hidden="true" />
              Jobs: {jobCount}
            </span>
            <span className="badge border-radar-line text-slate-200">{company.searchStatus}</span>
            <span className="badge border-radar-line text-slate-200">{company.confidence}% confidence</span>
            <span className="badge border-radar-line text-slate-200">{appliedCount} applied</span>
            <button className="btn px-3 py-2" type="button" title={refreshEnabled ? `Refresh ${company.name}` : "Company refresh is disabled for the initial production release."} aria-label={refreshEnabled ? `Refresh ${company.name}` : `Refresh unavailable for ${company.name}`} disabled={!refreshEnabled || isRefreshing} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onRefresh(company); }}>
              <RefreshCw className={isRefreshing ? "animate-spin" : ""} size={16} />
              {isRefreshing ? "Refreshing..." : refreshEnabled ? "Refresh" : "Refresh unavailable"}
            </button>
            <button className="icon-btn" type="button" title={`Edit ${company.name}`} aria-label={`Edit ${company.name}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onEdit(company); }}>
              <Pencil size={16} />
            </button>
            <button className="icon-btn border-red-900 text-red-400 hover:border-red-500 hover:text-red-300" type="button" title={`Delete ${company.name}`} aria-label={`Delete ${company.name}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onDelete(company); }}>
              <Trash2 size={16} />
            </button>
          </div>
        </div>
        {refreshResult ? <RefreshResult result={refreshResult} /> : null}
      </summary>
      <div className="mt-4 grid gap-3 text-sm md:grid-cols-2">
        <Info label="Official website" value={company.officialWebsite} link />
        <Info label="Careers page" value={company.careersPageUrl} link />
        <Info label="Job board URL" value={company.jobBoardUrl} link />
        <Info label="Job board discovery" value={company.jobBoardDiscoveryMethod} />
        <Info label="Jobs RSS feed" value={company.jobsRssFeedUrl} link />
        <Info label="Feed found" value={company.feedFound ? "True" : "False"} />
        <Info label="Known website" value={company.knownWebsite} link />
        <Info label="Last checked" value={company.lastChecked} />
        <Info label="Notes" value={company.notes} wide />
      </div>
      <button className="btn mt-4" onClick={() => onViewJobs(company.id)}>
        <BriefcaseBusiness size={16} />
        View current jobs ({jobCount})
      </button>
    </details>
  );
}

function RefreshResult({ result }: { result: CompanyRefreshResult }) {
  const hasErrors = result.errors.length > 0 || result.status === "failed";
  return <div className={`mt-3 rounded-md border px-4 py-3 text-sm ${hasErrors ? "border-red-900 bg-red-950/40 text-red-200" : "border-emerald-800 bg-emerald-950/40 text-emerald-200"}`} role="status" onClick={(event) => { event.preventDefault(); event.stopPropagation(); }}>
    <p className="font-semibold">{hasErrors ? `${result.companyName} refresh incomplete` : `${result.companyName} refreshed`}</p>
    <ul className="mt-2 space-y-1">
      <li>{result.companyMetadataChanged ? "Company information updated" : "Company information checked; no changes needed"}</li>
      <li>{result.totalJobsDiscovered} jobs found</li>
      <li>{result.newJobs} new jobs added</li>
      <li>{result.updatedJobs} jobs updated</li>
      {result.removedOrClosedJobs ? <li>{result.removedOrClosedJobs} jobs removed or closed</li> : null}
      {result.errors.map((message) => <li key={message}>{message}</li>)}
      {result.warnings.map((message) => <li key={message}>{message}</li>)}
    </ul>
  </div>;
}

function Info({ label, value, link, wide }: { label: string; value: string; link?: boolean; wide?: boolean }) {
  const display = notListed(value);
  return (
    <div className={wide ? "md:col-span-2" : ""}>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      {link && value ? (
        <a className="mt-1 inline-flex items-center gap-1 break-all text-radar-highlight" href={value} target="_blank" rel="noreferrer noopener">
          {display}
          <ExternalLink size={13} />
        </a>
      ) : (
        <p className="mt-1 text-slate-300">{display}</p>
      )}
    </div>
  );
}
