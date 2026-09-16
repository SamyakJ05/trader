"use client";

import { useApi } from "@/lib/useApi";

type Egress = {
  detected: string | null;
  // One address, or several comma-separated: brokers typically register a
  // primary and a secondary, and either may be the one orders leave from.
  expected: string | null;
  status: "match" | "mismatch" | "unknown" | "unconfigured";
  live_trading_enabled: boolean;
};

/**
 * Whether orders can leave this host at all.
 *
 * SEBI's algo framework requires order requests to originate from an IP the
 * broker has whitelisted. Orders from anywhere else are rejected while reads,
 * market data and the websocket keep working perfectly — so the platform
 * looks healthy and only trading is broken, and the rejection never says why.
 *
 * This was visible only in a startup log line, which means a droplet rebuild
 * or a detached reserved IP was discovered by a failed order during market
 * hours rather than before it.
 */
export function EgressBanner() {
  // Five minutes: the address changes only when the host does, and the lookup
  // leaves to a third-party echo service.
  const { data } = useApi<Egress>("/system/egress-ip", 300000);
  if (!data) return null;

  // Nothing to say when no address is registered — a paper-only deployment
  // never registers one, and nagging about a setting it does not need is
  // noise that teaches people to ignore banners.
  if (data.status === "unconfigured") return null;

  if (data.status === "match") {
    return (
      <p className="mb-4 text-xs text-ink-faint">
        Outbound IP <span className="num">{data.detected}</span> is registered
        with the broker.
      </p>
    );
  }

  if (data.status === "unknown") {
    return (
      <p className="mb-4 text-xs text-ink-faint">
        Could not determine this host&rsquo;s outbound IP — the echo service is
        unreachable. This says nothing about whether it is correct; the
        registered address is <span className="num">{data.expected}</span>.
      </p>
    );
  }

  return (
    <div className="mb-4 rounded border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss">
      <p className="font-semibold">
        Outbound IP does not match the address registered with your broker.
      </p>
      <p className="mt-1 text-xs">
        Leaving from <span className="num">{data.detected}</span>; registered:{" "}
        <span className="num">{data.expected}</span>.{" "}
        {data.live_trading_enabled
          ? "Live orders will be rejected while reads keep working, and the rejection will not say why."
          : "Live trading is off, so nothing is failing yet — but orders would be rejected if it were enabled."}{" "}
        Register this address with the broker — most allow a primary and a
        secondary — and add it to BROKER_STATIC_IP (comma-separated) so both
        are tracked.
      </p>
    </div>
  );
}
