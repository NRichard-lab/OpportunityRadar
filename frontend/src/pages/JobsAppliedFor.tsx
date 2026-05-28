import type { ApplicationStatus, Job } from "../types/Job";
import { StatCard } from "../components/StatCard";

interface JobsAppliedForProps {
  jobs: Job[];
  onUpdateStatus: (jobId: string, status: ApplicationStatus) => void;
  onUpdateFollowUp: (jobId: string, followUpDate: string) => void;
  onUpdateNotes: (jobId: string, notes: string) => void;
}

const statuses: ApplicationStatus[] = ["Interested", "Applied", "Followed Up", "Interview Scheduled", "Rejected", "Offer", "Archived"];

export function JobsAppliedFor({ jobs, onUpdateStatus, onUpdateFollowUp, onUpdateNotes }: JobsAppliedForProps) {
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
                    <select className="field min-w-40" value={job.applicationStatus} onChange={(event) => onUpdateStatus(job.id, event.target.value as ApplicationStatus)}>
                      {statuses.map((status) => <option key={status}>{status}</option>)}
                    </select>
                  </td>
                  <td className="p-3">
                    <input className="field min-w-36" type="date" value={job.followUpDate} onChange={(event) => onUpdateFollowUp(job.id, event.target.value)} />
                  </td>
                  <td className="p-3">Current resume</td>
                  <td className="p-3"><a className="text-radar-cyan" href={job.sourceUrl} target="_blank">Open</a></td>
                  <td className="p-3">
                    <input className="field min-w-56" value={job.notes} onChange={(event) => onUpdateNotes(job.id, event.target.value)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!appliedJobs.length ? <div className="p-8 text-center text-slate-400">No applied jobs yet.</div> : null}
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
