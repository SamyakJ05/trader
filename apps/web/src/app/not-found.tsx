import Link from "next/link";
import { Wordmark } from "@/components/Wordmark";

export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center px-6">
      <div className="w-full max-w-md text-center">
        <Wordmark size="lg" />

        <h1 className="mt-8 text-lg font-medium text-ink">This page does not exist</h1>
        <p className="mt-2 text-sm text-ink-dim">
          The link may be out of date, or the page may have moved.
        </p>

        <div className="mt-6">
          <Link
            href="/dashboard"
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-bg transition hover:bg-accent/90"
          >
            Go to dashboard
          </Link>
        </div>
      </div>
    </main>
  );
}
