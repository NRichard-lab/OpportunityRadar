import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Companies, JobDetails, type Company, type Job } from "./App";

const becuStaffDeveloper: Job = {
  id: 38,
  company_id: 1,
  title: "Staff Software Developer - AI Innovation Team",
  company_name: "BECU",
  location: "Remote-WA",
  department: "",
  employment_type: "Full time",
  role_classification: "Unclassified",
  posted_date: "",
  detail_url: "https://becu.wd1.myworkdayjobs.com/External/job/Remote-WA/Staff-Software-Developer---AI-Innovation-Team_R-12991",
  description: "PAY RANGE The Target Pay Range is $171,700.00-$209,800.00 annually. The full Pay Range is $133,100.00-$248,300.00 annually. BENEFITS Medical, Dental and Vision.",
  pay_min: 171700,
  pay_max: 209800,
  target_pay_min: 171700,
  target_pay_max: 209800,
  full_pay_min: 133100,
  full_pay_max: 248300,
  pay_currency: "USD",
  pay_period: "annual",
  pay_display: "$171,700.00-$209,800.00 annually",
  incentives_text: "Performance-based incentives",
  benefits_summary: "",
  benefit_tags: "[]",
  compensation_source_text: "Target $171,700.00-$209,800.00; full $133,100.00-$248,300.00; performance-based incentives.",
  benefits_source_text: "401(k) match up to 3%; 4% annual contribution; Medical, Dental and Vision; PTO and Exchange Program; Tuition Reimbursement; volunteer time off and donation match.",
  has_health_insurance: 1,
  has_dental_insurance: 1,
  has_vision_insurance: 1,
  has_retirement: 1,
  retirement_details: "401(k) company match up to 3%; 4% annual contribution",
  retirement_match_percent: 3,
  retirement_contribution_percent: 4,
  has_pto: 1,
  pto_details: "PTO / Exchange Program",
  has_tuition_reimbursement: 1,
  tuition_details: "Tuition Reimbursement Program",
  has_volunteer_time_off: 1,
  has_donation_match: 1,
  has_remote_hybrid: 1,
  other_benefit_details: "Volunteer time off; Donation match; Exchange Program",
};

describe("BECU structured Job Details", () => {
  it("renders saved API fields instead of the undisclosed fallback", () => {
    const html = renderToStaticMarkup(<JobDetails job={becuStaffDeveloper} onClose={() => undefined} />);
    expect(html).toContain("$171,700.00");
    expect(html).toContain("$209,800.00");
    expect(html).toContain("$133,100.00");
    expect(html).toContain("$248,300.00");
    expect(html).toContain("Performance-based incentives");
    expect(html).toContain("Up to 3%");
    expect(html).toContain("4% annual contribution");
    expect(html).toContain("Health insurance");
    expect(html).toContain("Dental insurance");
    expect(html).toContain("Vision insurance");
    expect(html).toContain("PTO / Exchange Program");
    expect(html).toContain("Tuition Reimbursement Program");
    expect(html).toContain("Volunteer time off");
    expect(html).toContain("Donation match");
    expect(html).not.toContain("Not listed by employer");
  });
});

const company = (id: number, name: string, boardType: string, url: string): Company => ({
  id, name, company_website: `https://www.${name.toLowerCase()}.org`, careers_page_url: `https://www.${name.toLowerCase()}.org/careers`,
  verified_job_board_url: url, job_board_type: boardType, discovery_status: "Verified", classification_confidence: "High",
  discovery_method: "Followed Careers → Jobs link", last_verified_at: "2026-08-22T12:00:00Z", needs_manual_refresh: 0,
  last_collection_status: "Completed", industry: "Financial Services", city: "", state: "", country: "United States",
  founded_year: null, total_assets: null, total_assets_display: "", assets_as_of_date: "", information_source_note: "",
  location_discovery_source: "", location_confidence: "Not Found", possible_locations: "", notes: "", job_count: 0,
  last_collector: boardType, last_collection_at: "", last_raw_count: 0, last_saved_count: 0, last_review_count: 0, last_collection_error: "",
});

describe("Company discovery table mapping", () => {
  it("renders canonical SQLite/API-shaped fields in the correct columns", () => {
    const rows = [
      company(1, "BECU", "Workday", "https://becu.wd1.myworkdayjobs.com/External"),
      company(2, "WECU", "ADP", "https://workforcenow.adp.com/wecu"),
      company(3, "Bellco", "Self-Hosted / In-House", "https://www.bellco.org/careers/openings"),
    ];
    const html = renderToStaticMarkup(<Companies companies={rows} onSaved={async () => undefined} />);
    expect(html).toContain("Job Board Type");
    expect(html).toContain("Discovery Status");
    expect(html).toContain("becu.wd1.myworkdayjobs.com/External");
    expect(html).toContain("Workday");
    expect(html).toContain("workforcenow.adp.com/wecu");
    expect(html).toContain("ADP");
    expect(html).toContain("Self-Hosted / In-House");
    expect(html.match(/Discovery Details/g)?.length).toBe(3);
    expect(html.match(/Verified/g)?.length).toBeGreaterThanOrEqual(3);
  });
});
