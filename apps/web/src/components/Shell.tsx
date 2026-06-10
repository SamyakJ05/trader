"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, getToken, setToken } from "@/lib/api";
import { KillSwitchStatus } from "@/lib/types";
import { KillSwitchBanner } from "./broker/KillSwitchBanner";
import { Pill } from "./ui";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/brokers", label: "Brokers" },
  { href: "/positions", label: "Positions" },
  { href: "/orders", label: "Orders" },
  { href: "/strategies", label: "Strategies" },
  { href: "/risk", label: "Risk" },
  { href: "/audit", label: "Audit Log" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [kill, setKill] = useState<KillSwitchStatus | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    const load = () => api<KillSwitchStatus>("/system/killswitch").then(setKill).catch(() => {});
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [router]);

  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-zinc-800 bg-zinc-900 p-4">
        <div className="mb-6">
          <h1 className="text-lg font-bold">trader</h1>
          <div className="mt-2 flex gap-2">
            <Pill value="paper" label="PAPER MODE" />
          </div>
        </div>
        <nav className="space-y-1">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded-md px-3 py-2 text-sm ${
                pathname?.startsWith(item.href)
                  ? "bg-zinc-800 text-white"
                  : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <button
          onClick={() => {
            api("/auth/logout", { method: "POST" }).catch(() => {});
            setToken(null);
            router.replace("/login");
          }}
          className="mt-8 text-sm text-zinc-500 hover:text-zinc-300"
        >
          Log out
        </button>
      </aside>
      <main className="flex-1 p-6">
        <KillSwitchBanner status={kill} />
        {children}
      </main>
    </div>
  );
}
