import { useEffect, useMemo, useState } from "react";
import type { Job, RoleType, WorkType } from "../types/Job";
import { JobCard } from "../components/JobCard";
import { JobDetailsModal } from "../components/JobDetailsModal";
import { DataStatePanel } from "../components/DataStatePanel";
import type { DataLoadStatus } from "../types/DataLoadState";
import { isCurrentJobRecord } from "../utils/jobRecords";

interface JobListProps {
  jobs: Job[];
  dataStatus: DataLoadStatus;
  dataError: string;
  onRetry: () => void;
  onRematch: (jobId: string) => Promise<Job>;
  onMarkApplied: (jobId: string) => Promise<boolean>;
  onNotInterested: (jobId: string) => Promise<boolean>;
  onUpdateNotes: (jobId: string, notes: string) => Promise<boolean>;
  pendingApplicationIds: Set<string>;
  selectedCompanyId?: string;
  onViewCompany?: (companyName: string) => void;
}

export function JobList({ jobs, dataStatus, dataError, onRetry, onRematch, onMarkApplied, onNotInterested, onUpdateNotes, pendingApplicationIds, selectedCompanyId, onViewCompany }: JobListProps) {
  const [query, setQuery] = useState("");
  const [workType, setWorkType] = useState<WorkType | "All work types">("All work types");
  const [companyFilter, setCompanyFilter] = useState("All companies");
  const [locationFilter, setLocationFilter] = useState("All locations");
  const [platform, setPlatform] = useState("All platforms");
  const [roleType, setRoleType] = useState<RoleType | "All role types">("All role types");
  const [applied, setApplied] = useState("All application statuses");
  const [minPay, setMinPay] = useState("");
  const [sort, setSort] = useState("Newest");
  const [pageSize, setPageSize] = useState(25);
  const [currentPage, setCurrentPage] = useState(1);
  const [detailsJob, setDetailsJob] = useState<Job | null>(null);

  const validJobs = useMemo(() => jobs.filter(isCurrentJobRecord), [jobs]);
  const hiddenJobs = jobs.length - validJobs.length;

  useEffect(() => {
    setCurrentPage(1);
  }, [applied, companyFilter, locationFilter, minPay, pageSize, platform, query, roleType, selectedCompanyId, sort, workType]);

  const filtered = useMemo(() => {
    const minimumPay = Number(minPay) || 0;
    return [...validJobs]
      .filter((job) => {
        const searchable = `${job.companyName} ${job.title} ${job.location} ${job.jobPlatform}`.toLowerCase();
        return (
          (!selectedCompanyId || job.companyId === selectedCompanyId) &&
          searchable.includes(query.toLowerCase()) &&
          (workType === "All work types" || job.workType === workType) &&
          (companyFilter === "All companies" || job.companyName === companyFilter) &&
          (locationFilter === "All locations" || job.location === locationFilter) &&
          (roleType === "All role types" || (job.roleType || "UNKNOWN") === roleType) &&
          (platform === "All platforms" || job.jobPlatform === platform) &&
          matchesApplicationFilter(job, applied) &&
          ((job.payMax ?? job.payMin ?? 0) >= minimumPay)
        );
      })
      .sort((a, b) => {
        if (sort === "Highest pay") return (b.payMax ?? 0) - (a.payMax ?? 0);
        if (sort === "Best resume fit") return (b.matchScore ?? 0) - (a.matchScore ?? 0);
        if (sort === "Role type") return roleSortValue(a.roleType) - roleSortValue(b.roleType) || a.title.localeCompare(b.title);
        if (sort === "Company name") return a.companyName.localeCompare(b.companyName);
        if (sort === "Job title") return a.title.localeCompare(b.title);
        return new Date(b.postedDate || 0).getTime() - new Date(a.postedDate || 0).getTime();
      });
  }, [applied, companyFilter, locationFilter, minPay, platform, query, roleType, validJobs, selectedCompanyId, sort, workType]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safeCurrentPage = Math.min(currentPage, pageCount);
  const startIndex = filtered.length ? (safeCurrentPage - 1) * pageSize : 0;
  const endIndex = Math.min(startIndex + pageSize, filtered.length);
  const pagedJobs = filtered.slice(startIndex, endIndex);
  const pageNumbers = buildPageNumbers(safeCurrentPage, pageCount);
  const platformOptions = useMemo(
    () => Array.from(new Set(validJobs.map((job) => job.jobPlatform).filter(Boolean))).sort(),
    [validJobs],
  );
  const companyOptions = useMemo(
    () => Array.from(new Set(validJobs.map((job) => job.companyName).filter(Boolean))).sort(),
    [validJobs],
  );
  const locationOptions = useMemo(
    () => Array.from(new Set(validJobs.map((job) => job.location).filter(Boolean))).sort(),
    [validJobs],
  );

  if (dataStatus === "loading" || dataStatus === "error") {
    return <DataStatePanel status={dataStatus} error={dataError} loadingLabel="Loading jobs..." onRetry={onRetry} />;
  }

  return (
    <div className="space-y-5">
      <div className="panel grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-5">
        <input className="field" placeholder="Search company, title, skill, or keyword" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select className="field" value={workType} onChange={(event) => setWorkType(event.target.value as WorkType | "All work types")}>
          <option>All work types</option>
          <option>Remote</option>
          <option>Hybrid</option>
          <option>Onsite</option>
          <option value="Not Listed">Not listed</option>
        </select>
        <select className="field" value={companyFilter} onChange={(event) => setCompanyFilter(event.target.value)}>
          <option>All companies</option>
          {companyOptions.map((value) => <option key={value}>{value}</option>)}
        </select>
        <select className="field" value={locationFilter} onChange={(event) => setLocationFilter(event.target.value)}>
          <option>All locations</option>
          {locationOptions.map((value) => <option key={value}>{value}</option>)}
        </select>
        <select className="field" value={platform} onChange={(event) => setPlatform(event.target.value)}>
          <option>All platforms</option>
          {platformOptions.map((value) => <option key={value}>{value}</option>)}
        </select>
        <select className="field" value={roleType} onChange={(event) => setRoleType(event.target.value as RoleType | "All role types")}>
          <option>All role types</option>
          <option>IC</option>
          <option>MGR</option>
          <option>EXEC</option>
          <option>UNKNOWN</option>
        </select>
        <input className="field" placeholder="Minimum pay" value={minPay} onChange={(event) => setMinPay(event.target.value)} />
        <select className="field" value={applied} onChange={(event) => setApplied(event.target.value)}>
          <option>All application statuses</option>
          <option>Not applied</option>
          <option>Applied</option>
          <option>Not interested</option>
          <option>Interview Scheduled</option>
          <option>Rejected</option>
          <option>Offer</option>
          <option>Archived</option>
        </select>
        <select className="field" value={sort} onChange={(event) => setSort(event.target.value)}>
          <option>Newest</option>
          <option>Highest pay</option>
          <option>Best resume fit</option>
          <option>Company name</option>
          <option>Job title</option>
          <option>Role type</option>
        </select>
      </div>
      <div className="panel flex flex-col gap-3 p-4 text-sm text-slate-300 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-2">
          <span className="badge border-radar-line">Total jobs loaded: {jobs.length}</span>
          <span className="badge border-radar-line">Inactive or invalid jobs hidden: {hiddenJobs}</span>
          <span className="badge border-radar-line">Jobs displayed after filters: {filtered.length}</span>
          <span className="badge border-radar-line">Current page: {safeCurrentPage}</span>
          <span className="badge border-radar-line">Page size: {pageSize}</span>
        </div>
        <label className="flex items-center gap-2 text-slate-400">
          Page size
          <select className="field w-36" value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}>
            <option value={10}>10 per page</option>
            <option value={25}>25 per page</option>
            <option value={50}>50 per page</option>
            <option value={100}>100 per page</option>
          </select>
        </label>
      </div>
      {filtered.length ? (
        <div className="flex flex-col gap-3 text-sm text-slate-400 md:flex-row md:items-center md:justify-between">
          <p>
            Showing {startIndex + 1}-{endIndex} of {filtered.length} jobs
          </p>
          <p>Page {safeCurrentPage} of {pageCount}</p>
        </div>
      ) : null}
      <div className="space-y-4">
        {pagedJobs.map((job) => (
          <JobCard
            key={job.id}
            job={job}
            onMarkApplied={onMarkApplied}
            onNotInterested={onNotInterested}
            onUpdateNotes={onUpdateNotes}
            updating={pendingApplicationIds.has(job.id)}
            onViewDetails={setDetailsJob}
            onViewCompany={onViewCompany}
          />
        ))}
        {!jobs.length ? <div className="card p-8 text-center text-slate-400">No jobs are stored yet.</div> : null}
        {jobs.length && !filtered.length ? <div className="card p-8 text-center text-slate-400">No jobs match the current filters.</div> : null}
      </div>
      {filtered.length ? (
        <div className="panel flex flex-col gap-3 p-4 md:flex-row md:items-center md:justify-between">
          <button className="btn justify-center" disabled={safeCurrentPage <= 1} onClick={() => setCurrentPage((page) => Math.max(1, page - 1))}>
            Previous
          </button>
          <div className="flex flex-wrap justify-center gap-2">
            {pageNumbers.map((page) => (
              <button
                className={`rounded-md border px-3 py-2 text-sm transition ${
                  page === safeCurrentPage
                    ? "border-radar-primary bg-radar-primary text-white"
                    : "border-radar-line bg-radar-panel text-slate-300 hover:border-radar-highlight hover:text-white"
                }`}
                key={page}
                onClick={() => setCurrentPage(page)}
              >
                {page}
              </button>
            ))}
          </div>
          <button className="btn justify-center" disabled={safeCurrentPage >= pageCount} onClick={() => setCurrentPage((page) => Math.min(pageCount, page + 1))}>
            Next
          </button>
        </div>
      ) : null}
      <JobDetailsModal job={detailsJob} onClose={() => setDetailsJob(null)} onRematch={onRematch} />
    </div>
  );
}

function roleSortValue(roleType: string | undefined): number {
  const order: Record<string, number> = { EXEC: 0, MGR: 1, IC: 2, UNKNOWN: 3 };
  return order[roleType || "UNKNOWN"] ?? 3;
}

function matchesApplicationFilter(job: Job, appliedFilter: string): boolean {
  if (appliedFilter === "All application statuses") return true;
  if (appliedFilter === "Not applied") return !job.applied && !job.notInterested;
  if (appliedFilter === "Not interested") return Boolean(job.notInterested);
  if (appliedFilter === "Applied") return Boolean(job.applied) || job.applicationStatus === "Applied";
  return job.applicationStatus === appliedFilter;
}

function buildPageNumbers(currentPage: number, pageCount: number): number[] {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);
  const pages = new Set([1, pageCount, currentPage - 1, currentPage, currentPage + 1]);
  if (currentPage <= 3) {
    pages.add(2);
    pages.add(3);
    pages.add(4);
  }
  if (currentPage >= pageCount - 2) {
    pages.add(pageCount - 1);
    pages.add(pageCount - 2);
    pages.add(pageCount - 3);
  }
  return Array.from(pages)
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((a, b) => a - b);
}
