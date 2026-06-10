import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BrokerConnectionCard } from "./BrokerConnectionCard";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";

function account(overrides: Partial<BrokerAccount> = {}): BrokerAccount {
  return {
    id: "acc-1",
    broker: "zerodha",
    label: "Main",
    environment: "paper",
    status: "disconnected",
    status_message: null,
    live_enabled: false,
    last_sync_at: null,
    read_verified_at: null,
    credential_ref: "ZERODHA_MAIN",
    broker_client_id: null,
    adapter_status: "scaffold",
    credentials: {
      env_keys_configured: true,
      env_access_token_configured: false,
      session_token_configured: false,
      session_expires_at: null,
    },
    ...overrides,
  };
}

const zerodhaCaps = {
  broker: "zerodha",
  adapter_status: "scaffold",
  display_name: "Zerodha Kite Connect",
  notes: "Adapter scaffolded. NOT verified against a live account.",
  place_order: true,
  holdings: true,
  positions: true,
  funds: true,
  instruments_dump: true,
  websocket_ticks: true,
  order_postbacks: true,
  amo_orders: true,
  bracket_gtt: true,
  modify_order: true,
  cancel_order: true,
  auth_model: "",
  session_validity: "",
  rate_limit_notes: "",
  exchanges: ["NSE"],
} as BrokerCapabilities;

describe("BrokerConnectionCard truthfulness", () => {
  it("scaffolded broker shows scaffold + unverified chips, never 'verified'", () => {
    render(
      <BrokerConnectionCard account={account()} capabilities={zerodhaCaps} onAction={vi.fn()} />
    );
    expect(screen.getByText("adapter: scaffold")).toBeDefined();
    expect(screen.getByText("trade path unverified")).toBeDefined();
    expect(screen.getByText("read unverified")).toBeDefined();
    expect(screen.queryByText(/trade path: verified/)).toBeNull();
  });

  it("paper account shows working trade path and paper environment", () => {
    render(
      <BrokerConnectionCard
        account={account({ broker: "paper", adapter_status: "working", status: "connected" })}
        onAction={vi.fn()}
      />
    );
    expect(screen.getByText("trade path: working (paper)")).toBeDefined();
    expect(screen.getAllByText("paper").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("adapter: working")).toBeDefined();
    expect(screen.getByText("live disabled")).toBeDefined();
  });

  it("errored account surfaces status_message", () => {
    render(
      <BrokerConnectionCard
        account={account({ status: "error", status_message: "Kite token exchange failed" })}
        onAction={vi.fn()}
      />
    );
    expect(screen.getByText("error")).toBeDefined();
    expect(screen.getByText("Kite token exchange failed")).toBeDefined();
  });

  it("warns when env credentials are missing", () => {
    render(
      <BrokerConnectionCard
        account={account({
          credentials: {
            env_keys_configured: false,
            env_access_token_configured: false,
            session_token_configured: false,
            session_expires_at: null,
          },
        })}
        onAction={vi.fn()}
      />
    );
    expect(screen.getByText(/API credentials missing/)).toBeDefined();
  });

  it("connected read-verified account shows the verified chip", () => {
    render(
      <BrokerConnectionCard
        account={account({
          status: "connected",
          read_verified_at: "2026-06-10T10:00:00Z",
          last_sync_at: "2026-06-10T10:05:00Z",
        })}
        onAction={vi.fn()}
      />
    );
    expect(screen.getByText(/read verified/)).toBeDefined();
    expect(screen.getByText(/synced/)).toBeDefined();
  });
});
