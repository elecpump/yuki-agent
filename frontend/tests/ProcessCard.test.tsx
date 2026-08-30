import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProcessCard } from "../src/components/console/health/ProcessCard";

describe("ProcessCard", () => {
  it("prioritizes stale heartbeat state over the last reported healthy value", () => {
    render(
      <ProcessCard
        name="yuki"
        process={{
          process: "yuki",
          healthy: true,
          fresh: false,
          last_seen_age_s: 42,
          components: {},
        }}
      />,
    );
    expect(screen.getByText("心跳过期")).toBeInTheDocument();
    expect(screen.getByText(/心跳 42.0s 前/)).toBeInTheDocument();
  });
});
