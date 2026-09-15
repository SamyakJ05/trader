"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Wordmark } from "@/components/Wordmark";

/**
 * Shown when a page throws. Next's default is a bare "Application error" with
 * no way back, which is the wrong thing to show someone who may have an open
 * position and now cannot see it.
 *
 * So this says what is still true — the failure is in this screen, not in the
 * trading engine — and offers the two things worth offering: try again, or go
 * somewhere that works.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The digest is the only handle on the server-side stack, which is not
    // sent to the browser. Worth having in the console when someone reports
    // a screen that broke.
    console.error("page_error", error);
  }, [error]);

  return (
    <main className="flex min-h-screen flex-col items-center justify-center px-6">
      <div className="w-full max-w-md text-center">
        <Wordmark size="lg" />

        <h1 className="mt-8 text-lg font-medium text-ink">This screen failed to load</h1>
        <p className="mt-2 text-sm text-ink-dim">
          Your orders, positions and strategies are unaffected — the failure is in
          displaying this page, not in the trading engine.
        </p>

        <div className="mt-6 flex items-center justify-center gap-3">
          <button
            onClick={reset}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-bg transition hover:bg-accent/90"
          >
            Try again
          </button>
          <Link
            href="/dashboard"
            className="rounded-lg border border-line-2 px-4 py-2 text-sm text-ink-dim transition hover:border-line-2 hover:text-ink"
          >
            Go to dashboard
          </Link>
        </div>

        {error.digest && (
          <p className="mt-8 font-mono text-xs text-ink-faint">
            Reference {error.digest}
          </p>
        )}
      </div>
    </main>
  );
}
