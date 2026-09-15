/**
 * The product mark.
 *
 * "Tick or Trade" is a choice — watch the feed, or act on it — and that choice
 * is the platform's central distinction: paper or live, separated by three
 * deliberate gates. So the mark shows which side you are on rather than
 * decorating the corner. In paper mode "tick" is lit; in live mode "trade"
 * burns amber, the colour reserved everywhere for real money.
 *
 * This is the one piece of state nobody should have to go looking for.
 */
export function Wordmark({
  live = false,
  size = "md",
}: {
  live?: boolean;
  size?: "sm" | "md" | "lg";
}) {
  const scale = { sm: "text-base", md: "text-lg", lg: "text-2xl" }[size];
  return (
    <span
      className={`${scale} font-semibold tracking-tight`}
      aria-label={live ? "Tick or Trade — live trading" : "Tick or Trade — paper trading"}
    >
      <span className={live ? "text-ink-faint" : "text-accent"}>tick</span>
      <span className="mx-1 text-ink-faint" aria-hidden>
        /
      </span>
      <span className={live ? "text-live" : "text-ink-faint"}>trade</span>
    </span>
  );
}
