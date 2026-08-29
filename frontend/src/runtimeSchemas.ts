import type { Company } from "./types/Company";
import type { FeatureFlags } from "./types/FeatureFlags";
import type { ApplicationStatus, Job } from "./types/Job";
import type {
  CompanyInfoRefreshResult,
  CompanyInfoRefreshSummary,
  MaintenanceJobState,
  MaintenanceJobsState,
  MaintenanceRun,
  MaintenanceSchedule,
  MaintenanceStatus,
} from "./types/Maintenance";

export interface SessionPayload {
  authenticated: true;
  id: string;
  username: string;
  email: string;
  displayName: string;
  role: string;
  permissions: string[];
  developmentBypass: boolean;
  canAdminister: boolean;
  features: FeatureFlags;
}

export interface ApplicationPatch {
  applied: boolean;
  applicationStatus: ApplicationStatus;
  dateApplied: string;
  followUpDate: string;
  notes: string;
  notInterested: boolean;
}

/** A sparse lookup: an arbitrary job ID may not have an application row. */
export type ApplicationOverrides = Record<string, ApplicationPatch | undefined>;

type ApplicationField = keyof ApplicationPatch;

/** A job as returned by `/jobs` before application tracking is merged into it. */
export type JobPayload = Omit<Job, ApplicationField>;

export interface ApplicationPatchResponse {
  message: string;
  application: ApplicationPatch;
}

export interface BrowserOverrideImportResponse {
  importedJobIds: string[];
  skippedJobIds: string[];
}

export interface JobMatchMutationResponse {
  message: string;
  jobId: string;
  score: number | null;
  status: "Matched" | "Not Matched" | "Needs Rematch" | "Match Failed";
  label: string;
  matchedAt: string;
  algorithmVersion: string;
  details: Record<string, unknown>;
  error: string;
  needsRematch: boolean;
  job: JobPayload;
}

export interface CompanyPageOptions {
  states: string[];
  industries: string[];
  jobBoardTypes: string[];
  discoveryStatuses: string[];
}

export interface CompanyPage {
  items: Company[];
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  options: CompanyPageOptions;
}

export interface CompanyMutationResponse {
  message: string;
  company: Company;
}

export interface CompanyUpdateResponse {
  message: string;
  company: Company & { jobBoardReverificationRequired: boolean };
}

export interface CompanyRefreshResponse {
  status: "completed" | "partial";
  companyId: string;
  companyName: string;
  companyMetadataChanged: boolean;
  totalJobsDiscovered: number;
  newJobs: number;
  updatedJobs: number;
  removedOrClosedJobs: number;
  activeJobs: number;
  warnings: string[];
  errors: string[];
}

export interface CompanyDeleteResponse {
  message: string;
  deletedCompanyId: string;
  deletedJobIds: string[];
  deletedJobs: number;
  deletedRawCandidates: number;
  deletedApplications: number;
}

export interface MaintenanceHistoryResponse {
  jobKey: string;
  runs: MaintenanceRun[];
}

export type UtilityRunResponse = MaintenanceRun;
export type UtilityCancelResponse = MaintenanceRun;
export type UtilityMutationResponse = MaintenanceRun;
export type ScheduleMutationResponse = MaintenanceSchedule;

export interface SynchronousUtilityResponse {
  status: "completed" | "failed";
  message: string;
  startedAt: string;
  completedAt: string;
  durationSeconds: number;
  summary: Record<string, unknown>;
  stdout: string;
  stderr: string;
  error?: string;
}

export interface LogoutResponse {
  message: string;
  redirectUrl: string;
}

export interface MessageResponse {
  message: string;
}

export interface EmailSettingsPayload {
  smtpHost: string;
  smtpPort: number;
  security: "ssl_tls" | "starttls" | "none";
  smtpUsername: string;
  fromEmail: string;
  fromName: string;
  replyToEmail: string;
  dailyEnabled: boolean;
  recipientEmail: string;
  sendAfterRefresh: boolean;
  sendWhenEmpty: boolean;
  hasSmtpPassword: boolean;
  trackingStartedAt: string;
  configured: boolean;
}

export interface EmailDigestPayload {
  id: string;
  startedAt: string;
  completedAt: string;
  recipient: string;
  jobCount: number;
  status: "Sending" | "Success" | "Failed" | "Skipped - No New Jobs";
  error: string;
  triggerType: "manual" | "scheduled";
}

export interface EmailStatusPayload {
  configured: boolean;
  dailyEnabled: boolean;
  recipientEmail: string;
  lastEmail: EmailDigestPayload | null;
  scheduledRefreshEnabled: boolean;
  scheduledRefreshTime: string;
  scheduledRefreshTimezone: string;
}

export interface EmailHistoryPayload {
  history: EmailDigestPayload[];
}

export interface EmailDigestMutationResponse {
  id: string;
  status: "Success" | "Skipped - No New Jobs";
  jobCount: number;
}

const MAINTENANCE_STATUSES: readonly MaintenanceStatus[] = [
  "Queued",
  "Running",
  "Cancelling",
  "Completed",
  "Cancelled",
  "Failed",
  "Skipped",
];

const MATCH_STATUSES = ["Matched", "Not Matched", "Needs Rematch", "Match Failed"] as const;
const LAST_RESULTS = ["Success", "Failed", "Running", "Never Run"] as const;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && isFiniteNumber(value) && value >= 0;
}

function isNullableNonNegativeNumber(value: unknown): value is number | null {
  return value === null || (isFiniteNumber(value) && value >= 0);
}

function hasStringFields<K extends string>(
  record: Record<string, unknown>,
  fields: readonly K[],
): record is Record<string, unknown> & Record<K, string> {
  return fields.every((field) => typeof record[field] === "string");
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || typeof value === "string";
}

function isOptionalBoolean(value: unknown): boolean {
  return value === undefined || typeof value === "boolean";
}

function isOptionalFiniteNumber(value: unknown): boolean {
  return value === undefined || isFiniteNumber(value);
}

function isOneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === "string" && allowed.includes(value as T);
}

export function isFeatureFlags(value: unknown): value is FeatureFlags {
  if (!isRecord(value)) return false;
  return typeof value.browserJobs === "boolean"
    && typeof value.companyRefresh === "boolean"
    && typeof value.utilities === "boolean"
    && typeof value.schedules === "boolean"
    && typeof value.discovery === "boolean";
}

/**
 * Normalizes feature flags field-by-field. Missing, legacy, or malformed values
 * disable that feature instead of granting access or invalidating authentication.
 */
export function normalizeRuntimeFeatureFlags(value: unknown): FeatureFlags {
  const flags = isRecord(value) ? value : {};
  return {
    browserJobs: flags.browserJobs === true,
    companyRefresh: flags.companyRefresh === true,
    utilities: flags.utilities === true,
    schedules: flags.schedules === true,
    discovery: flags.discovery === true,
  };
}

function isSessionIdentity(value: unknown): value is Omit<SessionPayload, "features"> & { features?: unknown } {
  if (!isRecord(value)) return false;
  return value.authenticated === true
    && hasStringFields(value, ["id", "username", "email", "displayName", "role"])
    && (value.id as string).trim().length > 0
    && (value.username as string).trim().length > 0
    && (value.email as string).trim().length > 0
    && (value.role as string).trim().length > 0
    && isStringArray(value.permissions)
    && typeof value.developmentBypass === "boolean"
    && typeof value.canAdminister === "boolean";
}

export function isSessionPayload(value: unknown): value is SessionPayload {
  return isSessionIdentity(value) && isFeatureFlags(value.features);
}

export function normalizeSessionPayload(value: unknown): SessionPayload | null {
  if (!isSessionIdentity(value)) return null;
  return {
    authenticated: true,
    id: value.id,
    username: value.username,
    email: value.email,
    displayName: value.displayName,
    role: value.role,
    permissions: [...value.permissions],
    developmentBypass: value.developmentBypass,
    canAdminister: value.canAdminister,
    features: normalizeRuntimeFeatureFlags(value.features),
  };
}

export const parseSessionPayload = normalizeSessionPayload;

export function isCompany(value: unknown): value is Company {
  if (!isRecord(value)) return false;
  if (!hasStringFields(value, [
    "id", "name", "companyDescription", "industry", "city", "state", "country", "knownWebsite",
    "officialWebsite", "websiteDiscoveryMethod", "websiteCandidateUrls",
    "websiteVerificationNotes", "careersPageUrl", "jobBoardUrl",
    "jobBoardDiscoveryMethod", "jobsRssFeedUrl", "jobPlatform", "searchStatus",
    "lastChecked", "notes", "assetsAsOfDate", "companyInfoLastChecked",
  ])) return false;
  if (typeof value.websiteVerified !== "boolean"
    || typeof value.feedFound !== "boolean"
    || typeof value.searchStatus !== "string"
    || !isFiniteNumber(value.confidence)
    || !isNullableFiniteNumber(value.foundedYear)
    || !isNullableFiniteNumber(value.totalAssets)) return false;
  return isOptionalFiniteNumber(value.activeJobCount)
    && isOptionalFiniteNumber(value.appliedCount)
    && isOptionalFiniteNumber(value.jobCount)
    && isOptionalString(value.lastCollectionDate)
    && isOptionalBoolean(value.jobBoardReverificationRequired);
}

export function isCompanyArray(value: unknown): value is Company[] {
  return Array.isArray(value) && value.every(isCompany);
}

export const isCompanyList = isCompanyArray;

function isMatchDetails(value: unknown): value is NonNullable<Job["matchDetails"]> {
  if (!isRecord(value)) return false;
  return (value.matchedKeywords === undefined || isStringArray(value.matchedKeywords))
    && (value.missingKeywords === undefined || isStringArray(value.missingKeywords))
    && isOptionalString(value.experienceAlignment)
    && isOptionalString(value.titleAlignment)
    && isOptionalString(value.summary);
}

export function isJob(value: unknown): value is JobPayload {
  if (!isRecord(value)) return false;
  if (!hasStringFields(value, [
    "id", "companyId", "companyName", "title", "location", "payText", "payPeriod",
    "payCurrency", "postedDate", "sourceUrl", "jobPlatform", "description",
    "descriptionSnippet", "collectedAt", "status", "roleTypeReason", "matchLabel",
    "matchedAt", "matchAlgorithmVersion", "matchError",
  ])) return false;
  return typeof value.workType === "string"
    && typeof value.roleType === "string"
    && isNullableFiniteNumber(value.payMin)
    && isNullableFiniteNumber(value.payMax)
    && isRecord(value.rawData)
    && isNullableFiniteNumber(value.matchScore)
    && isOneOf(value.matchStatus, MATCH_STATUSES)
    && isMatchDetails(value.matchDetails)
    && typeof value.needsRematch === "boolean";
}

export const isJobPayload = isJob;

export function isJobArray(value: unknown): value is JobPayload[] {
  return Array.isArray(value) && value.every(isJob);
}

export const isJobPayloadArray = isJobArray;
export const isJobList = isJobArray;

export function isApplicationPatch(value: unknown): value is ApplicationPatch {
  if (!isRecord(value)) return false;
  return typeof value.applied === "boolean"
    && typeof value.applicationStatus === "string"
    && typeof value.dateApplied === "string"
    && typeof value.followUpDate === "string"
    && typeof value.notes === "string"
    && typeof value.notInterested === "boolean";
}

export function isApplicationOverrides(value: unknown): value is ApplicationOverrides {
  return isRecord(value) && Object.values(value).every(isApplicationPatch);
}

export function isApplicationPatchResponse(value: unknown): value is ApplicationPatchResponse {
  return isRecord(value)
    && typeof value.message === "string"
    && isApplicationPatch(value.application);
}

export const isApplicationMutationResponse = isApplicationPatchResponse;

export function isBrowserOverrideImportResponse(value: unknown): value is BrowserOverrideImportResponse {
  return isRecord(value)
    && isStringArray(value.importedJobIds)
    && isStringArray(value.skippedJobIds);
}

export function isJobMatchMutationResponse(value: unknown): value is JobMatchMutationResponse {
  if (!isRecord(value) || !isJob(value.job)) return false;
  return typeof value.message === "string"
    && typeof value.jobId === "string"
    && value.jobId === value.job.id
    && isNullableFiniteNumber(value.score)
    && isOneOf(value.status, MATCH_STATUSES)
    && typeof value.label === "string"
    && typeof value.matchedAt === "string"
    && typeof value.algorithmVersion === "string"
    && isRecord(value.details)
    && typeof value.error === "string"
    && typeof value.needsRematch === "boolean";
}

export const isJobMutationResponse = isJobMatchMutationResponse;

function hasCompanyPageMetrics(company: Company): boolean {
  const value = company as Company & Record<string, unknown>;
  return isNonNegativeInteger(value.activeJobCount)
    && isNonNegativeInteger(value.jobCount)
    && isNonNegativeInteger(value.appliedCount)
    && typeof value.lastCollectionDate === "string";
}

export function isCompanyPageOptions(value: unknown): value is CompanyPageOptions {
  if (!isRecord(value)) return false;
  return isStringArray(value.states)
    && isStringArray(value.industries)
    && isStringArray(value.jobBoardTypes)
    && isStringArray(value.discoveryStatuses);
}

export function isCompanyPage(value: unknown): value is CompanyPage {
  if (!isRecord(value) || !Array.isArray(value.items)) return false;
  return value.items.every((item) => isCompany(item) && hasCompanyPageMetrics(item))
    && isNonNegativeInteger(value.total)
    && isNonNegativeInteger(value.page)
    && value.page >= 1
    && isNonNegativeInteger(value.pageSize)
    && value.pageSize >= 1
    && isNonNegativeInteger(value.totalPages)
    && value.totalPages >= 1
    && isCompanyPageOptions(value.options);
}

export function isCompanyMutationResponse(value: unknown): value is CompanyMutationResponse {
  return isRecord(value)
    && typeof value.message === "string"
    && isCompany(value.company);
}

export const isCompanyCreateResponse = isCompanyMutationResponse;
export const isCompanyCrudResponse = isCompanyMutationResponse;

export function isCompanyUpdateResponse(value: unknown): value is CompanyUpdateResponse {
  return isCompanyMutationResponse(value)
    && typeof value.company.jobBoardReverificationRequired === "boolean";
}

export function isCompanyRefreshResponse(value: unknown): value is CompanyRefreshResponse {
  if (!isRecord(value)) return false;
  return isOneOf(value.status, ["completed", "partial"] as const)
    && hasStringFields(value, ["companyId", "companyName"])
    && typeof value.companyMetadataChanged === "boolean"
    && isNonNegativeInteger(value.totalJobsDiscovered)
    && isNonNegativeInteger(value.newJobs)
    && isNonNegativeInteger(value.updatedJobs)
    && isNonNegativeInteger(value.removedOrClosedJobs)
    && isNonNegativeInteger(value.activeJobs)
    && isStringArray(value.warnings)
    && isStringArray(value.errors);
}

export function isCompanyInfoRefreshResult(value: unknown): value is CompanyInfoRefreshResult {
  return isRecord(value)
    && hasStringFields(value, ["companyId", "companyName", "message"])
    && isOneOf(value.outcome, ["updated", "unchanged", "no_information_found", "failed"] as const)
    && isStringArray(value.foundFields)
    && isStringArray(value.updatedFields);
}

export function isCompanyInfoRefreshSummary(value: unknown): value is CompanyInfoRefreshSummary {
  if (!isRecord(value)) return false;
  return [
    "totalCompaniesNeedingReview", "processedCount", "updatedCount",
    "noInformationFoundCount", "failedCount", "unchangedCount",
    "duplicateRecordsSkipped",
  ].every((field) => isNonNegativeInteger(value[field]))
    && Array.isArray(value.companyResults)
    && value.companyResults.every(isCompanyInfoRefreshResult);
}

export function isCompanyDeleteResponse(value: unknown): value is CompanyDeleteResponse {
  if (!isRecord(value)) return false;
  return typeof value.message === "string"
    && typeof value.deletedCompanyId === "string"
    && isStringArray(value.deletedJobIds)
    && isNonNegativeInteger(value.deletedJobs)
    && isNonNegativeInteger(value.deletedRawCandidates)
    && isNonNegativeInteger(value.deletedApplications);
}

export function isMaintenanceSchedule(value: unknown): value is MaintenanceSchedule {
  if (!isRecord(value)) return false;
  return typeof value.jobKey === "string"
    && typeof value.enabled === "boolean"
    && value.frequency === "daily"
    && typeof value.runTime === "string"
    && typeof value.timezone === "string"
    && typeof value.lastScheduledDate === "string"
    && typeof value.updatedAt === "string";
}

export function isMaintenanceRun(value: unknown): value is MaintenanceRun {
  if (!isRecord(value)) return false;
  return hasStringFields(value, [
    "id", "run_id", "action", "job_key", "taskName", "task_name", "progressVerb",
    "progressUnit", "progressText",
    "currentCompany", "currentMessage", "message", "error", "startedAt",
    "completedAt", "createdAt", "updatedAt",
  ])
    && isOneOf(value.triggerType, ["manual", "scheduled"] as const)
    && isOneOf(value.trigger_type, ["manual", "scheduled"] as const)
    && (value.id as string).length > 0
    && value.id === value.run_id
    && (value.action as string).length > 0
    && value.action === value.job_key
    && value.triggerType === value.trigger_type
    && value.taskName === value.task_name
    && isOneOf(value.status, MAINTENANCE_STATUSES)
    && typeof value.running === "boolean"
    && isNonNegativeInteger(value.current)
    && isNonNegativeInteger(value.total)
    && isNullableNonNegativeNumber(value.progress)
    && isRecord(value.summary)
    && isRecord(value.resultSummary)
    && isNullableNonNegativeNumber(value.runtimeSeconds);
}

export function isMaintenanceJobState(value: unknown): value is MaintenanceJobState {
  if (!isRecord(value)) return false;
  return hasStringFields(value, ["jobKey", "job_key", "taskName", "description"])
    && (value.jobKey as string).length > 0
    && value.jobKey === value.job_key
    && typeof value.supportsScheduling === "boolean"
    && (value.schedule === null || isMaintenanceSchedule(value.schedule))
    && typeof value.running === "boolean"
    && (value.activeRunId === null || typeof value.activeRunId === "string")
    && (value.active_run_id === null || typeof value.active_run_id === "string")
    && value.activeRunId === value.active_run_id
    && (value.activeRun === null || isMaintenanceRun(value.activeRun))
    && (value.activeRun === null || value.activeRun.id === value.activeRunId)
    && (value.latestRun === null || isMaintenanceRun(value.latestRun))
    && (value.lastRun === null || isMaintenanceRun(value.lastRun))
    && isNullableNonNegativeNumber(value.lastRuntimeSeconds)
    && isNullableNonNegativeNumber(value.averageRuntimeSeconds)
    && isOneOf(value.lastResult, LAST_RESULTS);
}

export function isMaintenanceJobsState(value: unknown): value is MaintenanceJobsState {
  if (!isRecord(value)) return false;
  return Array.isArray(value.jobs)
    && value.jobs.every(isMaintenanceJobState)
    && Array.isArray(value.activeRuns)
    && value.activeRuns.every(isMaintenanceRun)
    && isNonNegativeInteger(value.runningCount)
    && isNonNegativeInteger(value.running_count)
    && value.runningCount === value.running_count
    && value.runningCount === value.activeRuns.length;
}

export const isMaintenanceJob = isMaintenanceJobState;
export const isMaintenanceJobs = isMaintenanceJobsState;

export function isMaintenanceHistoryResponse(value: unknown): value is MaintenanceHistoryResponse {
  return isRecord(value)
    && typeof value.jobKey === "string"
    && Array.isArray(value.runs)
    && value.runs.every(isMaintenanceRun);
}

export const isUtilityRunResponse = isMaintenanceRun;
export const isUtilityCancelResponse = isMaintenanceRun;
export const isUtilityMutationResponse = isMaintenanceRun;
export const isScheduleMutationResponse = isMaintenanceSchedule;
export const isMaintenanceScheduleResponse = isMaintenanceSchedule;

export function isSynchronousUtilityResponse(value: unknown): value is SynchronousUtilityResponse {
  if (!isRecord(value)) return false;
  return isOneOf(value.status, ["completed", "failed"] as const)
    && typeof value.message === "string"
    && typeof value.startedAt === "string"
    && typeof value.completedAt === "string"
    && isNullableNonNegativeNumber(value.durationSeconds)
    && value.durationSeconds !== null
    && isRecord(value.summary)
    && typeof value.stdout === "string"
    && typeof value.stderr === "string"
    && (value.status !== "failed" || typeof value.error === "string")
    && isOptionalString(value.error);
}

export function isLogoutResponse(value: unknown): value is LogoutResponse {
  return isRecord(value)
    && typeof value.message === "string"
    && typeof value.redirectUrl === "string"
    && value.redirectUrl.trim().length > 0;
}

export function isMessageResponse(value: unknown): value is MessageResponse {
  return isRecord(value) && typeof value.message === "string";
}

export function isEmailSettingsPayload(value: unknown): value is EmailSettingsPayload {
  if (!isRecord(value)) return false;
  return hasStringFields(value, [
    "smtpHost", "smtpUsername", "fromEmail", "fromName", "replyToEmail",
    "recipientEmail", "trackingStartedAt",
  ])
    && isNonNegativeInteger(value.smtpPort)
    && value.smtpPort >= 1
    && value.smtpPort <= 65535
    && isOneOf(value.security, ["ssl_tls", "starttls", "none"] as const)
    && typeof value.dailyEnabled === "boolean"
    && typeof value.sendAfterRefresh === "boolean"
    && typeof value.sendWhenEmpty === "boolean"
    && typeof value.hasSmtpPassword === "boolean"
    && typeof value.configured === "boolean";
}

export function isEmailDigestPayload(value: unknown): value is EmailDigestPayload {
  if (!isRecord(value)) return false;
  return hasStringFields(value, ["id", "startedAt", "completedAt", "recipient", "error"])
    && isNonNegativeInteger(value.jobCount)
    && isOneOf(value.status, ["Sending", "Success", "Failed", "Skipped - No New Jobs"] as const)
    && isOneOf(value.triggerType, ["manual", "scheduled"] as const);
}

export function isEmailStatusPayload(value: unknown): value is EmailStatusPayload {
  if (!isRecord(value)) return false;
  return typeof value.configured === "boolean"
    && typeof value.dailyEnabled === "boolean"
    && typeof value.recipientEmail === "string"
    && (value.lastEmail === null || isEmailDigestPayload(value.lastEmail))
    && typeof value.scheduledRefreshEnabled === "boolean"
    && typeof value.scheduledRefreshTime === "string"
    && typeof value.scheduledRefreshTimezone === "string";
}

export function isEmailHistoryPayload(value: unknown): value is EmailHistoryPayload {
  return isRecord(value)
    && Array.isArray(value.history)
    && value.history.every(isEmailDigestPayload);
}

export function isEmailDigestMutationResponse(value: unknown): value is EmailDigestMutationResponse {
  if (!isRecord(value)) return false;
  return typeof value.id === "string"
    && isOneOf(value.status, ["Success", "Skipped - No New Jobs"] as const)
    && isNonNegativeInteger(value.jobCount);
}
