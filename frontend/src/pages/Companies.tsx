import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { AlertTriangle, Plus, X } from "lucide-react";
import type { Company } from "../types/Company";
import { CompanyCard, type CompanyRefreshResult } from "../components/CompanyCard";
import {
  isCompanyCreateResponse,
  isCompanyDeleteResponse,
  isCompanyPage,
  isCompanyRefreshResponse,
  isCompanyUpdateResponse,
  type CompanyPage,
} from "../runtimeSchemas";

import { ApiError, apiJson, userMessage } from "../api";

interface CompaniesProps {
  onViewCompanyJobs: (companyId: string) => void;
  onCompaniesChanged: () => Promise<void>;
  onCompanyRefreshed: () => Promise<void>;
  onCompanyDeleted: (deletedJobIds: string[]) => Promise<void>;
  selectedCompanyName?: string;
  refreshEnabled: boolean;
}

const initialPage: CompanyPage = {
  items: [], page: 1, pageSize: 25, total: 0, totalPages: 1,
  options: { states: [], industries: [], jobBoardTypes: [], discoveryStatuses: [] },
};

interface CompanyFormData {
  name: string;
  companyWebsite: string;
  careersPageUrl: string;
  jobBoardUrl: string;
  industry: string;
  city: string;
  state: string;
  country: string;
  notes: string;
}

const emptyCompany: CompanyFormData = {
  name: "",
  companyWebsite: "",
  careersPageUrl: "",
  jobBoardUrl: "",
  industry: "Financial Services",
  city: "",
  state: "",
  country: "United States",
  notes: "",
};

export function Companies({ onViewCompanyJobs, onCompaniesChanged, onCompanyRefreshed, onCompanyDeleted, selectedCompanyName, refreshEnabled }: CompaniesProps) {
  const [result, setResult] = useState<CompanyPage>(initialPage);
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [industry, setIndustry] = useState("");
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState("");
  const [hasVerifiedJobBoard, setHasVerifiedJobBoard] = useState("");
  const [hasActiveJobs, setHasActiveJobs] = useState("");
  const [sortBy, setSortBy] = useState("companyName");
  const [sortDirection, setSortDirection] = useState("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [refreshToken, setRefreshToken] = useState(0);
  const [loading, setLoading] = useState(true);
  const [pageLoaded, setPageLoaded] = useState(false);
  const [pageError, setPageError] = useState("");
  const [editingCompany, setEditingCompany] = useState<Company | null | undefined>(undefined);
  const [deletingCompany, setDeletingCompany] = useState<Company | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshingCompanyIds, setRefreshingCompanyIds] = useState<Set<string>>(() => new Set());
  const [refreshResults, setRefreshResults] = useState<Record<string, CompanyRefreshResult>>({});

  useEffect(() => {
    if (selectedCompanyName) {
      setQuery(selectedCompanyName);
      setPage(1);
    }
  }, [selectedCompanyName]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true);
      setPageError("");
      const parameters = new URLSearchParams({
        page: String(page), pageSize: String(pageSize), sortBy, sortDirection,
      });
      if (query.trim()) parameters.set("search", query.trim());
      if (state) parameters.set("state", state);
      if (industry) parameters.set("industry", industry);
      if (platform) parameters.set("jobBoardType", platform);
      if (status) parameters.set("discoveryStatus", status);
      if (hasVerifiedJobBoard) parameters.set("hasVerifiedJobBoard", hasVerifiedJobBoard);
      if (hasActiveJobs) parameters.set("hasActiveJobs", hasActiveJobs);
      try {
        const nextResult = await apiJson<unknown>(`/companies-page?${parameters}`, { signal: controller.signal }, "Could not load companies.");
        if (!isCompanyPage(nextResult)) throw new ApiError("Could not load companies. The server returned an invalid response.");
        setResult(nextResult);
        setPageLoaded(true);
        if (nextResult.page !== page) setPage(nextResult.page);
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setPageError(userMessage(caught, "Could not load companies."));
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, query ? 250 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [hasActiveJobs, hasVerifiedJobBoard, industry, page, pageSize, platform, query, refreshToken, sortBy, sortDirection, state, status]);

  const updateFirstPage = (setter: (value: string) => void) => (value: string) => {
    setter(value);
    setPage(1);
  };

  const clearFilters = () => {
    setQuery("");
    setState("");
    setIndustry("");
    setPlatform("");
    setStatus("");
    setHasVerifiedJobBoard("");
    setHasActiveJobs("");
    setSortBy("companyName");
    setSortDirection("asc");
    setPageSize(25);
    setPage(1);
  };

  const saveCompany = async (form: CompanyFormData) => {
    setBusy(true);
    setError("");
    try {
      const isEdit = Boolean(editingCompany);
      const saved = await apiJson<unknown>(`/companies${isEdit ? `/${editingCompany?.id}` : ""}`, {
        method: isEdit ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      }, "Could not save the company.");
      if (isEdit) {
        if (!isCompanyUpdateResponse(saved) || saved.company.id !== editingCompany?.id) {
          throw new ApiError("Could not save the company. The server returned an invalid response.");
        }
      } else if (!isCompanyCreateResponse(saved)) {
        throw new ApiError("Could not save the company. The server returned an invalid response.");
      }
      await onCompaniesChanged();
      setRefreshToken((current) => current + 1);
      setEditingCompany(undefined);
      setNotice(isEdit ? "Company updated." : "Company added.");
    } catch (caught) {
      setError(userMessage(caught, "Could not save the company."));
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deletingCompany) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<unknown>(`/companies/${deletingCompany.id}`, { method: "DELETE" }, "Could not delete the company.");
      if (!isCompanyDeleteResponse(result) || result.deletedCompanyId !== deletingCompany.id
        || result.deletedJobs !== result.deletedJobIds.length) {
        throw new ApiError("Could not delete the company. The server returned an invalid response.");
      }
      await onCompanyDeleted(result.deletedJobIds);
      setRefreshToken((current) => current + 1);
      setDeletingCompany(null);
      setNotice("Company and related job data deleted.");
    } catch (caught) {
      setError(userMessage(caught, "Could not delete the company."));
    } finally {
      setBusy(false);
    }
  };

  const refreshCompany = async (company: Company) => {
    if (!refreshEnabled) {
      setRefreshResults((current) => ({ ...current, [company.id]: {
        status: "failed", companyId: company.id, companyName: company.name, companyMetadataChanged: false,
        totalJobsDiscovered: 0, newJobs: 0, updatedJobs: 0, removedOrClosedJobs: 0,
        activeJobs: 0,
        warnings: [], errors: ["Company refresh is disabled for the initial production release."],
      } }));
      return;
    }
    setRefreshingCompanyIds((current) => new Set(current).add(company.id));
    setRefreshResults((current) => { const next = { ...current }; delete next[company.id]; return next; });
    try {
      const result = await apiJson<unknown>(`/companies/${company.id}/refresh`, { method: "POST" }, `Could not refresh ${company.name}.`);
      if (!isCompanyRefreshResponse(result) || result.companyId !== company.id) {
        throw new ApiError(`Could not refresh ${company.name}. The server returned an invalid response.`);
      }
      setRefreshResults((current) => ({ ...current, [company.id]: result }));
      await onCompanyRefreshed();
      setRefreshToken((current) => current + 1);
    } catch (caught) {
      const message = userMessage(caught, `Could not refresh ${company.name}.`);
      setRefreshResults((current) => ({ ...current, [company.id]: {
        status: "failed", companyId: company.id, companyName: company.name, companyMetadataChanged: false,
        totalJobsDiscovered: 0, newJobs: 0, updatedJobs: 0, removedOrClosedJobs: 0,
        activeJobs: 0,
        warnings: [], errors: [message],
      } }));
    } finally {
      setRefreshingCompanyIds((current) => { const next = new Set(current); next.delete(company.id); return next; });
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-slate-400">{pageLoaded && !pageError ? rangeLabel(result) : loading ? "Loading companies..." : "Company data unavailable"}</p>
        <button className="btn btn-primary" onClick={() => { setError(""); setEditingCompany(null); }}>
          <Plus size={17} /> Add Company
        </button>
      </div>
      {notice ? <div className="rounded-md border border-emerald-800 bg-emerald-950/50 px-4 py-3 text-sm text-emerald-300" role="status">{notice}</div> : null}
      <Filters
        query={query} setQuery={updateFirstPage(setQuery)} state={state} setState={updateFirstPage(setState)}
        industry={industry} setIndustry={updateFirstPage(setIndustry)} platform={platform} setPlatform={updateFirstPage(setPlatform)}
        status={status} setStatus={updateFirstPage(setStatus)} hasVerifiedJobBoard={hasVerifiedJobBoard}
        setHasVerifiedJobBoard={updateFirstPage(setHasVerifiedJobBoard)} hasActiveJobs={hasActiveJobs}
        setHasActiveJobs={updateFirstPage(setHasActiveJobs)} sortBy={sortBy} setSortBy={updateFirstPage(setSortBy)}
        sortDirection={sortDirection} setSortDirection={updateFirstPage(setSortDirection)} pageSize={pageSize}
        setPageSize={(value) => { setPageSize(value); setPage(1); }} options={result.options} onClear={clearFilters}
      />
      {pageError ? <div className="flex flex-col gap-3 rounded-md border border-red-900 bg-red-950/40 px-4 py-3 text-sm text-red-300 sm:flex-row sm:items-center sm:justify-between" role="alert"><span>{pageError}</span><button className="btn shrink-0" type="button" onClick={() => setRefreshToken((current) => current + 1)}>Retry</button></div> : null}
      {!pageLoaded && loading ? <div className="card p-8 text-center text-slate-400" role="status">Loading companies...</div> : null}
      <div className={`space-y-3 ${loading ? "opacity-60" : ""}`} aria-busy={loading} hidden={Boolean(pageError) || !pageLoaded}>
        {result.items.map((company) => <CompanyCard key={company.id} company={company} appliedCount={company.appliedCount || 0} jobCount={company.activeJobCount || 0} onViewJobs={onViewCompanyJobs} onRefresh={(selected) => void refreshCompany(selected)} refreshEnabled={refreshEnabled} isRefreshing={refreshingCompanyIds.has(company.id)} refreshResult={refreshResults[company.id]} onEdit={(selected) => { setError(""); setEditingCompany(selected); }} onDelete={(selected) => { setError(""); setDeletingCompany(selected); }} forceOpen={sameCompany(company.name, selectedCompanyName)} />)}
        {!loading && !pageError && !result.items.length ? <Empty message="No companies match the current filters." /> : null}
      </div>
      {pageLoaded && !pageError ? <Pagination result={result} page={page} setPage={setPage} /> : null}
      {editingCompany !== undefined ? <CompanyModal company={editingCompany} busy={busy} error={error} onClose={() => setEditingCompany(undefined)} onSave={saveCompany} /> : null}
      {deletingCompany ? <DeleteModal company={deletingCompany} busy={busy} error={error} onClose={() => setDeletingCompany(null)} onDelete={confirmDelete} /> : null}
    </div>
  );
}

function CompanyModal({ company, busy, error, onClose, onSave }: { company: Company | null; busy: boolean; error: string; onClose: () => void; onSave: (form: CompanyFormData) => Promise<void> }) {
  const [form, setForm] = useState<CompanyFormData>(() => company ? {
    name: company.name,
    companyWebsite: company.officialWebsite || company.knownWebsite || "",
    careersPageUrl: company.careersPageUrl || "",
    jobBoardUrl: company.jobBoardUrl || "",
    industry: company.industry || "Financial Services",
    city: company.city || "",
    state: company.state || "",
    country: company.country || "United States",
    notes: company.notes || "",
  } : emptyCompany);
  const update = (key: keyof CompanyFormData, value: string) => setForm((current) => ({ ...current, [key]: value }));
  const submit = (event: FormEvent) => { event.preventDefault(); void onSave(form); };

  return (
    <Modal title={company ? "Edit Company" : "Add Company"} onClose={onClose}>
      <form className="space-y-4" onSubmit={submit}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Company Name" required value={form.name} onChange={(value) => update("name", value)} />
          <Field label="Industry" value={form.industry} onChange={(value) => update("industry", value)} />
          <Field label="Company Website" type="url" value={form.companyWebsite} onChange={(value) => update("companyWebsite", value)} />
          <Field label="Careers Page URL" type="url" value={form.careersPageUrl} onChange={(value) => update("careersPageUrl", value)} />
          <Field label="Verified Job Board URL" type="url" value={form.jobBoardUrl} onChange={(value) => update("jobBoardUrl", value)} />
          <Field label="City" value={form.city} onChange={(value) => update("city", value)} />
          <Field label="State" value={form.state} onChange={(value) => update("state", value)} />
          <Field label="Country" value={form.country} onChange={(value) => update("country", value)} />
        </div>
        <label className="block text-sm text-slate-300">Notes<textarea className="field mt-1 min-h-24 resize-y" value={form.notes} onChange={(event) => update("notes", event.target.value)} /></label>
        {error ? <p className="text-sm text-red-400" role="alert">{error}</p> : null}
        <div className="flex justify-end gap-3">
          <button className="btn" type="button" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? "Saving..." : company ? "Save Changes" : "Add Company"}</button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteModal({ company, busy, error, onClose, onDelete }: { company: Company; busy: boolean; error: string; onClose: () => void; onDelete: () => Promise<void> }) {
  return (
    <Modal title="Delete Company?" onClose={onClose}>
      <div className="flex gap-3 rounded-md border border-red-900 bg-red-950/40 p-4 text-sm leading-6 text-red-100">
        <AlertTriangle className="mt-1 shrink-0 text-red-400" size={20} />
        <p>This will permanently delete <strong>{company.name}</strong>, its saved company information, job-board details, collected job listings, raw job candidates, and application-tracking records associated with those jobs. This action cannot be undone.</p>
      </div>
      {error ? <p className="mt-4 text-sm text-red-400" role="alert">{error}</p> : null}
      <div className="mt-5 flex justify-end gap-3">
        <button className="btn" type="button" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn-danger" type="button" onClick={() => void onDelete()} disabled={busy}>{busy ? "Deleting..." : "Delete Company"}</button>
      </div>
    </Modal>
  );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/70 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="panel my-6 w-full max-w-2xl p-5" role="dialog" aria-modal="true" aria-labelledby="company-modal-title">
        <header className="mb-5 flex items-center justify-between gap-4">
          <h3 id="company-modal-title" className="text-xl font-semibold text-white">{title}</h3>
          <button className="icon-btn" type="button" onClick={onClose} title="Close" aria-label="Close"><X size={18} /></button>
        </header>
        {children}
      </section>
    </div>
  );
}

function Field({ label, value, onChange, required, type = "text" }: { label: string; value: string; onChange: (value: string) => void; required?: boolean; type?: string }) {
  return <label className="block text-sm text-slate-300">{label}{required ? " *" : ""}<input className="field mt-1" type={type} required={required} value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

function sameCompany(companyName: string, selectedCompanyName?: string) { return selectedCompanyName ? companyName.toLowerCase() === selectedCompanyName.toLowerCase() : false; }

interface FiltersProps {
  query: string; setQuery: (value: string) => void;
  state: string; setState: (value: string) => void;
  industry: string; setIndustry: (value: string) => void;
  platform: string; setPlatform: (value: string) => void;
  status: string; setStatus: (value: string) => void;
  hasVerifiedJobBoard: string; setHasVerifiedJobBoard: (value: string) => void;
  hasActiveJobs: string; setHasActiveJobs: (value: string) => void;
  sortBy: string; setSortBy: (value: string) => void;
  sortDirection: string; setSortDirection: (value: string) => void;
  pageSize: number; setPageSize: (value: number) => void;
  options: CompanyPage["options"];
  onClear: () => void;
}

function Filters(props: FiltersProps) {
  return <div className="panel grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-4">
    <input className="field md:col-span-2" aria-label="Search companies" placeholder="Search company, city, state, or website" value={props.query} onChange={(event) => props.setQuery(event.target.value)} />
    <Select value={props.state} setValue={props.setState} values={props.options.states} label="State" />
    <Select value={props.industry} setValue={props.setIndustry} values={props.options.industries} label="Industry" />
    <Select value={props.platform} setValue={props.setPlatform} values={props.options.jobBoardTypes} label="Job Board Type" />
    <Select value={props.status} setValue={props.setStatus} values={props.options.discoveryStatuses} label="Discovery Status" />
    <BooleanSelect value={props.hasVerifiedJobBoard} setValue={props.setHasVerifiedJobBoard} label="Has Verified Job Board URL" />
    <BooleanSelect value={props.hasActiveJobs} setValue={props.setHasActiveJobs} label="Has Active Jobs" />
    <select className="field" aria-label="Sort By" value={props.sortBy} onChange={(event) => props.setSortBy(event.target.value)}>
      <option value="companyName">Company Name</option><option value="city">City</option><option value="state">State</option>
      <option value="jobBoardType">Job Board Type</option><option value="discoveryStatus">Discovery Status</option>
      <option value="jobCount">Job Count</option><option value="lastCollectionDate">Last Collection Date</option>
    </select>
    <select className="field" aria-label="Sort Direction" value={props.sortDirection} onChange={(event) => props.setSortDirection(event.target.value)}><option value="asc">Ascending</option><option value="desc">Descending</option></select>
    <select className="field" aria-label="Companies per page" value={props.pageSize} onChange={(event) => props.setPageSize(Number(event.target.value))}><option value={25}>25 per page</option><option value={50}>50 per page</option><option value={100}>100 per page</option></select>
    <button className="btn" type="button" onClick={props.onClear}>Clear Filters</button>
  </div>;
}

function Select({ value, setValue, values, label }: { value: string; setValue: (value: string) => void; values: string[]; label: string }) { return <select className="field" value={value} aria-label={label} onChange={(event) => setValue(event.target.value)}><option value="">All {label}</option>{values.map((item) => <option key={item}>{item}</option>)}</select>; }
function BooleanSelect({ value, setValue, label }: { value: string; setValue: (value: string) => void; label: string }) { const shortLabel = label === "Has Verified Job Board URL" ? "Verified Job Board" : "Has Active Jobs"; return <select className="field" value={value} aria-label={label} title={label} onChange={(event) => setValue(event.target.value)}><option value="">{shortLabel}: All</option><option value="true">{shortLabel}: Yes</option><option value="false">{shortLabel}: No</option></select>; }
function Pagination({ result, page, setPage }: { result: CompanyPage; page: number; setPage: (page: number) => void }) { return <div className="flex flex-col gap-3 border-t border-slate-800 pt-4 text-sm text-slate-400 sm:flex-row sm:items-center sm:justify-between"><span>{rangeLabel(result)}</span><div className="flex items-center gap-3"><button className="btn" type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><span>Page {result.page} of {result.totalPages}</span><button className="btn" type="button" disabled={page >= result.totalPages || result.total === 0} onClick={() => setPage(page + 1)}>Next</button></div></div>; }
function rangeLabel(result: CompanyPage) { const start = result.total ? (result.page - 1) * result.pageSize + 1 : 0; const end = Math.min(result.page * result.pageSize, result.total); return `Showing ${start}\u2013${end} of ${result.total} companies`; }
function Empty({ message }: { message: string }) { return <div className="card p-8 text-center text-slate-400">{message}</div>; }
