import { FormEvent, useEffect, useMemo, useState } from "react";

type Summary = { companies: number; open_jobs: number; applications: number; candidates_for_review: number };
export type Job = { id: number; company_id: number; title: string; company_name: string; location: string; department: string; employment_type: string; role_classification: string; posted_date: string; detail_url: string; description: string; pay_min: number | null; pay_max: number | null; target_pay_min: number | null; target_pay_max: number | null; full_pay_min: number | null; full_pay_max: number | null; pay_currency: string; pay_period: string; pay_display: string; incentives_text: string; benefits_summary: string; benefit_tags: string; compensation_source_text: string; benefits_source_text: string; has_health_insurance: number; has_dental_insurance: number; has_vision_insurance: number; has_retirement: number; retirement_details: string; retirement_match_percent: number | null; retirement_contribution_percent: number | null; has_pto: number; pto_details: string; has_tuition_reimbursement: number; tuition_details: string; has_volunteer_time_off: number; has_donation_match: number; has_remote_hybrid: number; other_benefit_details: string; application_status?: string | null };
export type Company = { id: number; name: string; company_website: string; careers_page_url: string; verified_job_board_url: string; job_board_type: string; discovery_status: string; classification_confidence: string; discovery_method: string; last_verified_at: string; needs_manual_refresh: number; last_collection_status: string; industry: string; city: string; state: string; country: string; founded_year: number | null; total_assets: number | null; total_assets_display: string; assets_as_of_date: string; information_source_note: string; location_discovery_source: string; location_confidence: "Verified" | "Needs Review" | "Not Found"; possible_locations: string; notes: string; job_count: number; last_collector: string; last_collection_at: string; last_raw_count: number; last_saved_count: number; last_review_count: number; last_collection_error: string };
type CompanyForm = { name: string; company_website: string; careers_page_url: string; verified_job_board_url: string; job_board_type: string; classification_confidence: string; industry: string; city: string; state: string; country: string; founded_year: number | null; total_assets: number | null; total_assets_display: string; assets_as_of_date: string; information_source_note: string; location_discovery_source: string; location_confidence: "Verified" | "Needs Review" | "Not Found"; possible_locations: string; notes: string; discovery_status: string; discovery_method: string; needs_manual_refresh?: boolean };
type GatherResult = Partial<CompanyForm> & { platform?: string; sources: Record<string, string> };
type Application = { id: number; status: string; job_title: string; company_name: string; applied_date: string; notes: string };
type CandidateReview = { id: number; company_name: string; title: string; location: string; detail_url: string; rejection_reason: string; collected_at: string };
type Tab = "Dashboard" | "Companies" | "Job List" | "Candidate Review" | "Jobs Applied For" | "Resume Match";

const API = "/api";
const tabs: Tab[] = ["Dashboard", "Companies", "Job List", "Candidate Review", "Jobs Applied For", "Resume Match"];

class ApiError extends Error {
  status: number;
  discoveryStatus?: string;
  careersPageUrl?: string;
  constructor(message: string, status: number, discoveryStatus?: string, careersPageUrl?: string) { super(message); this.status = status; this.discoveryStatus = discoveryStatus; this.careersPageUrl = careersPageUrl; }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    throw new ApiError(typeof detail === "object" ? detail.message : detail || "Request failed", response.status, typeof detail === "object" ? detail.discovery_status : undefined, typeof detail === "object" ? detail.careers_page_url : undefined);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export function App() {
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [candidates, setCandidates] = useState<CandidateReview[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const load = async () => {
    setLoading(true); setError("");
    try {
      const [dashboard, companyRows, jobRows, applicationRows, candidateRows] = await Promise.all([
        api<{ summary: Summary }>("/dashboard"), api<Company[]>("/companies"), api<Job[]>("/jobs"), api<Application[]>("/applications"), api<CandidateReview[]>("/candidates"),
      ]);
      setSummary(dashboard.summary); setCompanies(companyRows); setJobs(jobRows); setApplications(applicationRows); setCandidates(candidateRows);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not reach the Opportunity Radar API."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const title = useMemo(() => tab === "Dashboard" ? "Your job search, in one place" : tab, [tab]);
  const markInterested = async (job: Job) => { await api(`/jobs/${job.id}/application`, { method: "PUT", body: JSON.stringify({ status: "Interested" }) }); await load(); };

  return <main className={`app-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
    <aside><div className="brand"><span>◉</span> Opportunity Radar</div><nav>{tabs.map(item => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</nav><p className="local-note">Local-first. Your data stays on this computer.</p></aside>
    <section className="content"><header><div><p className="eyebrow">OPPORTUNITY RADAR</p><h1>{title}</h1></div><div className="header-actions"><button className="secondary" aria-pressed={sidebarCollapsed} onClick={() => setSidebarCollapsed(value => !value)}>{sidebarCollapsed ? "Show navigation" : "Hide navigation"}</button><button className="secondary" onClick={() => void load()}>Refresh</button></div></header>
      {error && <div className="alert">{error}</div>}
      {loading ? <p>Loading your workspace…</p> : <>
        {tab === "Dashboard" && <Dashboard summary={summary} jobs={jobs} onInterested={markInterested} />}
        {tab === "Companies" && <Companies companies={companies} onSaved={load} />}
        {tab === "Job List" && <Jobs jobs={jobs} companies={companies} onInterested={markInterested} onReprocessed={load} />}
        {tab === "Candidate Review" && <CandidateReviews candidates={candidates} onApproved={load} />}
        {tab === "Jobs Applied For" && <Applications applications={applications} />}
        {tab === "Resume Match" && <ResumeMatch />}
      </>}</section>
  </main>;
}

function Dashboard({ summary, jobs, onInterested }: { summary: Summary | null; jobs: Job[]; onInterested: (job: Job) => void }) {
  return <><div className="cards">{[["Companies", summary?.companies ?? 0], ["Open jobs", summary?.open_jobs ?? 0], ["Tracked applications", summary?.applications ?? 0], ["Candidate review", summary?.candidates_for_review ?? 0]].map(([label, value]) => <article className="card" key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</div><section className="panel"><h2>Recent opportunities</h2><JobTable jobs={jobs.slice(0, 5)} onInterested={onInterested} /></section></>;
}
const emptyCompany: CompanyForm = { name: "", company_website: "", careers_page_url: "", verified_job_board_url: "", job_board_type: "", classification_confidence: "Low", industry: "Financial Services", city: "", state: "", country: "United States", founded_year: null, total_assets: null, total_assets_display: "", assets_as_of_date: "", information_source_note: "", location_discovery_source: "", location_confidence: "Not Found", possible_locations: "", notes: "", discovery_status: "Not Started", discovery_method: "" };

export function Companies({ companies, onSaved }: { companies: Company[]; onSaved: () => Promise<void> }) {
  const [editing, setEditing] = useState<Company | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<CompanyForm>(emptyCompany);
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [discoveryMessage, setDiscoveryMessage] = useState("");
  const [gathered, setGathered] = useState<GatherResult | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshProgress, setRefreshProgress] = useState<{ current: number; total: number; company: string; verified: number; needsReview: number; failed: number } | null>(null);
  const [refreshSummary, setRefreshSummary] = useState("");
  const [deleting, setDeleting] = useState<Company | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteMessage, setDeleteMessage] = useState("");
  const [collecting, setCollecting] = useState<number | "batch" | null>(null);
  const [collectionMessage, setCollectionMessage] = useState("");
  const [formError, setFormError] = useState("");

  const openAdd = () => { setEditing(null); setForm(emptyCompany); setFormError(""); setDiscoveryMessage(""); setGathered(null); setModalOpen(true); };
  const openEdit = (company: Company) => {
    setEditing(company);
    setForm({ name: company.name, company_website: company.company_website, careers_page_url: company.careers_page_url, verified_job_board_url: company.verified_job_board_url, job_board_type: company.job_board_type, classification_confidence: company.classification_confidence, industry: company.industry, city: company.city, state: company.state, country: company.country, founded_year: company.founded_year, total_assets: company.total_assets, total_assets_display: company.total_assets_display, assets_as_of_date: company.assets_as_of_date, information_source_note: company.information_source_note, location_discovery_source: company.location_discovery_source, location_confidence: company.location_confidence, possible_locations: company.possible_locations, notes: company.notes, discovery_status: company.discovery_status, discovery_method: company.discovery_method, needs_manual_refresh: Boolean(company.needs_manual_refresh) });
    setFormError(""); setDiscoveryMessage(""); setGathered(null); setModalOpen(true);
  };
  const close = () => { if (!saving) setModalOpen(false); };
  const setField = <K extends keyof CompanyForm,>(field: K, value: CompanyForm[K]) => setForm(current => ({ ...current, [field]: value }));
  const gatherInformation = async () => {
    if (!form.company_website.trim() && !form.careers_page_url.trim()) { setFormError("Enter a Company Website or Careers Page URL first."); return; }
    setDiscovering(true); setFormError(""); setDiscoveryMessage("");
    try {
      const result = await api<GatherResult>("/company-discovery/gather-information", { method: "POST", body: JSON.stringify({ company_website: form.company_website, careers_page_url: form.careers_page_url }) });
      setGathered(result);
      const items = [`Company Website ${result.company_website ? "verified" : "Not found"}`, `Careers Page ${result.careers_page_url ? "found" : "Not found"}`, `${result.platform ? result.platform + " Job Board verified" : "Job Board Not found"}`, `${result.city && result.state ? result.city + ", " + result.state + " found" : "Location Not found"}`, `${result.founded_year ? "Founded " + result.founded_year + " found" : "Founded Year Not found"}`, `${result.total_assets_display ? "Total Assets found: " + result.total_assets_display + (result.assets_as_of_date ? " as of " + result.assets_as_of_date : "") : "Total Assets Not found"}`];
      setDiscoveryMessage(`Information gathering complete: ${items.join(", ")}. Review the proposed values below before applying them.`);
    } catch (err) { setFormError(err instanceof Error ? err.message : "Could not gather verified public company information."); }
    finally { setDiscovering(false); }
  };
  const applyGathered = () => {
    if (!gathered) return;
    const proposed = Object.fromEntries(Object.entries(gathered).filter(([key, value]) => key !== "sources" && key !== "platform" && value !== "" && value !== null && value !== undefined));
    setForm(current => ({ ...current, ...proposed, information_source_note: gathered.information_source_note || current.information_source_note }));
    setGathered(null);
    setDiscoveryMessage("Gathered values applied to the form. Review or edit them, then save the company. No jobs were collected.");
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.name.trim()) { setFormError("Company Name is required."); return; }
    setSaving(true); setFormError("");
    try {
      await api(editing ? `/companies/${editing.id}` : "/companies", { method: editing ? "PUT" : "POST", body: JSON.stringify({ ...form, name: form.name.trim(), discovery_result: Boolean(discoveryMessage) }) });
      await onSaved(); setModalOpen(false);
    } catch (err) { setFormError(err instanceof Error ? err.message : "Could not save company."); }
    finally { setSaving(false); }
  };

  const refreshDiscovery = async () => {
    const pending = companies.filter(company => ["Not Started", "Failed", "Needs Review"].includes(company.discovery_status));
    setRefreshSummary("");
    if (!pending.length) { setRefreshSummary("All companies are already verified. No discovery refresh was needed."); return; }
    setRefreshing(true);
    let verified = 0, needsReview = 0, failed = 0;
    for (let index = 0; index < pending.length; index += 1) {
      const company = pending[index];
      setRefreshProgress({ current: index + 1, total: pending.length, company: company.name, verified, needsReview, failed });
      try {
        const result = await api<Company & { message: string }>(`/companies/${company.id}/refresh-discovery`, { method: "POST" });
        if (result.discovery_status === "Verified") verified += 1;
        else if (result.discovery_status === "Needs Review") needsReview += 1;
        else failed += 1;
      } catch { failed += 1; }
      setRefreshProgress({ current: index + 1, total: pending.length, company: company.name, verified, needsReview, failed });
      await onSaved();
    }
    setRefreshing(false); setRefreshProgress(null);
    setRefreshSummary(`Discovery refresh complete: ${verified} verified, ${needsReview} need review, ${failed} failed.`);
  };
  const deleteCompany = async () => {
    if (!deleting) return;
    setDeleteBusy(true); setFormError("");
    try {
      await api(`/companies/${deleting.id}`, { method: "DELETE" });
      setDeleting(null); setDeleteMessage("Company and related job data deleted.");
      await onSaved();
    } catch (err) { setFormError(err instanceof Error ? err.message : "Could not delete company."); }
    finally { setDeleteBusy(false); }
  };
  const collectOne = async (company: Company, debug = false) => {
    setCollecting(company.id); setCollectionMessage(""); setFormError("");
    try {
      const report = await api<{ collector: string; candidate_count: number; saved_count: number; rejected_count: number }>(`/companies/${company.id}/collect-jobs`, { method: "POST", body: JSON.stringify({ debug }) });
      setCollectionMessage(`${company.name}: ${report.collector} collected ${report.candidate_count} raw candidates, saved ${report.saved_count} new validated jobs, and sent ${report.rejected_count} to Candidate Review.${debug ? " Debug diagnostics were written." : ""}`);
      await onSaved();
    } catch (err) { setFormError(err instanceof Error ? err.message : "Job collection failed."); await onSaved(); }
    finally { setCollecting(null); }
  };
  const collectAllVerified = async () => {
    const eligible = companies.filter(company => company.discovery_status === "Verified" && Boolean(company.verified_job_board_url));
    if (!eligible.length) { setCollectionMessage("No verified companies with a saved Job Board URL are ready for collection."); return; }
    setCollecting("batch"); let completed = 0, failed = 0;
    for (const company of eligible) {
      setCollectionMessage(`Collecting jobs for ${company.name} (${completed + failed + 1} of ${eligible.length})…`);
      try { await api(`/companies/${company.id}/collect-jobs`, { method: "POST", body: JSON.stringify({ debug: false }) }); completed += 1; }
      catch { failed += 1; }
      await onSaved();
    }
    setCollectionMessage(`Batch collection complete: ${completed} companies completed, ${failed} failed.`); setCollecting(null);
  };

  return <>
    <div className="section-actions"><p>Manage company sources. Job collection only uses a verified Job Board URL.</p><div className="button-group"><button className="secondary" disabled={refreshing || collecting !== null} onClick={() => void collectAllVerified()}>{collecting === "batch" ? "Collecting…" : "Collect All Verified"}</button><button className="secondary" disabled={refreshing || collecting !== null} onClick={() => void refreshDiscovery()}>{refreshing ? "Refreshing…" : "Refresh Discovery Status"}</button><button disabled={refreshing || collecting !== null} onClick={openAdd}>Add Company</button></div></div>
    {refreshProgress && <section className="discovery-progress" aria-live="polite"><strong>Checking {refreshProgress.current} of {refreshProgress.total} companies</strong><span>Currently checking: {refreshProgress.company}</span><div><span>Verified: {refreshProgress.verified}</span><span>Needs Review: {refreshProgress.needsReview}</span><span>Failed: {refreshProgress.failed}</span></div></section>}
    {refreshSummary && <div className="success-notice refresh-summary" aria-live="polite">{refreshSummary}</div>}
    {deleteMessage && <div className="success-notice refresh-summary" aria-live="polite">{deleteMessage}</div>}
    {collectionMessage && <div className="success-notice refresh-summary" aria-live="polite">{collectionMessage}</div>}
    <section className="panel"><h2>Companies</h2><table className="companies-table"><thead><tr><th>Company</th><th>Company Website</th><th>Careers Page</th><th>Job Board URL</th><th>Job Board Type</th><th>Discovery Status</th><th>Job Count</th><th>Last Collection</th><th></th></tr></thead><tbody>{companies.map(company => <tr key={company.id}>
      <td><strong>{company.name}</strong><small>{[company.city, company.state].filter(Boolean).join(", ") || company.industry}</small><DiscoveryDetails company={company} /></td>
      <td><ExternalLink href={company.company_website} empty="—" /></td>
      <td><ExternalLink href={company.careers_page_url} empty="—" /></td>
      <td>{company.verified_job_board_url ? <><ExternalLink href={company.verified_job_board_url} empty="—" />{Boolean(company.needs_manual_refresh) && <span className="warning-status">Manual refresh required</span>}</> : <span className="warning-status">Not configured</span>}</td><td><span className="pill">{company.job_board_type || "—"}</span><small>{company.classification_confidence}{company.last_verified_at ? ` · verified ${new Date(company.last_verified_at).toLocaleDateString()}` : ""}</small><small>{company.discovery_method || "Not classified"}</small></td>
      <td><span className="pill">{company.discovery_status}</span></td><td>{company.job_count}</td><td>{company.last_collection_at ? <div className="collection-result"><strong>{company.last_collector}</strong><small>{company.last_collection_status || "Unknown"} · {new Date(company.last_collection_at).toLocaleString()}</small><small>Raw {company.last_raw_count} · Saved {company.last_saved_count} · Review {company.last_review_count}</small>{company.last_collection_error && <small className="error-text">{company.last_collection_error}</small>}</div> : "Not run"}</td><td><div className="row-actions">
        <button className="icon-action collect-action" disabled={collecting !== null || company.discovery_status !== "Verified" || !company.verified_job_board_url} title={`Collect jobs for ${company.name}`} aria-label={`Collect jobs for ${company.name}`} onClick={() => void collectOne(company)}><CollectIcon /></button>
        <button className="icon-action secondary" disabled={collecting !== null || company.discovery_status !== "Verified" || !company.verified_job_board_url} title={`Debug collection for ${company.name}`} aria-label={`Debug collection for ${company.name}`} onClick={() => void collectOne(company, true)}><BugIcon /></button>
        <button className="icon-action secondary" title={`Edit ${company.name}`} aria-label={`Edit ${company.name}`} onClick={() => openEdit(company)}><PencilIcon /></button>
        <button className="icon-action danger" title={`Delete ${company.name}`} aria-label={`Delete ${company.name}`} onClick={() => { setFormError(""); setDeleting(company); }}><TrashIcon /></button>
      </div></td>
    </tr>)}</tbody></table></section>
    {modalOpen && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="company-modal-title">
      <div className="modal-header"><div><h2 id="company-modal-title">{editing ? "Edit Company" : "Add Company"}</h2><p>Save the company’s main website and the verified job board where Opportunity Radar should collect job postings. Job collection must use the Job Board URL only.</p></div><button className="icon-button" type="button" aria-label="Close" onClick={close}>×</button></div>
      <form className="company-form" onSubmit={save}>
        <label className="full">Company Name <span>*</span><input autoFocus required value={form.name} onChange={e => setField("name", e.target.value)} /></label>
        <label>Company Website <small>Optional; the organization’s main public website</small><input type="url" value={form.company_website} onChange={e => setField("company_website", e.target.value)} placeholder="https://company.com" /></label>
        <label>Careers Page URL <small>Optional; the company page linking to careers or jobs</small><input type="url" value={form.careers_page_url} onChange={e => setField("careers_page_url", e.target.value)} placeholder="https://company.com/careers" /></label>
        <div className="discovery-action full"><div><strong>Gather public company details</strong><small>Review the company’s public website and careers pages to gather available company details and identify the public job board used for job collection. Results are saved only when they can be confidently verified.</small></div><button className="secondary" type="button" disabled={discovering || (!form.company_website.trim() && !form.careers_page_url.trim())} onClick={() => void gatherInformation()}>{discovering ? "Gathering…" : "Gather Company Information"}</button></div>
        {discoveryMessage && <div className="success-notice full">{discoveryMessage}</div>}
        {gathered && <section className="gathered-results full"><h3>Proposed gathered values</h3><p>Nothing below will overwrite the form until you choose Apply Gathered Values.</p><div className="proposal-grid">
          <ProposalRow label="Company Website" value={gathered.company_website} source={gathered.sources.company_website} />
          <ProposalRow label="Careers Page URL" value={gathered.careers_page_url} source={gathered.sources.careers_page_url} />
          <ProposalRow label="Verified Job Board URL" value={gathered.verified_job_board_url} source={gathered.sources.verified_job_board_url} />
          <ProposalRow label="Headquarters / Principal Office City" value={gathered.city} source={gathered.sources.city} />
          <ProposalRow label="Headquarters / Principal Office State" value={gathered.state} source={gathered.sources.state} />
          <ProposalRow label="Location Confidence" value={gathered.location_confidence} source={gathered.location_discovery_source} />
          <ProposalRow label="Possible Locations" value={gathered.possible_locations} />
          <ProposalRow label="Founded / Established Year" value={gathered.founded_year} source={gathered.sources.founded_year} />
          <ProposalRow label="Total Assets" value={gathered.total_assets_display} source={gathered.sources.total_assets} />
          <ProposalRow label="Assets As Of Date" value={gathered.assets_as_of_date} source={gathered.sources.assets_as_of_date} />
          <ProposalRow label="Industry" value={gathered.industry} source={gathered.sources.industry} />
          <ProposalRow label="Discovery Status" value={gathered.discovery_status} source={gathered.sources.verified_job_board_url || gathered.sources.careers_page_url} />
        </div><div className="proposal-actions"><button type="button" onClick={applyGathered}>Apply Gathered Values</button></div></section>}
        <label className="full">Verified Job Board URL <span>* before collection</span><small>The actual listing site, such as Workday, ADP, Greenhouse, or ICIMS</small><input type="url" value={form.verified_job_board_url} onChange={e => { setField("verified_job_board_url", e.target.value); setDiscoveryMessage(""); }} placeholder="https://..." /></label>
        <label>Job Board Type<select value={form.job_board_type} onChange={e => setField("job_board_type", e.target.value)}><option value="">Not classified</option><option>Workday</option><option>ADP</option><option>Greenhouse</option><option>Lever</option><option>ICIMS</option><option>Paylocity</option><option>UKG</option><option>SaaS HR</option><option>Dayforce</option><option>Self-Hosted / In-House</option><option>Other External ATS</option></select></label><label>Classification Confidence<select value={form.classification_confidence} onChange={e => setField("classification_confidence", e.target.value)}><option>High</option><option>Medium</option><option>Low</option></select></label>
        <label>Industry<input value={form.industry} onChange={e => setField("industry", e.target.value)} /></label>
        <label>Discovery Status<select value={form.discovery_status} onChange={e => setField("discovery_status", e.target.value)}><option>Not Started</option><option>Discovering</option><option>Verified</option><option>Needs Review</option><option>Failed</option></select></label>
        <h3 className="form-section-title full">Company Information</h3>
        <label>Headquarters / Principal Office City<input value={form.city} onChange={e => setField("city", e.target.value)} /></label>
        <label>Headquarters / Principal Office State<input value={form.state} onChange={e => setField("state", e.target.value)} /></label>
        <label>Location Confidence<select value={form.location_confidence} onChange={e => setField("location_confidence", e.target.value as CompanyForm["location_confidence"])}><option>Verified</option><option>Needs Review</option><option>Not Found</option></select></label>
        <label>Location Discovery Source<input type="url" value={form.location_discovery_source} onChange={e => setField("location_discovery_source", e.target.value)} /></label>
        {form.possible_locations && <label className="full">Possible Locations <small>Choose a headquarters only when the company identifies it as the principal office.</small><textarea rows={2} value={form.possible_locations} onChange={e => setField("possible_locations", e.target.value)} /></label>}
        <label>Founded / Established Year<input type="number" min="1600" max="2200" value={form.founded_year ?? ""} onChange={e => setField("founded_year", e.target.value ? Number(e.target.value) : null)} /></label>
        <label>Total Assets<small>Readable value; numeric amount is stored when recognized</small><input value={form.total_assets_display} onChange={e => { setField("total_assets_display", e.target.value); setField("total_assets", parseAssetValue(e.target.value)); }} placeholder="$4.2 billion" /></label>
        <label>Assets As Of Date<input value={form.assets_as_of_date} onChange={e => setField("assets_as_of_date", e.target.value)} placeholder="December 31, 2025" /></label>
        <label className="full">Notes<textarea rows={4} value={form.notes} onChange={e => setField("notes", e.target.value)} /></label>
        {editing && editing.verified_job_board_url !== form.verified_job_board_url && <div className="refresh-notice full">Changing the Job Board URL will mark this company as needing a manual refresh. No jobs will be collected when you save.</div>}
        {formError && <div className="alert full">{formError}</div>}
        <div className="modal-actions full"><button className="secondary" type="button" onClick={close}>Cancel</button><button type="submit" disabled={saving}>{saving ? "Saving…" : editing ? "Save Changes" : "Save Company"}</button></div>
      </form>
    </section></div>}
    {deleting && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget && !deleteBusy) setDeleting(null); }}><section className="modal delete-modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-company-title">
      <div className="modal-header"><div><h2 id="delete-company-title">Delete Company?</h2><p>This will permanently delete <strong>{deleting.name}</strong>, its saved company information, verified job-board details, collected job listings, raw job candidates, and application-tracking records associated with those jobs. This action cannot be undone.</p></div></div>
      {formError && <div className="alert">{formError}</div>}
      <div className="modal-actions"><button className="secondary" type="button" disabled={deleteBusy} onClick={() => setDeleting(null)}>Cancel</button><button className="danger" type="button" disabled={deleteBusy} onClick={() => void deleteCompany()}>{deleteBusy ? "Deleting…" : "Delete Company"}</button></div>
    </section></div>}
  </>;
}

export function DiscoveryDetails({ company }: { company: Company }) { return <details className="discovery-details"><summary>Discovery Details</summary><dl><dt>Verified URL</dt><dd>{company.verified_job_board_url ? <ExternalLink href={company.verified_job_board_url} empty="—" /> : "Not configured"}</dd><dt>Board type</dt><dd>{company.job_board_type || "Not classified"}</dd><dt>Status</dt><dd>{company.discovery_status}</dd><dt>Confidence</dt><dd>{company.classification_confidence}</dd><dt>Method</dt><dd>{company.discovery_method || "Not recorded"}</dd><dt>Last verified</dt><dd>{company.last_verified_at ? new Date(company.last_verified_at).toLocaleString() : "Never"}</dd></dl></details>; }
function ExternalLink({ href, empty }: { href: string; empty: string }) { return href ? <a href={href} target="_blank" rel="noreferrer" title={href}>{href.replace(/^https?:\/\//, "").replace(/\/$/, "")}</a> : <>{empty}</>; }
function ProposalRow({ label, value, source }: { label: string; value: unknown; source?: string }) { const found = value !== "" && value !== null && value !== undefined; return <div><strong>{label}</strong><span className={found ? "" : "not-found"}>{found ? String(value) : "Not found"}</span>{source && <small>Source: <a href={source} target="_blank" rel="noreferrer">{source}</a></small>}</div>; }
function parseAssetValue(value: string): number | null { const match = value.replace(/,/g, "").match(/\$?\s*([\d.]+)\s*(million|billion|trillion)?/i); if (!match) return null; const amount = Number(match[1]); if (!Number.isFinite(amount)) return null; const multiplier = ({ million: 1e6, billion: 1e9, trillion: 1e12 } as Record<string, number>)[(match[2] || "").toLowerCase()] || 1; return amount * multiplier; }
function PencilIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l11-11-4-4L4 16v4Zm12.5-16.5 4 4 1-1a1.4 1.4 0 0 0 0-2l-2-2a1.4 1.4 0 0 0-2 0l-1 1Z" /></svg>; }
function TrashIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8l1 2h4v2H3V6h4l1-2Zm-2 6h12l-1 11H7L6 10Zm4 2v7h2v-7h-2Zm4 0v7h2v-7h-2Z" /></svg>; }
function CollectIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 3h2v10l3.5-3.5 1.4 1.4-5.9 5.9-5.9-5.9 1.4-1.4L11 13V3ZM4 19h16v2H4v-2Z" /></svg>; }
function BugIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h2v2h2V3h2v2.4a5 5 0 0 1 2 1.5L19 5.5 20.5 7 18.9 8.6c.1.45.1.92.1 1.4h3v2h-3v2h3v2h-3.4A7 7 0 0 1 13 21v-8h-2v8a7 7 0 0 1-5.6-5H2v-2h3v-2H2v-2h3c0-.48.04-.95.13-1.4L3.5 7 5 5.5l2 1.4a5 5 0 0 1 2-1.5V3Z" /></svg>; }
function Jobs({ jobs, companies, onInterested, onReprocessed }: { jobs: Job[]; companies: Company[]; onInterested: (job: Job) => void; onReprocessed: () => Promise<void> }) {
  const [filters, setFilters] = useState({ companyId: "", minimumTarget: "", minimumFull: "", payPeriod: "", health: false, dental: false, vision: false, tuition: false, retirement: false, pto: false, remote: false, incentives: false });
  const [reprocessing, setReprocessing] = useState(false);
  const [reprocessMessage, setReprocessMessage] = useState("");
  const [refreshingJobs, setRefreshingJobs] = useState(false);
  const [jobRefreshProgress, setJobRefreshProgress] = useState<{ current: number; total: number; company: string } | null>(null);
  const [jobRefreshSummary, setJobRefreshSummary] = useState("");
  const filtered = jobs.filter(job => {
    if (filters.companyId && String(job.company_id) !== filters.companyId) return false;
    if (filters.minimumTarget && (job.target_pay_min === null || job.target_pay_min < Number(filters.minimumTarget))) return false;
    if (filters.minimumFull && (job.full_pay_min === null || job.full_pay_min < Number(filters.minimumFull))) return false;
    if (filters.payPeriod && job.pay_period !== filters.payPeriod) return false;
    if (filters.health && !job.has_health_insurance) return false;
    if (filters.dental && !job.has_dental_insurance) return false;
    if (filters.vision && !job.has_vision_insurance) return false;
    if (filters.tuition && !job.has_tuition_reimbursement) return false;
    if (filters.retirement && !job.has_retirement) return false;
    if (filters.pto && !job.has_pto) return false;
    if (filters.remote && !job.has_remote_hybrid) return false;
    if (filters.incentives && !job.incentives_text) return false;
    return true;
  });
  const toggle = (field: "health" | "dental" | "vision" | "tuition" | "retirement" | "pto" | "remote" | "incentives") => setFilters(current => ({ ...current, [field]: !current[field] }));
  const reprocess = async () => { setReprocessing(true); setReprocessMessage(""); try { const result = await api<{ examined: number; updated: number; needs_review: number; failed: number }>("/jobs/reprocess-details", { method: "POST", body: JSON.stringify({}) }); await onReprocessed(); setReprocessMessage(`Reprocessing complete: ${result.updated} updated, ${result.needs_review} need review, ${result.failed} failed.`); } catch (err) { setReprocessMessage(err instanceof Error ? err.message : "Reprocessing failed."); } finally { setReprocessing(false); } };
  const refreshAllJobs = async () => {
    const eligible = companies.filter(company => company.discovery_status === "Verified" && company.verified_job_board_url);
    const skipped = companies.length - eligible.length;
    setRefreshingJobs(true); setJobRefreshSummary("");
    const totals = { refreshed: 0, newJobs: 0, updatedJobs: 0, removed: 0, tracked: 0, review: 0, errors: 0 };
    for (let index = 0; index < eligible.length; index += 1) {
      const company = eligible[index]; setJobRefreshProgress({ current: index + 1, total: eligible.length, company: company.name });
      try {
        const report = await api<{ saved_count: number; updated_count: number; removed_count: number; tracked_no_longer_posted_count: number; rejected_count: number }>(`/companies/${company.id}/collect-jobs`, { method: "POST", body: JSON.stringify({ debug: false }) });
        totals.refreshed += 1; totals.newJobs += report.saved_count; totals.updatedJobs += report.updated_count; totals.removed += report.removed_count; totals.tracked += report.tracked_no_longer_posted_count; totals.review += report.rejected_count;
      } catch { totals.errors += 1; }
      await onReprocessed();
    }
    setJobRefreshProgress(null); setRefreshingJobs(false);
    setJobRefreshSummary(`Job refresh complete: ${totals.refreshed} companies refreshed, ${skipped} skipped without a Verified Job Board URL, ${totals.newJobs} new jobs, ${totals.updatedJobs} existing jobs updated, ${totals.removed} jobs removed, ${totals.tracked} tracked jobs marked No Longer Posted, ${totals.review} Candidate Review items created, ${totals.errors} companies with errors.`);
  };
  return <section className="panel"><div className="panel-heading"><div><h2>Validated jobs</h2><p className="panel-helper">Refreshes public job postings from each company’s saved verified job board. Company details and job-board discovery are not changed.</p></div><div className="button-group"><button disabled={refreshingJobs || reprocessing} onClick={() => void refreshAllJobs()}>{refreshingJobs ? "Refreshing…" : "Refresh All Job Listings"}</button><button className="secondary" disabled={reprocessing || refreshingJobs} onClick={() => void reprocess()}>{reprocessing ? "Reprocessing…" : "Reprocess Existing Jobs"}</button></div></div>{jobRefreshProgress && <div className="discovery-progress" aria-live="polite"><strong>Refreshing {jobRefreshProgress.current} of {jobRefreshProgress.total} companies</strong><span>Currently collecting: {jobRefreshProgress.company}</span></div>}{jobRefreshSummary && <div className="success-notice refresh-summary" aria-live="polite">{jobRefreshSummary}</div>}{reprocessMessage && <div className="success-notice refresh-summary">{reprocessMessage}</div>}<div className="job-filters"><label>Company<select value={filters.companyId} onChange={e => setFilters(current => ({ ...current, companyId: e.target.value }))}><option value="">All companies</option>{companies.map(company => <option key={company.id} value={company.id}>{company.name}</option>)}</select></label><label>Minimum target salary<input type="number" min="0" value={filters.minimumTarget} onChange={e => setFilters(current => ({ ...current, minimumTarget: e.target.value }))} placeholder="150000" /></label><label>Minimum full salary range<input type="number" min="0" value={filters.minimumFull} onChange={e => setFilters(current => ({ ...current, minimumFull: e.target.value }))} placeholder="100000" /></label><label>Pay period<select value={filters.payPeriod} onChange={e => setFilters(current => ({ ...current, payPeriod: e.target.value }))}><option value="">Any</option><option value="hourly">Hourly</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option><option value="annual">Annual</option><option value="other">Other</option></select></label><label><input type="checkbox" checked={filters.health} onChange={() => toggle("health")} /> Health insurance</label><label><input type="checkbox" checked={filters.dental} onChange={() => toggle("dental")} /> Dental insurance</label><label><input type="checkbox" checked={filters.vision} onChange={() => toggle("vision")} /> Vision insurance</label><label><input type="checkbox" checked={filters.retirement} onChange={() => toggle("retirement")} /> 401(k) / retirement</label><label><input type="checkbox" checked={filters.tuition} onChange={() => toggle("tuition")} /> Tuition reimbursement</label><label><input type="checkbox" checked={filters.pto} onChange={() => toggle("pto")} /> PTO</label><label><input type="checkbox" checked={filters.remote} onChange={() => toggle("remote")} /> Remote/hybrid</label><label><input type="checkbox" checked={filters.incentives} onChange={() => toggle("incentives")} /> Incentives/bonus</label></div><p className="filter-count">Showing {filtered.length} of {jobs.length} jobs</p><JobTable jobs={filtered} onInterested={onInterested} /></section>;
}
function JobTable({ jobs, onInterested }: { jobs: Job[]; onInterested: (job: Job) => void }) { const [selected, setSelected] = useState<Job | null>(null); return <><table><thead><tr><th>Role</th><th>Company</th><th>Location</th><th>Type</th><th>Compensation</th><th></th></tr></thead><tbody>{jobs.length ? jobs.map(job => <tr key={job.id}><td><button className="job-title-button" onClick={() => setSelected(job)}><strong>{job.title}</strong></button><small>{job.role_classification}</small></td><td>{job.company_name}</td><td>{job.location || "—"}</td><td>{job.employment_type || "—"}</td><td>{job.pay_display || "Not listed"}</td><td><button className="secondary" disabled={Boolean(job.application_status)} onClick={() => void onInterested(job)}>{job.application_status || "Track"}</button></td></tr>) : <tr><td colSpan={6}>No validated jobs match these filters.</td></tr>}</tbody></table>{selected && <JobDetails job={selected} onClose={() => setSelected(null)} />}</>; }
export function JobDetails({ job, onClose }: { job: Job; onClose: () => void }) { const benefits = benefitRows(job); const hasCompensation = job.target_pay_min !== null || job.full_pay_min !== null || Boolean(job.incentives_text); return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="modal job-details-modal" role="dialog" aria-modal="true" aria-labelledby="job-details-title"><div className="modal-header"><div><h2 id="job-details-title">{job.title}</h2><p>{job.company_name} · {job.location || "Location not listed"}</p></div><button className="icon-button" aria-label="Close job details" onClick={onClose}>×</button></div><div className="job-detail-grid"><section><h3>Compensation</h3>{hasCompensation ? <><dl><dt>Target range</dt><dd>{formatRange(job.target_pay_min, job.target_pay_max, job.pay_currency)}</dd><dt>Full range</dt><dd>{formatRange(job.full_pay_min, job.full_pay_max, job.pay_currency)}</dd><dt>Pay period</dt><dd>{job.pay_period || "Other"}</dd><dt>Incentives / bonus</dt><dd>{job.incentives_text || "Not listed by employer"}</dd></dl>{job.compensation_source_text && <SourceText text={job.compensation_source_text} />}</> : <p>Not listed by employer</p>}</section><section><h3>Benefits</h3>{benefits.length ? <><div className="benefit-rows">{benefits.map(([label, detail]) => <div key={label}><span className="pill">{label}</span><span>{detail}</span></div>)}</div>{job.benefits_source_text && <SourceText text={job.benefits_source_text} />}</> : <p>Not listed by employer</p>}</section></div>{job.description && <section className="job-description"><h3>Job Description</h3><p>{job.description}</p></section>}<div className="modal-actions">{job.detail_url && <a className="button-link" href={job.detail_url} target="_blank" rel="noreferrer">Open employer posting</a>}<button className="secondary" onClick={onClose}>Close</button></div></section></div>; }
function SourceText({ text }: { text: string }) { return <details className="source-text"><summary>View Source Text</summary><p>{text}</p></details>; }
function benefitRows(job: Job): Array<[string, string]> { const rows: Array<[string, string]> = []; if (job.has_health_insurance) rows.push(["Health insurance", "Listed by employer"]); if (job.has_dental_insurance) rows.push(["Dental insurance", "Listed by employer"]); if (job.has_vision_insurance) rows.push(["Vision insurance", "Listed by employer"]); if (job.has_retirement) rows.push(["Retirement / 401(k)", job.retirement_details || "Listed by employer"]); if (job.retirement_match_percent !== null) rows.push(["401(k) match", `Up to ${job.retirement_match_percent}%`]); if (job.retirement_contribution_percent !== null) rows.push(["Employer contribution", `${job.retirement_contribution_percent}% annual contribution`]); if (job.has_pto) rows.push(["PTO", job.pto_details || "Listed by employer"]); if (job.has_tuition_reimbursement) rows.push(["Tuition reimbursement", job.tuition_details || "Listed by employer"]); if (job.has_volunteer_time_off) rows.push(["Volunteer time off", "Listed by employer"]); if (job.has_donation_match) rows.push(["Donation match", "Listed by employer"]); if (job.has_remote_hybrid) rows.push(["Remote/hybrid", "Listed by employer"]); const known = new Set(["Health insurance", "Dental insurance", "Vision insurance", "Retirement", "Paid time off", "Tuition assistance", "Remote/hybrid"]); try { for (const tag of JSON.parse(job.benefit_tags || "[]") as string[]) if (!known.has(tag)) rows.push([tag, "Listed by employer"]); } catch { /* The source field may be legacy non-JSON text. */ } if (job.other_benefit_details) rows.push(["Other benefits", job.other_benefit_details]); return rows; }
function formatPay(value: number | null, currency: string): string { if (value === null) return "Not listed"; try { return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD", maximumFractionDigits: 2 }).format(value); } catch { return `${currency} ${value}`.trim(); } }
function formatRange(minimum: number | null, maximum: number | null, currency: string): string { if (minimum === null) return "Not listed by employer"; return `${formatPay(minimum, currency)}${maximum !== null && maximum !== minimum ? ` – ${formatPay(maximum, currency)}` : ""}`; }
function Applications({ applications }: { applications: Application[] }) { return <section className="panel"><h2>Jobs applied for</h2><table><thead><tr><th>Role</th><th>Company</th><th>Status</th><th>Applied</th></tr></thead><tbody>{applications.length ? applications.map(application => <tr key={application.id}><td>{application.job_title}</td><td>{application.company_name}</td><td><span className="pill">{application.status}</span></td><td>{application.applied_date || "—"}</td></tr>) : <tr><td colSpan={4}>Choose “Track” from a job to begin application tracking.</td></tr>}</tbody></table></section>; }
function CandidateReviews({ candidates, onApproved }: { candidates: CandidateReview[]; onApproved: () => Promise<void> }) { const [approving, setApproving] = useState<number | null>(null); const approve = async (candidate: CandidateReview) => { setApproving(candidate.id); try { await api(`/candidates/${candidate.id}/approve`, { method: "POST" }); await onApproved(); } finally { setApproving(null); } }; return <section className="panel"><h2>Candidate Review</h2><p>These candidates did not pass deterministic validation. Approving one saves it to the Job List with validation source <code>manual_review</code>.</p><table><thead><tr><th>Candidate</th><th>Company</th><th>Location</th><th>Rejection reason</th><th></th></tr></thead><tbody>{candidates.length ? candidates.map(candidate => <tr key={candidate.id}><td><strong>{candidate.title || "Untitled candidate"}</strong>{candidate.detail_url && <small><a href={candidate.detail_url} target="_blank" rel="noreferrer">Open public detail</a></small>}</td><td>{candidate.company_name}</td><td>{candidate.location || "—"}</td><td>{candidate.rejection_reason}</td><td><button disabled={approving !== null} onClick={() => void approve(candidate)}>{approving === candidate.id ? "Approving…" : "Approve"}</button></td></tr>) : <tr><td colSpan={5}>No candidates need manual review.</td></tr>}</tbody></table></section>; }
function ResumeMatch() { return <section className="panel"><h2>Resume Match</h2><p>Resume upload and deterministic scoring are the next build step. Scores will stay local: 80–100 Strong Apply, 60–79 Good Match, 40–59 Stretch Role, and 0–39 Poor Fit.</p></section>; }
