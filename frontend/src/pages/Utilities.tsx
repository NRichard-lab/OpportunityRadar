import { useEffect, useMemo, useState, type ChangeEvent, type ReactNode } from "react";
import { Building2, CalendarClock, DatabaseBackup, Download, History, Mail, RefreshCw, RotateCw, Save, Search, Send, Upload, X, type LucideIcon } from "lucide-react";
import type { MaintenanceJobState, MaintenanceJobsState, MaintenanceRun } from "../types/Maintenance";
import type { FeatureFlags } from "../types/FeatureFlags";
import {
  isEmailDigestMutationResponse,
  isEmailHistoryPayload,
  isEmailSettingsPayload,
  isEmailStatusPayload,
  isMaintenanceHistoryResponse,
  isMessageResponse,
  isScheduleMutationResponse,
  isUtilityCancelResponse,
  isUtilityRunResponse,
  type EmailDigestPayload,
  type EmailSettingsPayload,
  type EmailStatusPayload,
} from "../runtimeSchemas";
import { ApiError, apiJson, userMessage } from "../api";

type UtilityKey = "refresh-missing-company-information" | "refresh-company-discovery" | "refresh-all-job-listings" | "reprocess-saved-jobs" | "create-backup" | "export-data" | "import-data";
interface UtilitiesProps { maintenance: MaintenanceJobsState; onMaintenanceRefresh: () => Promise<MaintenanceJobsState>; features: FeatureFlags; }
interface UtilityPresentation { key: UtilityKey; confirmation: string; icon: LucideIcon; }
type UtilityTab = "refresh" | "data" | "email";

const timezoneOptions = ["America/Denver", "America/Chicago", "America/New_York", "America/Los_Angeles", "UTC"];
const refreshSections: Array<{ title: string; actions: UtilityPresentation[] }> = [
  { title: "Company Maintenance", actions: [
    { key: "refresh-missing-company-information", confirmation: "This checks companies with missing or unverified information and fills only blank fields. Existing verified or user-entered information will not be replaced.", icon: Building2 },
    { key: "refresh-company-discovery", confirmation: "This rechecks companies that need review or do not have a verified job board. Companies with current verified information will be skipped.", icon: Search },
  ] },
  { title: "Job Maintenance", actions: [
    { key: "refresh-all-job-listings", confirmation: "This refreshes saved job listings from verified public job boards. Company details will not be changed.", icon: RefreshCw },
    { key: "reprocess-saved-jobs", confirmation: "This reprocesses jobs already saved in Opportunity Radar without visiting company websites or collecting new jobs.", icon: RotateCw },
  ] },
];
const dataActions: UtilityPresentation[] = [
  { key: "create-backup", confirmation: "This creates a timestamped recovery copy of the database and current export files. Existing backups will not be changed.", icon: DatabaseBackup },
  { key: "export-data", confirmation: "This regenerates the current Excel and JSON exports from the Opportunity Radar database.", icon: Download },
  { key: "import-data", confirmation: "Select a JSON or Excel file to import. Matching stable IDs will be updated and new records will be added.", icon: Upload },
];

export function Utilities({ maintenance, onMaintenanceRefresh, features }: UtilitiesProps) {
  const initialTab = new URLSearchParams(window.location.search).get("tab");
  const [activeTab, setActiveTab] = useState<UtilityTab>(initialTab === "data" || initialTab === "email" ? initialTab : "refresh");
  const [pendingAction, setPendingAction] = useState<UtilityPresentation | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [scheduleJob, setScheduleJob] = useState<MaintenanceJobState | null>(null);
  const [historyJob, setHistoryJob] = useState<MaintenanceJobState | null>(null);
  const [historyRuns, setHistoryRuns] = useState<MaintenanceRun[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  useEffect(() => { void onMaintenanceRefresh(); }, []);
  useEffect(() => { const url = new URL(window.location.href); url.searchParams.set("tab", activeTab); window.history.replaceState(null, "", url); }, [activeTab]);
  const jobsByKey = useMemo(() => new Map(maintenance.jobs.map((job) => [job.jobKey, job])), [maintenance.jobs]);

  const startAction = async () => {
    if (!pendingAction || (pendingAction.key === "import-data" && !importFile)) return;
    if (!actionEnabled(pendingAction.key, features)) {
      setError("This utility is disabled for the initial production release.");
      return;
    }
    setStarting(true); setError("");
    try {
      const action = pendingAction;
      const selectedFile = importFile;
      const suffix = action.key === "import-data" && selectedFile ? `?filename=${encodeURIComponent(selectedFile.name)}` : "";
      const startedRun = await apiJson<unknown>(`/maintenance/jobs/${action.key}/run${suffix}`, {
        method: "POST",
        headers: action.key === "import-data" ? { "Content-Type": "application/octet-stream" } : undefined,
        body: action.key === "import-data" && selectedFile ? await selectedFile.arrayBuffer() : undefined,
      }, "Could not start this action.");
      if (!isUtilityRunResponse(startedRun) || startedRun.action !== action.key || startedRun.job_key !== action.key) {
        throw new ApiError("Could not start this action. The server returned an invalid response.");
      }
      setPendingAction(null); setImportFile(null);
      await onMaintenanceRefresh();
    } catch (caught) { setError(userMessage(caught, "Could not start this action.")); }
    finally { setStarting(false); }
  };

  const saveSchedule = async (job: MaintenanceJobState, enabled: boolean, runTime: string, timezone: string) => {
    if (!features.schedules || (enabled && !actionEnabled(job.jobKey as UtilityKey, features))) {
      throw new Error("Schedules are disabled for this utility in the initial production release.");
    }
    setError("");
    const updatedSchedule = await apiJson<unknown>(`/maintenance/jobs/${job.jobKey}/schedule`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled, runTime, timezone }),
    }, "Could not update this schedule.");
    if (!isScheduleMutationResponse(updatedSchedule) || updatedSchedule.jobKey !== job.jobKey) {
      throw new ApiError("Could not update this schedule. The server returned an invalid response.");
    }
    await onMaintenanceRefresh();
  };

  const toggleSchedule = async (job: MaintenanceJobState) => {
    if (!job.schedule || !features.schedules) {
      setError("Schedules are disabled for the initial production release.");
      return;
    }
    try { await saveSchedule(job, !job.schedule.enabled, job.schedule.runTime, job.schedule.timezone); }
    catch (caught) { setError(userMessage(caught, "Could not update this schedule.")); }
  };

  const openHistory = async (job: MaintenanceJobState) => {
    if (!features.utilities) return;
    setHistoryJob(job); setHistoryRuns([]); setHistoryLoading(true); setError("");
    try {
      const result = await apiJson<unknown>(`/maintenance/jobs/${job.jobKey}/history?limit=20`, {}, "Could not load run history.");
      if (!isMaintenanceHistoryResponse(result) || result.jobKey !== job.jobKey) {
        throw new ApiError("Could not load run history. The server returned an invalid response.");
      }
      setHistoryRuns(result.runs);
    } catch (caught) { setError(userMessage(caught, "Could not load run history.")); }
    finally { setHistoryLoading(false); }
  };

  const cancelRun = async (runId: string) => {
    if (!features.utilities) return;
    try {
      const cancelledRun = await apiJson<unknown>(`/maintenance/runs/${runId}/cancel`, { method: "POST" }, "Could not cancel this action.");
      if (!isUtilityCancelResponse(cancelledRun) || cancelledRun.id !== runId || cancelledRun.run_id !== runId) {
        throw new ApiError("Could not cancel this action. The server returned an invalid response.");
      }
      await onMaintenanceRefresh();
    } catch (caught) { setError(userMessage(caught, "Could not cancel this action.")); }
  };

  return <div className="space-y-8">
    <nav className="flex gap-1 border-b border-radar-line" aria-label="Utilities sections">
      {([{"key":"refresh","label":"Refresh"},{"key":"data","label":"Data & Recovery"},{"key":"email","label":"Email"}] as Array<{key: UtilityTab; label: string}>).map((tab) => <button className={`border-b-2 px-4 py-3 text-sm font-medium transition ${activeTab === tab.key ? "border-radar-highlight text-white" : "border-transparent text-slate-400 hover:text-white"}`} type="button" key={tab.key} aria-current={activeTab === tab.key ? "page" : undefined} onClick={() => setActiveTab(tab.key)}>{tab.label}</button>)}
    </nav>
    {maintenance.activeRuns.map((run) => <ProgressPanel key={run.id} run={run} onCancel={cancelRun} />)}
    {error && !pendingAction && !scheduleJob && !historyJob ? <Alert message={error} /> : null}
    {activeTab === "refresh" && (!maintenance.jobs.length ? <p className="text-sm text-slate-400">Loading utilities...</p> : refreshSections.map((section) => <section key={section.title}>
      <h2 className="text-lg font-semibold text-white">{section.title}</h2>
      <div className="mt-3 grid gap-4 xl:grid-cols-2">{section.actions.map((action) => {
        const job = jobsByKey.get(action.key);
        const enabled = actionEnabled(action.key, features);
        return job ? <MaintenanceCard key={action.key} action={action} job={job} enabled={enabled} schedulesEnabled={features.schedules && enabled} onRun={() => { if (!enabled) return; setError(""); setImportFile(null); setPendingAction(action); }} onToggle={() => void toggleSchedule(job)} onEditSchedule={() => { if (features.schedules && enabled) setScheduleJob(job); }} onHistory={() => void openHistory(job)} /> : null;
      })}</div>
    </section>))}
    {activeTab === "data" ? <section><h2 className="text-lg font-semibold text-white">Data & Recovery</h2><div className="mt-3 grid gap-4 xl:grid-cols-2">{dataActions.map((action) => { const job = jobsByKey.get(action.key); const enabled = actionEnabled(action.key, features); return job ? <MaintenanceCard key={action.key} action={action} job={job} enabled={enabled} schedulesEnabled={features.schedules && enabled} onRun={() => { if (!enabled) return; setError(""); setImportFile(null); setPendingAction(action); }} onToggle={() => void toggleSchedule(job)} onEditSchedule={() => { if (features.schedules && enabled) setScheduleJob(job); }} onHistory={() => void openHistory(job)} /> : null; })}</div></section> : null}
    {activeTab === "email" ? <EmailTab /> : null}
    {pendingAction ? <ConfirmationModal action={pendingAction} job={jobsByKey.get(pendingAction.key)} file={importFile} error={error} starting={starting} onFileChange={(event) => setImportFile(event.target.files?.[0] || null)} onCancel={() => { setPendingAction(null); setImportFile(null); setError(""); }} onConfirm={startAction} /> : null}
    {scheduleJob?.schedule ? <ScheduleModal job={scheduleJob} error={error} onCancel={() => { setScheduleJob(null); setError(""); }} onSave={async (enabled, runTime, timezone) => { try { await saveSchedule(scheduleJob, enabled, runTime, timezone); setScheduleJob(null); } catch (caught) { setError(userMessage(caught, "Could not update this schedule.")); } }} /> : null}
    {historyJob ? <HistoryModal job={historyJob} runs={historyRuns} loading={historyLoading} error={error} onClose={() => { setHistoryJob(null); setError(""); }} /> : null}
  </div>;
}

const emptyEmailSettings: EmailSettingsPayload = { smtpHost: "", smtpPort: 465, security: "ssl_tls", smtpUsername: "", fromEmail: "", fromName: "Opportunity Radar", replyToEmail: "", dailyEnabled: false, recipientEmail: "", sendAfterRefresh: true, sendWhenEmpty: false, hasSmtpPassword: false, trackingStartedAt: "", configured: false };

function EmailTab() {
  const [settings, setSettings] = useState<EmailSettingsPayload>(emptyEmailSettings);
  const [password, setPassword] = useState("");
  const [testRecipient, setTestRecipient] = useState("");
  const [status, setStatus] = useState<EmailStatusPayload | null>(null);
  const [history, setHistory] = useState<EmailDigestPayload[]>([]);
  const [selectedDigest, setSelectedDigest] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = async () => {
    const [loadedSettings, loadedStatus, loadedHistory] = await Promise.all([
      apiJson<unknown>("/settings/email", {}, "Email settings could not be loaded."),
      apiJson<unknown>("/email/status", {}, "Email status could not be loaded."),
      apiJson<unknown>("/email/history?limit=20", {}, "Email history could not be loaded."),
    ]);
    if (!isEmailSettingsPayload(loadedSettings) || !isEmailStatusPayload(loadedStatus)
      || !isEmailHistoryPayload(loadedHistory)) {
      throw new ApiError("Email settings could not be loaded. The server returned an invalid response.");
    }
    setSettings(loadedSettings); setStatus(loadedStatus); setHistory(loadedHistory.history);
    setTestRecipient((current) => current || loadedSettings.recipientEmail);
    setError("");
  };

  const loadVisible = async () => {
    setLoading(true);
    try { await load(); }
    catch (caught) { setError(userMessage(caught, "Email settings could not be loaded.")); }
    finally { setLoading(false); }
  };

  useEffect(() => { void loadVisible(); }, []);

  const save = async () => {
    setBusy("save"); setError(""); setMessage("");
    try {
      const result = await apiJson<unknown>("/settings/email", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...settings, smtpPassword: password }) }, "Email settings could not be saved.");
      if (!isEmailSettingsPayload(result)) throw new ApiError("Email settings could not be saved. The server returned an invalid response.");
      setSettings(result); setPassword(""); setMessage("Email settings saved.");
      try { await load(); }
      catch (reloadError) {
        setStatus(null); setHistory([]);
        setError(userMessage(reloadError, "Email settings were saved, but current email status could not be reloaded."));
      }
    } catch (caught) { setError(userMessage(caught, "Email settings could not be saved.")); }
    finally { setBusy(""); }
  };

  const runEmailAction = async (action: "test" | "digest") => {
    setBusy(action); setError(""); setMessage("");
    try {
      const result = await apiJson<unknown>(action === "test" ? "/settings/email/test" : "/email/send-new-jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: action === "test" ? JSON.stringify({ recipient: testRecipient }) : undefined }, "The email could not be sent.");
      if (action === "test") {
        if (!isMessageResponse(result)) throw new ApiError("The test email response was invalid.");
        setMessage(result.message || "Test email sent.");
      } else {
        if (!isEmailDigestMutationResponse(result)) {
          throw new ApiError("The daily email response was invalid.");
        }
        setMessage(result.status === "Skipped - No New Jobs" ? "No email was sent because there are no new jobs." : `Daily job email sent with ${result.jobCount} new jobs.`);
      }
      try { await load(); }
      catch (reloadError) {
        setStatus(null); setHistory([]);
        setError(userMessage(reloadError, "The email was sent, but current email status could not be reloaded."));
      }
    } catch (caught) { setError(userMessage(caught, "The email could not be sent.")); }
    finally { setBusy(""); }
  };

  const update = <K extends keyof EmailSettingsPayload>(key: K, value: EmailSettingsPayload[K]) => setSettings((current) => ({ ...current, [key]: value }));
  if (loading) return <p className="text-sm text-slate-400" role="status">Loading email settings...</p>;
  if (error && !status) return <div className="panel p-6 text-center" role="alert"><p className="text-sm text-red-300">{error}</p><button className="btn mt-4" type="button" onClick={() => void loadVisible()}>Retry</button></div>;
  return <div className="space-y-6">
    {message ? <div className="rounded-md border border-emerald-800 bg-emerald-950/30 px-4 py-3 text-sm text-emerald-200" role="status">{message}</div> : null}
    {error ? <Alert message={error} /> : null}
    <section className="panel p-5">
      <div className="flex items-start gap-3"><Mail className="mt-0.5 text-radar-highlight" size={20} /><div><h2 className="font-semibold text-white">Email Provider</h2><p className="mt-1 text-sm text-slate-400">Enter the SMTP settings supplied by your email provider.</p></div></div>
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <EmailField label="SMTP Host"><input className="field mt-1" value={settings.smtpHost} placeholder="SMTP server" onChange={(event) => update("smtpHost", event.target.value)} /></EmailField>
        <EmailField label="SMTP Port"><input className="field mt-1" type="number" min={1} max={65535} value={settings.smtpPort} onChange={(event) => update("smtpPort", Number(event.target.value))} /></EmailField>
        <EmailField label="Security"><select className="field mt-1" value={settings.security} onChange={(event) => update("security", event.target.value as EmailSettingsPayload["security"])}><option value="ssl_tls">SSL/TLS</option><option value="starttls">STARTTLS</option><option value="none">None</option></select></EmailField>
        <EmailField label="SMTP Username"><input className="field mt-1" autoComplete="username" value={settings.smtpUsername} onChange={(event) => update("smtpUsername", event.target.value)} /></EmailField>
        <EmailField label="SMTP Password / App Password"><input className="field mt-1" type="password" autoComplete="new-password" value={password} placeholder={settings.hasSmtpPassword ? "Saved - leave blank to keep" : "Enter password"} onChange={(event) => setPassword(event.target.value)} /></EmailField>
        <EmailField label="From Email"><input className="field mt-1" type="email" value={settings.fromEmail} onChange={(event) => update("fromEmail", event.target.value)} /></EmailField>
        <EmailField label="From Name"><input className="field mt-1" value={settings.fromName} placeholder="Opportunity Radar" onChange={(event) => update("fromName", event.target.value)} /></EmailField>
        <EmailField label="Reply-To Email (optional)"><input className="field mt-1" type="email" value={settings.replyToEmail} onChange={(event) => update("replyToEmail", event.target.value)} /></EmailField>
      </div>
      <div className="mt-5 flex justify-end"><button className="btn btn-primary" type="button" disabled={Boolean(busy)} onClick={() => void save()}><Save size={16} />{busy === "save" ? "Saving..." : "Save Email Settings"}</button></div>
    </section>

    <section className="panel p-5">
      <h2 className="font-semibold text-white">Daily Job Email</h2>
      <div className="mt-4 space-y-4">
        <ToggleRow label="Enable Daily Job Email" checked={settings.dailyEnabled} onChange={(value) => update("dailyEnabled", value)} />
        <EmailField label="Recipient Email"><input className="field mt-1 max-w-xl" type="email" value={settings.recipientEmail} onChange={(event) => update("recipientEmail", event.target.value)} /></EmailField>
        <ToggleRow label="Send After Daily Refresh" checked={settings.sendAfterRefresh} onChange={(value) => update("sendAfterRefresh", value)} />
        <ToggleRow label="Send email when no new jobs are found" checked={settings.sendWhenEmpty} onChange={(value) => update("sendWhenEmpty", value)} />
      </div>
      {settings.dailyEnabled && !status?.scheduledRefreshEnabled ? <p className="mt-4 rounded-md border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-200">Daily job emails require an enabled scheduled job refresh in the Refresh tab.</p> : null}
      <div className="mt-5 flex justify-end"><button className="btn btn-primary" type="button" disabled={Boolean(busy)} onClick={() => void save()}><Save size={16} />Save Daily Email</button></div>
    </section>

    <section className="grid gap-4 lg:grid-cols-2">
      <div className="panel p-5"><h2 className="font-semibold text-white">Test Email</h2><EmailField label="Test Recipient"><input className="field mt-3" type="email" value={testRecipient} onChange={(event) => setTestRecipient(event.target.value)} /></EmailField><button className="btn btn-primary mt-4" type="button" disabled={Boolean(busy) || !testRecipient} onClick={() => void runEmailAction("test")}><Send size={16} />{busy === "test" ? "Sending..." : "Send Test Email"}</button></div>
      <div className="panel p-5"><h2 className="font-semibold text-white">Email Service</h2><dl className="mt-4 grid grid-cols-2 gap-4"><Stat label="Configured" value={status?.configured ? "Yes" : "No"} /><Stat label="Daily Digest" value={status?.dailyEnabled ? "Enabled" : "Disabled"} /><Stat label="Recipient" value={status?.recipientEmail || "Not Set"} /><Stat label="Last Email" value={status?.lastEmail ? formatTimestamp(status.lastEmail.completedAt || status.lastEmail.startedAt) : "Never"} /><Stat label="Last Result" value={status?.lastEmail?.status || "Never Sent"} /><Stat label="New Jobs Sent" value={String(status?.lastEmail?.jobCount ?? 0)} /><Stat label="Next Digest" value={status?.scheduledRefreshEnabled ? `After the scheduled ${formatClockTime(status.scheduledRefreshTime)} refresh` : "Scheduled refresh is off"} /></dl><button className="btn mt-5" type="button" disabled={Boolean(busy) || !settings.configured} onClick={() => void runEmailAction("digest")}><Send size={16} />{busy === "digest" ? "Sending..." : "Send New Jobs Now"}</button></div>
    </section>

    <section className="panel overflow-hidden"><header className="border-b border-radar-line p-5"><h2 className="font-semibold text-white">Email History</h2></header>{history.length ? <div className="overflow-x-auto"><div className="min-w-[38rem]"><div className="grid grid-cols-[1fr_6rem_8rem_6rem] gap-3 border-b border-radar-line px-5 py-3 text-xs font-semibold uppercase text-slate-500"><span>Date</span><span>New Jobs</span><span>Result</span><span>Trigger</span></div>{history.map((digest) => <button className="grid w-full grid-cols-[1fr_6rem_8rem_6rem] gap-3 border-b border-radar-line px-5 py-3 text-left text-sm text-slate-300 last:border-0 hover:bg-radar-bg/50" type="button" key={digest.id} onClick={() => setSelectedDigest(selectedDigest === digest.id ? null : digest.id)}><span>{formatTimestamp(digest.startedAt)}</span><span>{digest.jobCount}</span><span className={digest.status === "Success" ? "text-emerald-300" : digest.status === "Failed" ? "text-red-300" : "text-slate-400"}>{digest.status}</span><span className="capitalize">{digest.triggerType}</span>{selectedDigest === digest.id && digest.error ? <span className="col-span-4 text-red-300">This email attempt failed. Check the email configuration and try again.</span> : null}</button>)}</div></div> : <p className="p-5 text-sm text-slate-400">No daily job emails have been attempted yet.</p>}</section>
  </div>;
}

function EmailField({ label, children }: { label: string; children: ReactNode }) { return <label className="block text-sm text-slate-300">{label}{children}</label>; }
function ToggleRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <div className="flex items-center justify-between gap-4 border-b border-radar-line pb-4"><span className="text-sm font-medium text-slate-200">{label}</span><button className={`relative h-6 w-11 rounded-full transition ${checked ? "bg-radar-accent" : "bg-slate-700"}`} type="button" role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)}><span className={`absolute top-1 size-4 rounded-full bg-white transition-all ${checked ? "left-6" : "left-1"}`} /></button></div>; }

function MaintenanceCard({ action, job, enabled, schedulesEnabled, onRun, onToggle, onEditSchedule, onHistory }: { action: UtilityPresentation; job: MaintenanceJobState; enabled: boolean; schedulesEnabled: boolean; onRun: () => void; onToggle: () => void; onEditSchedule: () => void; onHistory: () => void; }) {
  const Icon = action.icon;
  const scheduleText = job.schedule?.enabled ? `Daily at ${formatClockTime(job.schedule.runTime)} (${job.schedule.timezone})` : "Automatic schedule is off";
  return <article className="card flex flex-col p-5">
    <div className="flex items-start justify-between gap-4"><div className="flex min-w-0 items-start gap-3"><div className="grid size-10 shrink-0 place-items-center rounded-md border border-radar-line bg-radar-bg text-radar-highlight"><Icon size={20} /></div><div><h3 className="font-semibold text-white">{job.taskName}</h3><p className="mt-1 text-sm leading-6 text-slate-400">{job.description}</p></div></div><ResultBadge result={job.lastResult} /></div>
    <dl className="mt-5 grid grid-cols-2 gap-x-5 gap-y-4 border-y border-radar-line py-4 sm:grid-cols-3"><Stat label="Last Run" value={job.lastRun ? formatTimestamp(job.lastRun.startedAt) : "Never"} /><Stat label="Last Runtime" value={formatDuration(job.lastRuntimeSeconds)} /><Stat label="Average Runtime" value={formatDuration(job.averageRuntimeSeconds)} /></dl>
    {!enabled ? <p className="mt-4 text-sm text-amber-200">Disabled for the initial production release.</p> : null}
    {job.supportsScheduling && job.schedule ? <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"><div><p className="text-sm font-medium text-slate-200">{schedulesEnabled ? scheduleText : "Scheduling unavailable"}</p><p className="mt-1 text-xs text-slate-500">{schedulesEnabled ? "Runs once each day while scheduled." : "Schedules are disabled for the initial production release."}</p></div><div className="flex items-center gap-2"><button className={`relative h-6 w-11 rounded-full transition disabled:cursor-not-allowed disabled:opacity-50 ${job.schedule.enabled ? "bg-radar-accent" : "bg-slate-700"}`} type="button" role="switch" aria-checked={job.schedule.enabled} aria-label={`Scheduled ${job.schedule.enabled ? "ON" : "OFF"} for ${job.taskName}`} disabled={!schedulesEnabled} onClick={onToggle}><span className={`absolute top-1 size-4 rounded-full bg-white transition-all ${job.schedule.enabled ? "left-6" : "left-1"}`} /></button><span className="w-24 text-sm text-slate-300">Scheduled {job.schedule.enabled ? "ON" : "OFF"}</span><button className="icon-btn disabled:cursor-not-allowed disabled:opacity-50" type="button" title={schedulesEnabled ? `Edit schedule for ${job.taskName}` : "Schedules are disabled for the initial production release."} aria-label={`Edit schedule for ${job.taskName}`} disabled={!schedulesEnabled} onClick={onEditSchedule}><CalendarClock size={18} /></button></div></div> : null}
    <div className="mt-5 flex flex-wrap gap-3"><button className="btn btn-primary" type="button" title={!enabled ? "This utility is disabled for the initial production release." : undefined} disabled={!enabled || job.running} onClick={onRun}><RefreshCw className={job.running ? "animate-spin" : ""} size={17} />{job.running ? "Running..." : enabled ? job.jobKey === "import-data" ? "Import Data" : "Run Now" : "Unavailable"}</button><button className="btn" type="button" onClick={onHistory}><History size={17} />History</button></div>
  </article>;
}

function ProgressPanel({ run, onCancel }: { run: MaintenanceRun; onCancel: (runId: string) => Promise<void> }) {
  const unit = run.progressText.split(" ").slice(-1)[0] || "items";
  return <section className="panel p-5" aria-live="polite"><div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between"><div><div className="flex flex-wrap items-center gap-2"><p className="text-xs font-semibold uppercase text-radar-highlight">Maintenance running</p><ResultBadge result="Running" /></div><h2 className="mt-2 text-lg font-semibold text-white">{run.taskName}</h2><p className="mt-2 text-sm text-slate-300">Started: {formatTimestamp(run.startedAt)} · Elapsed: {formatDuration(run.runtimeSeconds)}</p><p className="mt-1 text-sm text-slate-300">Progress: {run.total ? `${run.current} / ${run.total} ${unit}` : "Running..."}</p><p className="mt-1 text-sm text-slate-400">Current item: {run.currentCompany || run.currentMessage || "Preparing"}</p><p className="mt-2 break-all font-mono text-xs text-slate-500">Run {run.id}</p></div><button className="btn" type="button" disabled={run.status === "Cancelling"} onClick={() => void onCancel(run.id)}>{run.status === "Cancelling" ? "Cancelling..." : "Cancel"}</button></div>{run.total ? <div className="mt-4 h-2 overflow-hidden rounded-full bg-radar-bg" aria-label={`${run.progress ?? 0}% complete`}><div className="h-full bg-radar-accent transition-all" style={{ width: `${run.progress ?? 0}%` }} /></div> : null}</section>;
}

function ScheduleModal({ job, error, onCancel, onSave }: { job: MaintenanceJobState; error: string; onCancel: () => void; onSave: (enabled: boolean, runTime: string, timezone: string) => Promise<void>; }) {
  const [enabled, setEnabled] = useState(job.schedule!.enabled);
  const [runTime, setRunTime] = useState(job.schedule!.runTime);
  const [timezone, setTimezone] = useState(job.schedule!.timezone);
  const [saving, setSaving] = useState(false);
  return <Modal title={`Schedule ${job.taskName}`} onClose={onCancel}><p className="text-sm text-slate-400">Set a daily time for this maintenance job. The application timezone is shared by all schedules.</p><label className="mt-5 flex items-center justify-between gap-4 border-y border-radar-line py-4"><span><span className="block font-medium text-white">Scheduled</span><span className="mt-1 block text-sm text-slate-400">Run this job automatically once per day.</span></span><input className="size-5 accent-radar-accent" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /></label><div className="mt-4 grid gap-4 sm:grid-cols-2"><label className="text-sm text-slate-300">Run Time<input className="field mt-1" type="time" value={runTime} onChange={(event) => setRunTime(event.target.value)} /></label><label className="text-sm text-slate-300">Application Timezone<select className="field mt-1" value={timezone} onChange={(event) => setTimezone(event.target.value)}>{timezoneOptions.map((value) => <option key={value}>{value}</option>)}</select></label></div>{error ? <Alert message={error} /> : null}<div className="mt-5 flex justify-end gap-3"><button className="btn" type="button" disabled={saving} onClick={onCancel}>Cancel</button><button className="btn btn-primary" type="button" disabled={saving || !runTime} onClick={() => { setSaving(true); void onSave(enabled, runTime, timezone).finally(() => setSaving(false)); }}>{saving ? "Saving..." : "Save Schedule"}</button></div></Modal>;
}

function HistoryModal({ job, runs, loading, error, onClose }: { job: MaintenanceJobState; runs: MaintenanceRun[]; loading: boolean; error: string; onClose: () => void; }) {
  return <Modal title={`${job.taskName} History`} onClose={onClose}>{loading ? <p className="text-sm text-slate-400">Loading history...</p> : error ? <Alert message={error} /> : runs.length ? <div className="max-h-[60vh] overflow-auto"><div className="grid min-w-[34rem] grid-cols-[minmax(9rem,1.5fr)_6rem_5rem_5rem] gap-3 border-b border-radar-line pb-2 text-xs font-semibold uppercase text-slate-500"><span>Started</span><span>Trigger</span><span>Runtime</span><span>Result</span></div>{runs.map((run) => <div className="min-w-[34rem] border-b border-radar-line py-3 last:border-0" key={run.id}><div className="grid grid-cols-[minmax(9rem,1.5fr)_6rem_5rem_5rem] gap-3 text-sm text-slate-300"><span>{formatTimestamp(run.startedAt || run.createdAt)}</span><span className="capitalize">{run.triggerType}</span><span>{formatDuration(run.runtimeSeconds)}</span><span className={run.status === "Completed" ? "text-emerald-300" : run.status === "Failed" ? "text-red-300" : "text-slate-400"}>{run.status === "Completed" ? "Success" : run.status}</span></div>{run.error ? <p className="mt-2 text-sm text-red-300">This maintenance run failed. Try the action again or contact an administrator.</p> : null}</div>)}</div> : <p className="text-sm text-slate-400">This utility has not run yet.</p>}</Modal>;
}

function ConfirmationModal({ action, job, file, error, starting, onFileChange, onCancel, onConfirm }: { action: UtilityPresentation; job?: MaintenanceJobState; file: File | null; error: string; starting: boolean; onFileChange: (event: ChangeEvent<HTMLInputElement>) => void; onCancel: () => void; onConfirm: () => Promise<void>; }) {
  return <Modal title={job?.taskName || "Run Maintenance"} onClose={onCancel}><p className="text-sm leading-6 text-slate-300">{action.confirmation}</p>{action.key === "import-data" ? <label className="mt-4 block text-sm text-slate-300">Import file<input className="field mt-1" type="file" accept=".json,.xlsx,application/json,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={onFileChange} /></label> : null}{file ? <p className="mt-2 text-sm text-slate-400">Selected: {file.name}</p> : null}{error ? <Alert message={error} /> : null}<div className="mt-5 flex justify-end gap-3"><button className="btn" type="button" disabled={starting} onClick={onCancel}>Cancel</button><button className="btn btn-primary" type="button" disabled={starting || (action.key === "import-data" && !file)} onClick={() => void onConfirm()}>{starting ? "Starting..." : action.key === "import-data" ? "Import Data" : "Run Now"}</button></div></Modal>;
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) { return <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="panel w-full max-w-2xl p-5" role="dialog" aria-modal="true" aria-label={title}><header className="mb-4 flex items-center justify-between gap-4"><h2 className="text-xl font-semibold text-white">{title}</h2><button className="icon-btn" type="button" onClick={onClose} title="Close" aria-label="Close"><X size={18} /></button></header>{children}</section></div>; }
function Stat({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs uppercase text-slate-500">{label}</dt><dd className="mt-1 text-sm text-slate-200">{value}</dd></div>; }
function Alert({ message }: { message: string }) { return <div className="mt-4 rounded-md border border-red-900 bg-red-950/40 px-4 py-3 text-sm text-red-300" role="alert">{message}</div>; }
function ResultBadge({ result }: { result: MaintenanceJobState["lastResult"] }) { const colors = result === "Success" ? "border-emerald-800 text-emerald-300" : result === "Failed" ? "border-red-900 text-red-300" : result === "Running" ? "border-blue-800 text-blue-300" : "border-slate-700 text-slate-400"; return <span className={`badge shrink-0 ${colors}`}>{result}</span>; }
function formatTimestamp(value: string): string { if (!value) return "Never"; return new Date(value).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }); }
function formatClockTime(value: string): string { const [hour, minute] = value.split(":").map(Number); return new Date(2000, 0, 1, hour, minute).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function formatDuration(seconds: number | null): string { if (seconds === null) return "Not available"; if (seconds > 0 && seconds < 1) return "<1s"; const total = Math.max(0, Math.floor(seconds)); const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60); const remaining = total % 60; return [hours ? `${hours}h` : "", minutes || hours ? `${minutes}m` : "", `${remaining}s`].filter(Boolean).join(" "); }

function actionEnabled(action: UtilityKey, features: FeatureFlags): boolean {
  if (!features.utilities) return false;
  if (action === "refresh-missing-company-information" || action === "refresh-company-discovery") {
    return features.companyRefresh && features.discovery && features.browserJobs;
  }
  if (action === "refresh-all-job-listings") return features.browserJobs;
  return true;
}
