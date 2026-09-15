import type { Metadata } from "next";
import { Space_Grotesk, JetBrains_Mono } from "next/font/google";
import { ToastProvider } from "@/components/toast";
import "./globals.css";

const grotesk = Space_Grotesk({ subsets: ["latin"], variable: "--font-grotesk" });
const jbMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jb-mono" });

export const metadata: Metadata = {
  // The name is the choice the platform is built around: watch the feed, or
  // act on it. Paper and live are separate modes, and the interface never
  // lets them look alike.
  title: {
    default: "Tick or Trade",
    template: "%s · Tick or Trade",
  },
  description:
    "Algorithmic trading for Indian markets. Paper-first, with live trading " +
    "behind deliberate gates, honest costs, and an audit trail that cannot " +
    "be rewritten.",
  applicationName: "Tick or Trade",
  // A private instance: nothing here should be indexed.
  robots: { index: false, follow: false },
  openGraph: {
    title: "Tick or Trade",
    description:
      "Algorithmic trading for Indian markets — paper-first, honest costs, " +
      "and a live gate you have to open on purpose.",
    type: "website",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${grotesk.variable} ${jbMono.variable}`}>
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
