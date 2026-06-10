"use client";

import { ReactNode } from "react";

export function Card({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      {(title || action) && (
        <div className="mb-3 flex items-center justify-between">
          {title && <h2 className="text-sm font-semibold text-zinc-300">{title}</h2>}
          {action}
        </div>
      )}
      {children}
    </div>
  );
}

const PILL_STYLES: Record<string, string> = {
  // environments
  paper: "bg-emerald-900/60 text-emerald-300 border-emerald-700",
  live: "bg-red-900/60 text-red-300 border-red-700",
  // adapter status
  working: "bg-emerald-900/60 text-emerald-300 border-emerald-700",
  scaffold: "bg-amber-900/60 text-amber-300 border-amber-700",
  planned: "bg-zinc-800 text-zinc-400 border-zinc-700",
  // connection
  connected: "bg-emerald-900/60 text-emerald-300 border-emerald-700",
  disconnected: "bg-zinc-800 text-zinc-400 border-zinc-700",
  pending_auth: "bg-amber-900/60 text-amber-300 border-amber-700",
  session_expired: "bg-amber-900/60 text-amber-300 border-amber-700",
  error: "bg-red-900/60 text-red-300 border-red-700",
  // order status
  FILLED: "bg-emerald-900/60 text-emerald-300 border-emerald-700",
  PARTIALLY_FILLED: "bg-sky-900/60 text-sky-300 border-sky-700",
  OPEN: "bg-sky-900/60 text-sky-300 border-sky-700",
  ACCEPTED: "bg-sky-900/60 text-sky-300 border-sky-700",
  SUBMITTED: "bg-sky-900/60 text-sky-300 border-sky-700",
  CANCELLED: "bg-zinc-800 text-zinc-400 border-zinc-700",
  REJECTED: "bg-red-900/60 text-red-300 border-red-700",
  REJECTED_RISK: "bg-red-900/60 text-red-300 border-red-700",
  FAILED: "bg-red-900/60 text-red-300 border-red-700",
  RUNNING: "bg-emerald-900/60 text-emerald-300 border-emerald-700",
  STOPPED: "bg-zinc-800 text-zinc-400 border-zinc-700",
  KILLED: "bg-red-900/60 text-red-300 border-red-700",
  DRAFT: "bg-zinc-800 text-zinc-400 border-zinc-700",
  ERROR: "bg-red-900/60 text-red-300 border-red-700",
};

export function Pill({ value, label }: { value: string; label?: string }) {
  const style = PILL_STYLES[value] ?? "bg-zinc-800 text-zinc-300 border-zinc-700";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${style}`}>
      {label ?? value}
    </span>
  );
}

export function Th({ children }: { children: ReactNode }) {
  return <th className="px-3 py-2 text-left text-xs font-semibold uppercase text-zinc-500">{children}</th>;
}

export function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`px-3 py-2 text-sm ${className}`}>{children}</td>;
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "danger" | "primary";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  const styles = {
    default: "border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-zinc-200",
    danger: "border-red-800 bg-red-900/50 hover:bg-red-900 text-red-200",
    primary: "border-sky-700 bg-sky-800 hover:bg-sky-700 text-sky-100",
  }[variant];
  return (
    <button
      type={type ?? "button"}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium disabled:opacity-40 ${styles}`}
    >
      {children}
    </button>
  );
}

export function Pnl({ value }: { value: string | number }) {
  const n = Number(value);
  const color = n > 0 ? "text-emerald-400" : n < 0 ? "text-red-400" : "text-zinc-400";
  return <span className={color}>{n.toFixed(2)}</span>;
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return <p className="mt-2 rounded border border-red-800 bg-red-950/50 p-2 text-sm text-red-300">{message}</p>;
}
