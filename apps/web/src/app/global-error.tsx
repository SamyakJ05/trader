"use client";

/**
 * The last resort: a failure in the root layout itself, where app/error.tsx
 * cannot render because the layout that would host it is the thing that
 * broke. This replaces the whole document, so it carries its own <html> and
 * cannot rely on globals.css having loaded — hence the inline styles.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          background: "#0b0e14",
          color: "#e6ebf2",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          margin: 0,
          padding: "0 1.5rem",
        }}
      >
        <div style={{ maxWidth: "28rem", textAlign: "center" }}>
          <p style={{ fontSize: "1.125rem", fontWeight: 600, letterSpacing: "-0.01em" }}>
            <span style={{ color: "#22d3ee" }}>tick</span>
            <span style={{ color: "#5c6575", margin: "0 0.25rem" }}>/</span>
            <span style={{ color: "#5c6575" }}>trade</span>
          </p>
          <h1 style={{ marginTop: "2rem", fontSize: "1.125rem", fontWeight: 500 }}>
            Something went wrong
          </h1>
          <p style={{ marginTop: "0.5rem", fontSize: "0.875rem", color: "#8b94a7" }}>
            Your orders, positions and strategies are unaffected. Reload to try again.
          </p>
          <button
            onClick={reset}
            style={{
              marginTop: "1.5rem",
              background: "#22d3ee",
              color: "#0b0e14",
              border: "none",
              borderRadius: "0.5rem",
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
          {error.digest && (
            <p style={{ marginTop: "2rem", fontSize: "0.75rem", color: "#5c6575" }}>
              Reference {error.digest}
            </p>
          )}
        </div>
      </body>
    </html>
  );
}
