import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { KillSwitchBanner } from "./KillSwitchBanner";

describe("KillSwitchBanner", () => {
  it("renders alert with reason when engaged", () => {
    render(
      <KillSwitchBanner
        status={{ global_engaged: true, global_reason: "daily loss", killed_strategies: [] }}
      />
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("GLOBAL KILL SWITCH ENGAGED");
    expect(alert.textContent).toContain("daily loss");
  });

  it("renders nothing when not engaged", () => {
    const { container } = render(
      <KillSwitchBanner
        status={{ global_engaged: false, global_reason: null, killed_strategies: [] }}
      />
    );
    expect(container.innerHTML).toBe("");
  });

  it("renders nothing while status is loading", () => {
    const { container } = render(<KillSwitchBanner status={null} />);
    expect(container.innerHTML).toBe("");
  });
});
