'use client';
import { useState, useEffect } from 'react';
import { A } from './tokens';

function DagMark() {
  return (
    <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
      <rect width="28" height="28" rx="6" fill={A.surface} />
      <circle cx="14" cy="7" r="3.5" stroke={A.researcher} strokeWidth="1.4" />
      <circle cx="8" cy="19" r="3.5" stroke={A.worker} strokeWidth="1.4" />
      <circle cx="20" cy="19" r="3.5" stroke={A.worker} strokeWidth="1.4" />
      <line x1="13.1" y1="10.3" x2="8.9" y2="15.7" stroke={A.borderLight} strokeWidth="1" />
      <line x1="14.9" y1="10.3" x2="19.1" y2="15.7" stroke={A.borderLight} strokeWidth="1" />
    </svg>
  );
}

const links: [string, string][] = [
  ['How It Works', '#how-it-works'],
  ['The Roles', '#roles'],
  ['Self-Improvement', '#improve'],
  ['Research', '#research'],
  ['Open Source', '#open-source'],
];

export function ScrollNav() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 40);
    window.addEventListener('scroll', fn, { passive: true });
    return () => window.removeEventListener('scroll', fn);
  }, []);

  return (
    <nav style={{
      position: 'fixed', top: 0, left: 0, right: 0, zIndex: 100,
      backgroundColor: scrolled ? 'rgba(13,17,23,0.92)' : 'transparent',
      backdropFilter: scrolled ? 'blur(16px)' : 'none',
      borderBottom: scrolled ? `1px solid ${A.border}` : '1px solid transparent',
      transition: 'all 0.3s ease',
    }}>
      <div style={{
        maxWidth: 1060, margin: '0 auto', padding: '0 32px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 60,
      }}>
        <a href="/armature" style={{ display: 'flex', alignItems: 'center', gap: 10, textDecoration: 'none' }}>
          <DagMark />
          <span style={{
            fontFamily: "'IBM Plex Mono', var(--font-ibm-plex-mono), monospace",
            fontSize: 14, fontWeight: 700, color: A.textBright, letterSpacing: '0.20em',
          }}>ARMATURE</span>
        </a>
        <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
          {links.map(([label, href]) => (
            <a key={label} href={href} style={{
              fontSize: 13, color: A.textSecondary,
              fontFamily: "'DM Sans', var(--font-dm-sans), sans-serif",
              textDecoration: 'none', fontWeight: 500, transition: 'color 0.2s',
            }}
              onMouseEnter={e => (e.currentTarget.style.color = A.researcher)}
              onMouseLeave={e => (e.currentTarget.style.color = A.textSecondary)}
            >{label}</a>
          ))}
          <a
            href="https://github.com/bryansparks/armature"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              fontSize: 13, fontWeight: 600, color: A.bg, backgroundColor: A.researcher,
              padding: '8px 20px', borderRadius: 6, textDecoration: 'none',
              fontFamily: "'DM Sans', var(--font-dm-sans), sans-serif",
            }}
          >GitHub →</a>
        </div>
      </div>
    </nav>
  );
}
