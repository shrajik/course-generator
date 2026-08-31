import type { Metadata } from "next";
import "./globals.css";
import { CourseDraftProvider } from "@/lib/state/course-draft";

export const metadata: Metadata = {
  title: "AI Course Creator",
  description: "Create, generate and edit AI-authored courses.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Loaded via <link> rather than next/font so the Docker image can be
            built without network access to the font CDN. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <CourseDraftProvider>{children}</CourseDraftProvider>
      </body>
    </html>
  );
}
