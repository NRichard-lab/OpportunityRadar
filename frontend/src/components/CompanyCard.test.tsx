import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { isCompany } from "../runtimeSchemas";
import type { Company } from "../types/Company";
import { CompanyModal } from "../pages/Companies";
import { CompanyCard } from "./CompanyCard";

const description = "A member-owned financial cooperative serving families and local businesses.";
const company: Company & Record<string, unknown> = {
  id: "company-example",
  name: "Example Credit Union",
  companyDescription: description,
  industry: "Financial Services",
  city: "Denver",
  state: "CO",
  country: "United States",
  knownWebsite: "https://example.org",
  officialWebsite: "https://example.org",
  websiteDiscoveryMethod: "Static Link",
  websiteCandidateUrls: "",
  websiteVerificationNotes: "Confirmed from the official site.",
  websiteVerified: true,
  careersPageUrl: "https://example.org/careers",
  jobBoardUrl: "https://jobs.example.org",
  jobBoardDiscoveryMethod: "Static Link",
  jobsRssFeedUrl: "",
  jobPlatform: "Self-Hosted",
  feedFound: false,
  searchStatus: "Completed",
  confidence: 100,
  lastChecked: "2026-08-28T12:00:00-06:00",
  notes: "User-entered note.",
  foundedYear: null,
  totalAssets: null,
  assetsAsOfDate: "",
  companyInfoLastChecked: "2026-08-28T12:00:00-06:00",
};

function text(markup: string): string {
  return markup.replace(/<[^>]+>/g, " ").replace(/&[^;]+;/g, " ").replace(/\s+/g, " ").trim();
}

describe("Company description management", () => {
  it("accepts the API field and rejects company payloads that omit it", () => {
    expect(isCompany(company)).toBe(true);
    const { companyDescription: _omitted, ...withoutDescription } = company;
    expect(isCompany(withoutDescription)).toBe(false);
  });

  it("shows the saved description in company details", () => {
    const markup = renderToStaticMarkup(<CompanyCard
      company={company}
      appliedCount={0}
      jobCount={0}
      onViewJobs={() => undefined}
      onEdit={() => undefined}
      onDelete={() => undefined}
      onRefresh={() => undefined}
      refreshEnabled
      canAdminister={false}
    />);

    const content = text(markup);
    expect(content).toContain("Company description");
    expect(content).toContain(description);
  });

  it("prefills the edit form so saving preserves the existing description", () => {
    const markup = renderToStaticMarkup(<CompanyModal
      company={company}
      busy={false}
      error=""
      onClose={() => undefined}
      onSave={async () => undefined}
    />);

    const content = text(markup);
    expect(content).toContain("Company Description");
    expect(content).toContain(description);
  });
});
