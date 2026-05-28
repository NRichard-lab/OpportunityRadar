import { useMemo, useState } from "react";
import type { Job } from "../types/Job";
import type { ResumeProfile } from "../types/ResumeProfile";
import { ResumeUpload } from "../components/ResumeUpload";
import { MatchScoreBadge } from "../components/MatchScoreBadge";
import { compareResumeToJob } from "../utils/resumeMatch";

interface ResumeMatchProps {
  jobs: Job[];
  resume: ResumeProfile | null;
  onResumeChange: (resume: ResumeProfile) => void;
}

export function ResumeMatch({ jobs, resume, onResumeChange }: ResumeMatchProps) {
  const [selectedJobId, setSelectedJobId] = useState(jobs[0]?.id ?? "");
  const selectedJob = jobs.find((job) => job.id === selectedJobId) ?? jobs[0];
  const result = useMemo(() => (selectedJob ? compareResumeToJob(resume, selectedJob) : null), [resume, selectedJob]);

  return (
    <div className="grid gap-6 xl:grid-cols-[0.85fr_1.15fr]">
      <div className="space-y-5">
        <ResumeUpload resume={resume} onResumeChange={onResumeChange} />
        <div className="card p-5">
          <label className="text-sm font-semibold text-white" htmlFor="job-select">Select job</label>
          <select id="job-select" className="field mt-2" value={selectedJob?.id ?? ""} onChange={(event) => setSelectedJobId(event.target.value)}>
            {jobs.map((job) => <option key={job.id} value={job.id}>{job.companyName} - {job.title}</option>)}
          </select>
          <p className="mt-4 text-sm text-slate-400">
            Suggestions stay honest: this tool highlights focus areas and does not generate fake experience or exaggerated qualifications.
          </p>
        </div>
      </div>
      <div className="panel p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-sm text-radar-cyan">{selectedJob?.companyName ?? "No job selected"}</p>
            <h2 className="text-xl font-semibold text-white">{selectedJob?.title ?? "Resume match"}</h2>
            <p className="mt-1 text-sm text-slate-400">
              This is an estimated resume/job fit score, not a hiring prediction.
            </p>
          </div>
          {result ? <MatchScoreBadge score={result.score} recommendation={result.recommendation} /> : null}
        </div>
        {result ? (
          <div className="mt-6 space-y-5">
            <Section title="Matched keywords" items={result.matchedKeywords} />
            <Section title="Missing keywords" items={result.missingKeywords} />
            <div className="grid gap-4 md:grid-cols-2">
              <Block title="Experience alignment" body={result.experienceAlignment} />
              <Block title="Title alignment" body={result.titleAlignment} />
            </div>
            <Block
              title="Suggested resume focus areas"
              body={
                result.missingKeywords.length
                  ? `If accurate, consider making relevant experience easier to find for: ${result.missingKeywords.slice(0, 5).join(", ")}.`
                  : "The visible keywords already overlap well. Keep the resume specific and evidence-based."
              }
            />
            <Block title="Summary" body={result.summary} />
          </div>
        ) : (
          <p className="mt-6 text-slate-400">Add a job to compare against your resume.</p>
        )}
      </div>
    </div>
  );
}

function Section({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <h3 className="font-semibold text-white">{title}</h3>
      <div className="mt-3 flex flex-wrap gap-2">
        {items.length ? items.map((item) => <span className="badge border-radar-line text-slate-200" key={item}>{item}</span>) : <span className="text-sm text-slate-400">Not listed</span>}
      </div>
    </div>
  );
}

function Block({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-md bg-radar-bg p-4">
      <p className="text-xs uppercase tracking-wide text-slate-500">{title}</p>
      <p className="mt-2 text-sm leading-6 text-slate-300">{body}</p>
    </div>
  );
}
