"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, getToken, setToken } from "@/lib/api";
import { KillSwitchStatus, User } from "@/lib/types";
import { KillSwitchBanner } from "./broker/KillSwitchBanner";
import { Pill } from "./ui";
import {
  IconAI,
  IconAudit,
  IconBroker,
  IconDashboard,
  IconLogout,
  IconOrders,
  IconPositions,
  IconRisk,
  IconSettings,
  IconStrategy,
} from "./icons";

const NAV_GROUPS: { label: string; items: { href: string; label: string; icon: ReactNode }[] }[] = [
  {
    label: "Trading",
    items: [
      { href: "/dashboard", label: "Dashboard", icon: <IconDashboard /> },
      { href: "/brokers", label: "Brokers", icon: <IconBroker /> },
      { href: "/positions", label: "Positions", icon: <IconPositions /> },
      { href: "/orders", label: "Orders", icon: <IconOrders /> },
    ],
  },
  {
    label: "AI",
    items: [{ href: "/ai", label: "AI Trading", icon: <IconAI /> }],
  },
  {
    label: "System",
    items: [
      { href: "/strategies", label: "Strategies", icon: <IconStrategy /> },
      { href: "/risk", label: "Risk", icon: <IconRisk /> },
      { href: "/audit", label: "Audit Log", icon: <IconAudit /> },
      { href: "/security", label: "Security", icon: <IconSettings /> },
      { href: "/devices", label: "Devices", icon: <IconSettings /> },
    ],
  },
];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [kill, setKill] = useState<KillSwitchStatus | null>(null);
  const [me, setMe] = useState<User | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    const load = () => api<KillSwitchStatus>("/system/killswitch").then(setKill).catch(() => {});
    load();
    api<User>("/auth/me").then(setMe).catch(() => {});
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [router]);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-panel px-4 py-5">
        <div className="mb-7 px-2">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold tracking-tight">
              trader<span className="text-accent">_</span>
            </span>
            <Pill value="paper" label="PAPER" />
          </div>
        </div>

        <nav className="flex-1 space-y-5">
          {[
            ...NAV_GROUPS,
            ...(me?.is_admin
              ? [
                  {
                    label: "Operator",
                    items: [
                      { href: "/admin", label: "Users", icon: <IconSettings /> },
                    ],
                  },
                ]
              : []),
          ].map((group) => (
            <div key={group.label}>
              <p className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-ink-faint">
                {group.label}
              </p>
              <div className="space-y-0.5">
                {group.items.map((item) => {
                  const active = pathname?.startsWith(item.href);
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={`flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm transition-colors ${
                        active
                          ? "bg-panel-2 font-medium text-ink"
                          : "text-ink-dim hover:bg-panel-2/60 hover:text-ink"
                      }`}
                    >
                      <span className={active ? "text-accent" : "text-ink-faint"}>{item.icon}</span>
                      {item.label}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="mt-6 space-y-3 border-t border-line pt-4">
          {kill && (
            <div className="flex items-center gap-2 px-2">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  kill.global_engaged ? "bg-loss" : "bg-gain"
                }`}
              />
              <span className="text-xs text-ink-faint">
                Kill switch {kill.global_engaged ? "ENGAGED" : "normal"}
              </span>
            </div>
          )}
          <button
            onClick={() => {
              api("/auth/logout", { method: "POST" }).catch(() => {});
              setToken(null);
              router.replace("/login");
            }}
            className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm text-ink-faint transition-colors hover:bg-panel-2/60 hover:text-ink"
          >
            <IconLogout />
            Log out
          </button>
        </div>
      </aside>

      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-[1400px] px-8 py-6">
          <KillSwitchBanner status={kill} />
          {children}
        </div>
      </main>
    </div>
  );
}
