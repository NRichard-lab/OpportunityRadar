export type SearchStatus = "Completed" | "Partial" | "Needs Review" | "Failed" | (string & {});

export interface Company {
  id: string;
  name: string;
  industry?: string;
  city: string;
  state: string;
  country?: string;
  knownWebsite: string;
  officialWebsite: string;
  careersPageUrl: string;
  jobBoardUrl: string;
  jobBoardDiscoveryMethod: "Static Link" | "Browser Click" | "Manual" | "Not Found" | string;
  jobsRssFeedUrl: string;
  jobPlatform: string;
  feedFound: boolean;
  searchStatus: SearchStatus;
  confidence: number;
  lastChecked: string;
  notes: string;
  activeJobCount?: number;
  jobCount?: number;
  appliedCount?: number;
  lastCollectionDate?: string;
  foundedYear?: number | null;
  totalAssets?: number | null;
  assetsAsOfDate?: string;
  companyInfoLastChecked?: string;
  jobBoardReverificationRequired?: boolean;
}
