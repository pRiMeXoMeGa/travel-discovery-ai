import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Travel Discovery AI",
  description: "AI-native travel discovery & booking",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
