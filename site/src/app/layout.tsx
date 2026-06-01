import type { Metadata } from 'next';
import { Newsreader, DM_Sans, IBM_Plex_Mono } from 'next/font/google';
import './globals.css';

const newsreader = Newsreader({
  weight: ['400', '500', '600'],
  style: ['normal', 'italic'],
  subsets: ['latin'],
  variable: '--font-newsreader',
  adjustFontFallback: false,
});

const dmSans = DM_Sans({
  weight: ['400', '500', '600'],
  subsets: ['latin'],
  variable: '--font-dm-sans',
});

const ibmPlexMono = IBM_Plex_Mono({
  weight: ['400', '500', '700'],
  subsets: ['latin'],
  variable: '--font-ibm-plex-mono',
});

export const metadata: Metadata = {
  title: 'Armature — A Maturity of AI Agents',
  description: 'YAML-first multi-agent workflow harness. Define researcher, worker, and judge agents. Execute as a DAG. Self-improve with every run.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${newsreader.variable} ${dmSans.variable} ${ibmPlexMono.variable}`}>
        {children}
      </body>
    </html>
  );
}
