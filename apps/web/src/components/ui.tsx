"use client";

import { ReactNode, useEffect } from "react";

/* ── Layout ──────────────────────────────────────────────────────── */

export function PageHeader({
  title,
  sub,
  action,
}: {
  title: string;
  sub?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex items-end justify-between">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {sub && <p className="mt-1 text-sm text-ink-dim">{sub}</p>}
      </div>
      {action}
    </div>
  );
}

export function Card({
  title,
  children,
  action,
  pad = true,
  className = "",
}: {
  title?: string;
  children: ReactNode;
  action?: ReactNode;
  pad?: boolean;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-line bg-panel ${pad ? "p-4" : ""} ${className}`}>
      {(title || action) && (
        <div className={`flex items-center justify-between ${pad ? "mb-3" : "p-4 pb-0"}`}>
          {title && (
            <h2 className="text-xs font-semibold uppercase tracking-wider text-ink-dim">{title}</h2>
          )}
          {action}
        </div>
      )}
      {children}
    </div>
  );
}

export function StatCard({
  label,
  value,
  sub,
  spark,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  spark?: ReactNode;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wider text-ink-dim">{label}</p>
          <p className="num mt-2 truncate text-2xl font-semibold">{value}</p>
          {sub && <p className="mt-1 truncate text-xs text-ink-faint">{sub}</p>}
        </div>
        {spark && <div className="shrink-0 pt-1">{spark}</div>}
      </div>
    </Card>
  );
}

/* ── Status pills ────────────────────────────────────────────────── */

const PILL_STYLES: Record<string, string> = {
  // environments
  paper: "bg-gain/10 text-gain border-gain/30",
  live: "bg-loss/10 text-loss border-loss/30",
  // adapter status
  working: "bg-gain/10 text-gain border-gain/30",
  scaffold: "bg-warn/10 text-warn border-warn/30",
  planned: "bg-panel-2 text-ink-faint border-line-2",
  // generic severity
  warn: "bg-warn/10 text-warn border-warn/30",
  // connection
  connected: "bg-gain/10 text-gain border-gain/30",
  disconnected: "bg-panel-2 text-ink-faint border-line-2",
  pending_auth: "bg-warn/10 text-warn border-warn/30",
  session_expired: "bg-warn/10 text-warn border-warn/30",
  error: "bg-loss/10 text-loss border-loss/30",
  // order / proposal status
  FILLED: "bg-gain/10 text-gain border-gain/30",
  PARTIALLY_FILLED: "bg-accent/10 text-accent border-accent/30",
  OPEN: "bg-accent/10 text-accent border-accent/30",
  ACCEPTED: "bg-accent/10 text-accent border-accent/30",
  SUBMITTED: "bg-accent/10 text-accent border-accent/30",
  CANCELLED: "bg-panel-2 text-ink-faint border-line-2",
  REJECTED: "bg-loss/10 text-loss border-loss/30",
  REJECTED_RISK: "bg-loss/10 text-loss border-loss/30",
  FAILED: "bg-loss/10 text-loss border-loss/30",
  RUNNING: "bg-gain/10 text-gain border-gain/30",
  STOPPED: "bg-panel-2 text-ink-faint border-line-2",
  KILLED: "bg-loss/10 text-loss border-loss/30",
  DRAFT: "bg-panel-2 text-ink-faint border-line-2",
  ERROR: "bg-loss/10 text-loss border-loss/30",
  PROPOSED: "bg-accent/10 text-accent border-accent/30",
  APPROVED: "bg-gain/10 text-gain border-gain/30",
  // sides
  BUY: "bg-gain/10 text-gain border-gain/30",
  SELL: "bg-loss/10 text-loss border-loss/30",
};

export function Pill({ value, label }: { value: string; label?: string }) {
  const style = PILL_STYLES[value] ?? "bg-panel-2 text-ink-dim border-line-2";
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${style}`}
    >
      {label ?? value}
    </span>
  );
}

/* ── Tables ──────────────────────────────────────────────────────── */

export function Th({ children, right }: { children: ReactNode; right?: boolean }) {
  return (
    <th
      className={`px-3 py-2 text-xs font-semibold uppercase tracking-wider text-ink-faint ${
        right ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  className = "",
  right,
}: {
  children: ReactNode;
  className?: string;
  right?: boolean;
}) {
  return (
    <td className={`px-3 py-2.5 text-sm ${right ? "num text-right" : ""} ${className}`}>
      {children}
    </td>
  );
}

/* ── Controls ────────────────────────────────────────────────────── */

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type,
  size = "md",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "danger" | "primary" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  size?: "sm" | "md";
}) {
  const styles = {
    default: "border-line-2 bg-panel-2 hover:border-ink-faint text-ink",
    primary: "border-accent/50 bg-accent/15 hover:bg-accent/25 text-accent",
    danger: "border-loss/40 bg-loss/10 hover:bg-loss/20 text-loss",
    ghost: "border-transparent bg-transparent hover:bg-panel-2 text-ink-dim hover:text-ink",
  }[variant];
  const sizing = size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-1.5 text-sm";
  return (
    <button
      type={type ?? "button"}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg border font-medium transition-colors disabled:pointer-events-none disabled:opacity-40 ${sizing} ${styles}`}
    >
      {children}
    </button>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line-2 bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent/60 focus:outline-none";

export const labelClass = "mb-1 block text-xs font-medium uppercase tracking-wider text-ink-faint";

/* ── Feedback ────────────────────────────────────────────────────── */

export function Pnl({ value }: { value: string | number }) {
  const n = Number(value);
  const color = n > 0 ? "text-gain" : n < 0 ? "text-loss" : "text-ink-dim";
  return (
    <span className={`num ${color}`}>
      {n > 0 ? "+" : ""}
      {n.toFixed(2)}
    </span>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p className="mt-2 rounded-lg border border-loss/30 bg-loss/10 p-2.5 text-sm text-loss">
      {message}
    </p>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-panel-2 ${className}`} />;
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-line-2 py-12 text-center">
      <p className="text-sm font-medium text-ink-dim">{title}</p>
      {hint && <p className="mt-1 max-w-sm text-xs text-ink-faint">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/* ── Modal ───────────────────────────────────────────────────────── */

export function Modal({
  open,
  onClose,
  title,
  width = "w-[34rem]",
  children,
}: {
  open: boolean;
  onClose: () => void;
  title?: string;
  width?: string;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className={`max-h-[85vh] overflow-y-auto rounded-xl border border-line-2 bg-panel p-6 shadow-2xl shadow-black/50 ${width}`}
        onClick={(e) => e.stopPropagation()}
      >
        {title && <h2 className="mb-4 text-lg font-semibold tracking-tight">{title}</h2>}
        {children}
      </div>
    </div>
  );
}

/* ── Sparkline ───────────────────────────────────────────────────── */

export function Sparkline({
  points,
  width = 96,
  height = 32,
}: {
  points: number[];
  width?: number;
  height?: number;
}) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(height - ((p - min) / span) * (height - 4) - 2).toFixed(1)}`)
    .join(" ");
  const up = points[points.length - 1] >= points[0];
  return (
    <svg width={width} height={height} className={up ? "text-gain" : "text-loss"}>
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
