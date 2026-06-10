import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "trader",
  description: "Broker-agnostic algorithmic trading platform (India)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
