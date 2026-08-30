import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Filters, stateOptions } from "./Companies";

const noOp = () => undefined;

describe("Companies filters", () => {
  it("displays state names alphabetically while retaining backend values", () => {
    const options = stateOptions(["WA", "AL", "Wyoming", "AK", "AZ"]);

    expect(options).toEqual([
      { value: "AL", label: "Alabama (AL)", name: "Alabama" },
      { value: "AK", label: "Alaska (AK)", name: "Alaska" },
      { value: "AZ", label: "Arizona (AZ)", name: "Arizona" },
      { value: "WA", label: "Washington (WA)", name: "Washington" },
      { value: "Wyoming", label: "Wyoming (WY)", name: "Wyoming" },
    ]);
  });

  it("renders only the requested Companies page filters", () => {
    const markup = renderToStaticMarkup(<Filters
      query=""
      setQuery={noOp}
      state=""
      setState={noOp}
      industry=""
      setIndustry={noOp}
      hasActiveJobs=""
      setHasActiveJobs={noOp}
      sortBy="companyName"
      setSortBy={noOp}
      sortDirection="asc"
      setSortDirection={noOp}
      pageSize={25}
      setPageSize={noOp}
      options={{
        states: ["WY", "AL", "WA"],
        industries: ["Financial Services"],
        jobBoardTypes: ["Workday"],
        discoveryStatuses: ["Completed"],
      }}
      onClear={noOp}
    />);

    expect(markup).toContain("All States");
    expect(markup.indexOf("Alabama (AL)")).toBeLessThan(markup.indexOf("Washington (WA)"));
    expect(markup.indexOf("Washington (WA)")).toBeLessThan(markup.indexOf("Wyoming (WY)"));
    expect(markup).toContain('value="AL"');
    expect(markup).toContain("Has Active Jobs: All");
    expect(markup).toContain("Clear Filters");
    expect(markup).not.toContain("All Job Board Type");
    expect(markup).not.toContain("All Discovery Status");
    expect(markup).not.toContain("Verified Job Board: All");
  });
});
