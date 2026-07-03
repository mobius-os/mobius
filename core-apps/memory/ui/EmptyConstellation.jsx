// A small constellation drawn in SVG for the empty state — on-brand, not a
// generic spinner. Theme-aware via currentColor / theme vars.
export function EmptyConstellation() {
  return (
    <svg width="132" height="96" viewBox="0 0 132 96" fill="none" style={{ opacity: 0.92 }}>
      <g className="mg-constellation">
        <line x1="66" y1="48" x2="30" y2="26" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="104" y2="30" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="42" y2="74" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="96" y2="70" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="30" y1="26" x2="42" y2="74" stroke="var(--border)" strokeWidth="0.8" strokeDasharray="2 3" />
        <circle cx="30" cy="26" r="3.5" fill="var(--muted)" className="mg-star" style={{ animationDelay: '0.1s' }} />
        <circle cx="104" cy="30" r="3" fill="var(--muted)" className="mg-star" style={{ animationDelay: '0.6s' }} />
        <circle cx="42" cy="74" r="3" fill="var(--muted)" className="mg-star" style={{ animationDelay: '1.1s' }} />
        <circle cx="96" cy="70" r="2.6" fill="var(--muted)" className="mg-star" style={{ animationDelay: '1.5s' }} />
        <circle cx="66" cy="48" r="7" fill="var(--accent)" className="mg-star-hub" />
        <circle cx="66" cy="48" r="11" fill="none" stroke="var(--accent)" strokeWidth="1" opacity="0.35" className="mg-pulse" />
      </g>
    </svg>
  );
}
