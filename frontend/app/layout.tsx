import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bright Smile Scheduler",
  description: "Appointment scheduling agent with human approval (fictional dental office).",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
