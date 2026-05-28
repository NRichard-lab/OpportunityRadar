import { BriefcaseBusiness, ExternalLink } from "lucide-react";
import type { Company } from "../types/Company";
import { notListed } from "../utils/stats";

interface CompanyCardProps {
  company: Company;
  appliedCount: number;
  jobCount: number;
  onViewJobs: (companyId: string) => void;
}

export function CompanyCard({ company, appliedCount, jobCount, onViewJobs }: CompanyCardProps) {
  return (
    <details className="card p-4">
      <summary className="cursor-pointer list-none">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <p className="font-semibold text-white">{company.name}</p>
            <p className="text-sm text-slate-400">
              {notListed(company.city)}, {notListed(company.state)} - {notListed(company.jobPlatform)}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <span className="badge border-radar-line text-slate-200">{company.searchStatus}</span>
            <span className="badge border-radar-line text-slate-200">{company.confidence}% confidence</span>
            <span className="badge border-radar-line text-slate-200">{appliedCount} applied</span>
          </div>
        </div>
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

function Info({ label, value, link, wide }: { label: string; value: string; link?: boolean; wide?: boolean }) {
  const display = notListed(value);
  return (
    <div className={wide ? "md:col-span-2" : ""}>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      {link && value ? (
        <a className="mt-1 inline-flex items-center gap-1 break-all text-radar-cyan" href={value} target="_blank">
          {display}
          <ExternalLink size={13} />
        </a>
      ) : (
        <p className="mt-1 text-slate-300">{display}</p>
      )}
    </div>
  );
}
