import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TEI Player",
  description: "A cinematic player for shared media links.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
