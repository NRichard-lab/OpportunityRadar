export interface FeatureFlags {
  browserJobs: boolean;
  companyRefresh: boolean;
  utilities: boolean;
  schedules: boolean;
  discovery: boolean;
}

export const disabledFeatureFlags: FeatureFlags = {
  browserJobs: false,
  companyRefresh: false,
  utilities: false,
  schedules: false,
  discovery: false,
};

export function normalizeFeatureFlags(value: Partial<FeatureFlags> | null | undefined): FeatureFlags {
  return {
    browserJobs: value?.browserJobs === true,
    companyRefresh: value?.companyRefresh === true,
    utilities: value?.utilities === true,
    schedules: value?.schedules === true,
    discovery: value?.discovery === true,
  };
}
