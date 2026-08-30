import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OpportunityRadarBrand } from "./App";

describe("OpportunityRadar branding", () => {
  it("uses the cache-busted radar image without distorting it", () => {
    const markup = renderToStaticMarkup(<OpportunityRadarBrand />);

    expect(markup).toContain("opportunity-radar-icon-20260830-192.png");
    expect(markup).toContain('width="44"');
    expect(markup).toContain('height="44"');
    expect(markup).toContain("object-contain");
    expect(markup).toContain("Opportunity Radar");
    expect(markup).not.toContain("<svg");
  });
});
