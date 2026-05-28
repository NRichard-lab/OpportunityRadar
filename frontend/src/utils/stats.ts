import type { Company } from "../types/Company";
import type { Job, WorkType } from "../types/Job";

export function formatMoney(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "Not listed";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

export function notListed(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "Not listed";
  return String(value);
}

export function buildStats(companies: Company[], jobs: Job[]) {
  const jobsWithPay = jobs.filter((job) => job.payMin !== null || job.payMax !== null);
  const minPayJobs = jobs.filter((job) => job.payMin !== null);
  const maxPayJobs = jobs.filter((job) => job.payMax !== null);
  const average = (values: number[]) =>
    values.length ? Math.round(values.reduce((sum, value) => sum + value, 0) / values.length) : null;
  const byWorkType = (["Remote", "Hybrid", "Onsite", "Not Listed"] as WorkType[]).map((type) => ({
    label: type,
    count: jobs.filter((job) => job.workType === type).length,
  }));
  const roleTypes = ["IC", "MGR", "EXEC", "UNKNOWN"];
  const byRoleType = roleTypes.map((type) => ({
    label: type,
    count: jobs.filter((job) => (job.roleType || "UNKNOWN") === type).length,
  }));
  const averagePayByRoleType = roleTypes.map((type) => {
    const roleJobs = jobs.filter((job) => (job.roleType || "UNKNOWN") === type && (job.payMin !== null || job.payMax !== null));
    return {
      label: type,
      count: average(roleJobs.map((job) => (job.payMin ?? job.payMax) as number)),
    };
  });
  const platforms = countTop([...companies.map((company) => company.jobPlatform), ...jobs.map((job) => job.jobPlatform)]);
  const titleKeywords = countTop(
    ["Systems", "Administrator", "Engineer", "Manager", "Analyst", "Core", "Banking", "Infrastructure", "Security"].flatMap(
      (keyword) => jobs.filter((job) => job.title.toLowerCase().includes(keyword.toLowerCase())).map(() => keyword),
    ),
  );
  const sevenDaysAgo = new Date();
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
  const applied = jobs.filter((job) => job.applied);
  const responses = applied.filter((job) =>
    ["Interview Scheduled", "Rejected", "Offer"].includes(job.applicationStatus),
  ).length;

  return {
    totalCompanies: companies.length,
    totalJobs: jobs.length,
    byWorkType,
    byRoleType,
    averagePayByRoleType,
    averageMinPay: average(minPayJobs.map((job) => job.payMin as number)),
    averageMaxPay: average(maxPayJobs.map((job) => job.payMax as number)),
    jobsWithPay: jobsWithPay.length,
    jobsMissingPay: jobs.length - jobsWithPay.length,
    platforms,
    titleKeywords,
    jobsAddedLast7Days: jobs.filter((job) => job.postedDate && new Date(job.postedDate) >= sevenDaysAgo).length,
    jobsAppliedFor: applied.length,
    responseRate: applied.length ? Math.round((responses / applied.length) * 100) : null,
  };
}

function countTop(values: string[]) {
  const counts = values
    .filter((value) => value && value !== "Not detected")
    .reduce<Record<string, number>>((acc, value) => {
      acc[value] = (acc[value] || 0) + 1;
      return acc;
    }, {});
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([label, count]) => ({ label, count }));
}
