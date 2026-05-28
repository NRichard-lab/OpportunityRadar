import { useEffect, useMemo, useState } from "react";
import type { Job, RoleType, WorkType } from "../types/Job";
import type { ResumeProfile } from "../types/ResumeProfile";
import { JobCard } from "../components/JobCard";
import { compareResumeToJob } from "../utils/resumeMatch";

interface JobListProps {
  jobs: Job[];
  resume: ResumeProfile | null;
  onMarkApplied: (jobId: string) => void;
  onNotInterested: (jobId: string) => void;
  onUpdateNotes: (jobId: string, notes: string) => void;
  selectedCompanyId?: string;
}

export function JobList({ jobs, resume, onMarkApplied, onNotInterested, onUpdateNotes, selectedCompanyId }: JobListProps) {
  const [query, setQuery] = useState("");
  const [workType, setWorkType] = useState<WorkType | "All">("All");
  const [platform, setPlatform] = useState("All");
  const [roleType, setRoleType] = useState<RoleType | "All">("All");
  const [applied, setApplied] = useState("All");
  const [minPay, setMinPay] = useState("");
  const [sort, setSort] = useState("Newest");
  const [pageSize, setPageSize] = useState(25);
  const [currentPage, setCurrentPage] = useState(1);
  const [detailsJob, setDetailsJob] = useState<Job | null>(null);

  const validJobs = useMemo(() => jobs.filter(isValidJobRecord), [jobs]);
  const invalidJobsHidden = jobs.length - validJobs.length;

  const scoredJobs = useMemo(
    () => validJobs.map((job) => ({ ...job, matchScore: resume ? compareResumeToJob(resume, job).score : job.matchScore })),
    [resume, validJobs],
  );

  useEffect(() => {
    setCurrentPage(1);
  }, [applied, minPay, pageSize, platform, query, roleType, selectedCompanyId, sort, workType]);

  const filtered = useMemo(() => {
    const minimumPay = Number(minPay) || 0;
    return [...scoredJobs]
      .filter((job) => {
        const searchable = `${job.companyName} ${job.title} ${job.location} ${job.jobPlatform}`.toLowerCase();
        return (
          (!selectedCompanyId || job.companyId === selectedCompanyId) &&
          searchable.includes(query.toLowerCase()) &&
          (workType === "All" || job.workType === workType) &&
          (roleType === "All" || (job.roleType || "UNKNOWN") === roleType) &&
          (platform === "All" || job.jobPlatform === platform) &&
          (applied === "All" || String(job.applied) === applied) &&
          ((job.payMax ?? job.payMin ?? 0) >= minimumPay)
        );
      })
      .sort((a, b) => {
        if (sort === "Highest pay") return (b.payMax ?? 0) - (a.payMax ?? 0);
        if (sort === "Best resume match") return (b.matchScore ?? 0) - (a.matchScore ?? 0);
        if (sort === "Role Type") return roleSortValue(a.roleType) - roleSortValue(b.roleType) || a.title.localeCompare(b.title);
        if (sort === "Company name") return a.companyName.localeCompare(b.companyName);
        if (sort === "Title") return a.title.localeCompare(b.title);
        return new Date(b.postedDate || 0).getTime() - new Date(a.postedDate || 0).getTime();
      });
  }, [applied, minPay, platform, query, roleType, scoredJobs, selectedCompanyId, sort, workType]);

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

  return (
    <div className="space-y-5">
      <div className="panel grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-7">
        <input className="field" placeholder="Search company, title, location" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select className="field" value={workType} onChange={(event) => setWorkType(event.target.value as WorkType | "All")}>
          <option>All</option>
          <option>Remote</option>
          <option>Hybrid</option>
          <option>Onsite</option>
          <option>Not Listed</option>
        </select>
        <select className="field" value={platform} onChange={(event) => setPlatform(event.target.value)}>
          <option>All</option>
          {platformOptions.map((value) => <option key={value}>{value}</option>)}
        </select>
        <select className="field" value={roleType} onChange={(event) => setRoleType(event.target.value as RoleType | "All")}>
          <option>All</option>
          <option>IC</option>
          <option>MGR</option>
          <option>EXEC</option>
          <option>UNKNOWN</option>
        </select>
        <input className="field" placeholder="Minimum pay" value={minPay} onChange={(event) => setMinPay(event.target.value)} />
        <select className="field" value={applied} onChange={(event) => setApplied(event.target.value)}>
          <option>All</option>
          <option value="true">Applied</option>
          <option value="false">Not applied</option>
        </select>
        <select className="field" value={sort} onChange={(event) => setSort(event.target.value)}>
          <option>Newest</option>
          <option>Highest pay</option>
          <option>Best resume match</option>
          <option>Role Type</option>
          <option>Company name</option>
          <option>Title</option>
        </select>
      </div>
      <div className="panel flex flex-col gap-3 p-4 text-sm text-slate-300 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-2">
          <span className="badge border-radar-line">Total jobs loaded: {jobs.length}</span>
          <span className="badge border-radar-line">Invalid jobs hidden: {invalidJobsHidden}</span>
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
            onViewDetails={setDetailsJob}
          />
        ))}
        {!jobs.length ? <div className="card p-8 text-center text-slate-400">No jobs loaded yet. Run job collection first.</div> : null}
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
                    ? "border-radar-cyan bg-radar-cyan text-slate-950"
                    : "border-radar-line bg-radar-panel text-slate-300 hover:border-radar-cyan hover:text-white"
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
      {detailsJob ? (
        <div className="fixed inset-0 z-40 grid place-items-center bg-black/70 p-3 md:p-4" onClick={() => setDetailsJob(null)}>
          <div className="panel flex max-h-[85vh] w-full max-w-4xl min-h-0 flex-col overflow-hidden p-0" onClick={(event) => event.stopPropagation()}>
            <div className="flex shrink-0 items-start justify-between gap-4 border-b border-radar-line p-5">
              <div>
                <p className="text-radar-cyan">{detailsJob.companyName}</p>
                <h2 className="break-words text-xl font-semibold text-white">{detailsJob.title}</h2>
              </div>
              <button className="btn" onClick={() => setDetailsJob(null)}>Close</button>
            </div>
            <div className="min-h-0 overflow-y-auto p-5">
              <div className="grid gap-3 break-words text-sm md:grid-cols-2">
                <p>Company: {detailsJob.companyName || "Not listed"}</p>
                <p>Location: {detailsJob.location || "Not listed"}</p>
                <p>Work type: {detailsJob.workType || "Not listed"}</p>
                <p>Role type: {detailsJob.roleType || "UNKNOWN"}</p>
                <p className="md:col-span-2">Role type reason: {detailsJob.roleTypeReason || "Not listed"}</p>
                <p>Platform: {detailsJob.jobPlatform || "Not listed"}</p>
                <p>Pay min: {detailsJob.payMin ?? "Not listed"}</p>
                <p>Pay max: {detailsJob.payMax ?? "Not listed"}</p>
                <p>Pay text: {detailsJob.payText || "Not listed"}</p>
                <p>Pay period: {detailsJob.payPeriod || "Not listed"}</p>
                <p>Posted: {detailsJob.postedDate || "Not listed"}</p>
                <p className="md:col-span-2">
                  Source URL:{" "}
                  {detailsJob.sourceUrl ? (
                    <a className="break-all text-radar-cyan hover:text-white" href={detailsJob.sourceUrl} target="_blank">
                      {detailsJob.sourceUrl}
                    </a>
                  ) : (
                    "Not listed"
                  )}
                </p>
              </div>
              <section className="mt-5 min-h-0">
                <h3 className="text-sm font-semibold text-white">Full description</h3>
                <p className="mt-3 max-h-[46vh] overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-radar-line bg-radar-bg p-4 text-sm leading-6 text-slate-300">
                  {detailsJob.description || "Not listed"}
                </p>
              </section>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function roleSortValue(roleType: string | undefined): number {
  const order: Record<string, number> = { EXEC: 0, MGR: 1, IC: 2, UNKNOWN: 3 };
  return order[roleType || "UNKNOWN"] ?? 3;
}

function isValidJobRecord(job: Job): boolean {
  const title = (job.title || "").trim().toLowerCase();
  const rejectedTitles = new Set([
    "remote work",
    "skip to content",
    "careers",
    "search open positions",
    "search jobs",
    "view open positions",
    "apply now",
    "join our team",
    "home",
    "menu",
    "privacy",
    "terms",
    "accessibility",
    "login",
    "sign in",
    "benefits",
    "culture",
    "locations",
    "equal opportunity",
    "talent community",
    "view details",
    "view details (opens an external site)",
    "opens an external site",
    "learn more",
    "apply",
  ]);
  const rejectedParts = [
    "view details",
    "opens an external site",
    "apply now",
    "search jobs",
    "careers",
    "remote work",
    "benefits",
    "culture",
    "locations",
    "talent community",
  ];
  if (!job.id || !job.title || !job.sourceUrl) return false;
  if (rejectedTitles.has(title)) return false;
  if (rejectedParts.some((part) => title.includes(part))) return false;
  if (title.length < 4) return false;
  return true;
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
