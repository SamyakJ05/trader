import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BacktestsPage from "./page";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/components/Shell", () => ({ default: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }));
vi.mock("@/lib/api", () => ({ api: request }));
vi.mock("@/lib/useApi", () => ({ useApi: () => ({ data: [], reload: vi.fn() }) }));

beforeEach(() => { request.mockReset(); });
describe("Backtests", () => {
  it("submits IST dates and renders API errors", async () => {
    request.mockRejectedValue(new Error("Not enough completed candles"));
    render(<BacktestsPage />);
    fireEvent.change(screen.getByLabelText("Start date (IST)"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("End date (exclusive, IST)"), { target: { value: "2026-09-10" } });
    fireEvent.click(screen.getByRole("button", { name: "Run backtest" }));
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    const body = JSON.parse(request.mock.calls[0][1].body);
    expect(body.start).toBe("2026-09-01T00:00:00+05:30");
    expect(body.source).toBe("yfinance_unadjusted");
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("Not enough completed candles");
  });
});
