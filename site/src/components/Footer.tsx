import { A } from './tokens';

export function Footer() {
  const mono = "'IBM Plex Mono', var(--font-ibm-plex-mono), monospace";
  const sans = "'DM Sans', var(--font-dm-sans), sans-serif";

  return (
    <footer style={{ backgroundColor: A.surface, borderTop: `1px solid ${A.border}`, padding: '48px 32px' }}>
      <div style={{
        maxWidth: 1060, margin: '0 auto',
        display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
        flexWrap: 'wrap', gap: 32,
      }}>
        <div>
          <div style={{ fontFamily: mono, fontSize: 14, fontWeight: 700, color: A.textBright, letterSpacing: '0.20em' }}>
            ARMATURE
          </div>
          <div style={{ fontFamily: mono, fontSize: 11, color: A.textMuted, marginTop: 6, lineHeight: 1.6 }}>
            A maturity of AI agents.<br />MIT License · Python 3.11+
          </div>
        </div>

        <div style={{ display: 'flex', gap: 48 }}>
          <div>
            <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 10 }}>Project</div>
            {[
              ['GitHub', 'https://github.com/bryansparks/armature'],
              ['User Guide', 'https://github.com/bryansparks/armature/blob/main/USER-GUIDE.md'],
              ['Tutorial', 'https://github.com/bryansparks/armature/blob/main/BUILD_FIRST_WORKFLOW.md'],
            ].map(([label, href]) => (
              <a key={label} href={href} style={{ display: 'block', fontSize: 13, fontFamily: sans, color: A.textSecondary, textDecoration: 'none', padding: '3px 0', transition: 'color 0.2s' }}>{label}</a>
            ))}
          </div>
          <div>
            <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 600, color: A.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 10 }}>ElfTech</div>
            {[
              ['elftech.com', 'https://elftech.com'],
              ['Tessera', 'https://tessera.elftech.com'],
              ['hello@elftech.com', 'mailto:hello@elftech.com'],
            ].map(([label, href]) => (
              <a key={label} href={href} style={{ display: 'block', fontSize: 13, fontFamily: sans, color: A.textSecondary, textDecoration: 'none', padding: '3px 0' }}>{label}</a>
            ))}
          </div>
        </div>

        <div style={{ fontFamily: mono, fontSize: 11, color: A.textMuted, alignSelf: 'flex-end' }}>
          © 2026 ElfTech, Inc.
        </div>
      </div>
    </footer>
  );
}
