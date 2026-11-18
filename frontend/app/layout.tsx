import type { Metadata, Viewport } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "ClippingShorts",
  description: "Multi-stream AI clipping: live highlights to vertical Shorts.",
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { themeColor: "#0a0a0c", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
