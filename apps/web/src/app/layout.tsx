import type { Metadata } from "next";
import { Space_Grotesk, JetBrains_Mono } from "next/font/google";
import { ToastProvider } from "@/components/toast";
import "./globals.css";

const grotesk = Space_Grotesk({ subsets: ["latin"], variable: "--font-grotesk" });
const jbMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jb-mono" });

export const metadata: Metadata = {
  title: "trader",
  description: "Broker-agnostic algorithmic trading platform (India)",
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
