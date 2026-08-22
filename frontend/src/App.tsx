import { FormEvent, useEffect, useMemo, useState } from "react";

type Summary = { companies: number; open_jobs: number; applications: number; candidates_for_review: number };
type Job = { id: number; title: string; company_name: string; location: string; employment_type: string; role_classification: string; application_status?: string | null };
type Company = { id: number; name: string; city: string; state: string; job_board_url: string; search_status: string; job_count: number };
type Application = { id: number; status: string; job_title: string; company_name: string; applied_date: string; notes: string };
type Tab = "Dashboard" | "Companies" | "Job List" | "Jobs Applied For" | "Resume Match";

const API = "/api";
const tabs: Tab[] = ["Dashboard", "Companies", "Job List", "Jobs Applied For", "Resume Match"];

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "Request failed");
  return response.json();
}

export function App() {
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [companyName, setCompanyName] = useState("");

  const load = async () => {
    setLoading(true); setError("");
    try {
      const [dashboard, companyRows, jobRows, applicationRows] = await Promise.all([
        api<{ summary: Summary }>("/dashboard"), api<Company[]>("/companies"), api<Job[]>("/jobs"), api<Application[]>("/applications"),
      ]);
      setSummary(dashboard.summary); setCompanies(companyRows); setJobs(jobRows); setApplications(applicationRows);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not reach the Opportunity Radar API."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const title = useMemo(() => tab === "Dashboard" ? "Your job search, in one place" : tab, [tab]);
  const addCompany = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyName.trim()) return;
    await api("/companies", { method: "POST", body: JSON.stringify({ name: companyName.trim() }) });
    setCompanyName(""); await load(); setTab("Companies");
  };
  const markInterested = async (job: Job) => { await api(`/jobs/${job.id}/application`, { method: "PUT", body: JSON.stringify({ status: "Interested" }) }); await load(); };

  return <main className="app-shell">
    <aside><div className="brand"><span>◉</span> Opportunity Radar</div><nav>{tabs.map(item => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</nav><p className="local-note">Local-first. Your data stays on this computer.</p></aside>
    <section className="content"><header><div><p className="eyebrow">OPPORTUNITY RADAR</p><h1>{title}</h1></div><button className="secondary" onClick={() => void load()}>Refresh</button></header>
      {error && <div className="alert">{error}</div>}
      {loading ? <p>Loading your workspace…</p> : <>
        {tab === "Dashboard" && <Dashboard summary={summary} jobs={jobs} onInterested={markInterested} />}
        {tab === "Companies" && <Companies companies={companies} name={companyName} setName={setCompanyName} onSubmit={addCompany} />}
        {tab === "Job List" && <Jobs jobs={jobs} onInterested={markInterested} />}
        {tab === "Jobs Applied For" && <Applications applications={applications} />}
        {tab === "Resume Match" && <ResumeMatch />}
      </>}</section>
  </main>;
}

function Dashboard({ summary, jobs, onInterested }: { summary: Summary | null; jobs: Job[]; onInterested: (job: Job) => void }) {
  return <><div className="cards">{[["Companies", summary?.companies ?? 0], ["Open jobs", summary?.open_jobs ?? 0], ["Tracked applications", summary?.applications ?? 0], ["Candidate review", summary?.candidates_for_review ?? 0]].map(([label, value]) => <article className="card" key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</div><section className="panel"><h2>Recent opportunities</h2><JobTable jobs={jobs.slice(0, 5)} onInterested={onInterested} /></section></>;
}
function Companies({ companies, name, setName, onSubmit }: { companies: Company[]; name: string; setName: (value: string) => void; onSubmit: (event: FormEvent) => void }) {
  return <><section className="panel"><h2>Add a company</h2><form onSubmit={onSubmit}><input value={name} onChange={event => setName(event.target.value)} placeholder="Company name" /><button>Add company</button></form></section><section className="panel"><h2>Companies</h2><table><thead><tr><th>Company</th><th>Location</th><th>Board status</th><th>Jobs</th></tr></thead><tbody>{companies.map(company => <tr key={company.id}><td>{company.name}</td><td>{[company.city, company.state].filter(Boolean).join(", ") || "—"}</td><td>{company.job_board_url ? "Board saved" : company.search_status}</td><td>{company.job_count}</td></tr>)}</tbody></table></section></>;
}
function Jobs({ jobs, onInterested }: { jobs: Job[]; onInterested: (job: Job) => void }) { return <section className="panel"><h2>Validated jobs</h2><JobTable jobs={jobs} onInterested={onInterested} /></section>; }
function JobTable({ jobs, onInterested }: { jobs: Job[]; onInterested: (job: Job) => void }) { return <table><thead><tr><th>Role</th><th>Company</th><th>Location</th><th>Type</th><th></th></tr></thead><tbody>{jobs.length ? jobs.map(job => <tr key={job.id}><td><strong>{job.title}</strong><small>{job.role_classification}</small></td><td>{job.company_name}</td><td>{job.location || "—"}</td><td>{job.employment_type || "—"}</td><td><button className="secondary" disabled={Boolean(job.application_status)} onClick={() => void onInterested(job)}>{job.application_status || "Track"}</button></td></tr>) : <tr><td colSpan={5}>No validated jobs yet. Collection will only use saved, true job-board URLs.</td></tr>}</tbody></table>; }
function Applications({ applications }: { applications: Application[] }) { return <section className="panel"><h2>Jobs applied for</h2><table><thead><tr><th>Role</th><th>Company</th><th>Status</th><th>Applied</th></tr></thead><tbody>{applications.length ? applications.map(application => <tr key={application.id}><td>{application.job_title}</td><td>{application.company_name}</td><td><span className="pill">{application.status}</span></td><td>{application.applied_date || "—"}</td></tr>) : <tr><td colSpan={4}>Choose “Track” from a job to begin application tracking.</td></tr>}</tbody></table></section>; }
function ResumeMatch() { return <section className="panel"><h2>Resume Match</h2><p>Resume upload and deterministic scoring are the next build step. Scores will stay local: 80–100 Strong Apply, 60–79 Good Match, 40–59 Stretch Role, and 0–39 Poor Fit.</p></section>; }
