import type { ApplicationStatus, Job } from "../types/Job";
import type { DataLoadStatus } from "../types/DataLoadState";
import { DataStatePanel } from "../components/DataStatePanel";
import { StatCard } from "../components/StatCard";

interface JobsAppliedForProps {
  jobs: Job[];
  dataStatus: DataLoadStatus;
  dataError: string;
  onRetry: () => void;
  pendingApplicationIds: Set<string>;
  onUpdateStatus: (jobId: string, status: ApplicationStatus) => Promise<boolean>;
  onUpdateFollowUp: (jobId: string, followUpDate: string) => Promise<boolean>;
  onUpdateNotes: (jobId: string, notes: string) => Promise<boolean>;
}

const statuses: ApplicationStatus[] = ["Interested", "Applied", "Followed Up", "Interview Scheduled", "Rejected", "Offer", "Archived"];

export function JobsAppliedFor({ jobs, dataStatus, dataError, onRetry, pendingApplicationIds, onUpdateStatus, onUpdateFollowUp, onUpdateNotes }: JobsAppliedForProps) {
  if (dataStatus === "loading" || dataStatus === "error") {
    return <DataStatePanel status={dataStatus} error={dataError} loadingLabel="Loading applications..." onRetry={onRetry} />;
  }
  const appliedJobs = jobs.filter((job) => job.applied);
  const interviews = appliedJobs.filter((job) => job.applicationStatus === "Interview Scheduled").length;
  const rejections = appliedJobs.filter((job) => job.applicationStatus === "Rejected").length;
  const offers = appliedJobs.filter((job) => job.applicationStatus === "Offer").length;
  const pending = appliedJobs.filter((job) => ["Applied", "Followed Up"].includes(job.applicationStatus)).length;
  const averageDays = averageDaysSince(appliedJobs);

  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard label="Total applied" value={appliedJobs.length} />
        <StatCard label="Interviews" value={interviews} />
        <StatCard label="Rejections" value={rejections} />
        <StatCard label="Offers" value={offers} />
        <StatCard label="Pending" value={pending} />
        <StatCard label="Avg days since app" value={averageDays ?? "Not listed"} />
      </div>
      <div className="panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[960px] text-left text-sm">
            <thead className="border-b border-radar-line bg-radar-bg text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="p-3">Company</th>
                <th className="p-3">Job title</th>
                <th className="p-3">Date applied</th>
                <th className="p-3">Status</th>
                <th className="p-3">Follow-up</th>
                <th className="p-3">Resume version</th>
                <th className="p-3">Job URL</th>
                <th className="p-3">Notes</th>
              </tr>
            </thead>
            <tbody>
              {appliedJobs.map((job) => (
                <tr className="border-b border-radar-line/70" key={job.id}>
                  <td className="p-3 text-white">{job.companyName}</td>
                  <td className="p-3">{job.title}</td>
                  <td className="p-3">{job.dateApplied || "Not listed"}</td>
                  <td className="p-3">
                    <select className="field min-w-40" value={job.applicationStatus} disabled={pendingApplicationIds.has(job.id)} onChange={(event) => void onUpdateStatus(job.id, event.target.value as ApplicationStatus)}>
                      {statuses.map((status) => <option key={status}>{status}</option>)}
                    </select>
                  </td>
                  <td className="p-3">
                    <input className="field min-w-36" type="date" value={job.followUpDate} disabled={pendingApplicationIds.has(job.id)} onChange={(event) => void onUpdateFollowUp(job.id, event.target.value)} />
                  </td>
                  <td className="p-3">Current resume</td>
                  <td className="p-3"><a className="text-radar-highlight" href={job.sourceUrl} target="_blank" rel="noreferrer noopener">Open</a></td>
                  <td className="p-3">
                    <input key={`${job.id}-${job.notes}`} className="field min-w-56" defaultValue={job.notes} disabled={pendingApplicationIds.has(job.id)} onBlur={(event) => { const input = event.currentTarget; if (input.value === job.notes) return; void onUpdateNotes(job.id, input.value).then((saved) => { if (!saved) input.value = job.notes; }); }} />
                    {pendingApplicationIds.has(job.id) ? <span className="mt-1 block text-xs text-slate-500" role="status">Saving...</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!appliedJobs.length ? <div className="p-8 text-center text-slate-400">{dataStatus === "empty" ? "No jobs or applications are stored yet." : "No applied jobs yet."}</div> : null}
      </div>
    </div>
  );
}

function averageDaysSince(jobs: Job[]) {
  const dates = jobs.map((job) => job.dateApplied).filter(Boolean);
  if (!dates.length) return null;
  const now = Date.now();
  const total = dates.reduce((sum, date) => sum + Math.max(0, now - new Date(date).getTime()) / 86400000, 0);
  return Math.round(total / dates.length);
}
