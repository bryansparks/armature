export function DagAnimation({ size = 280 }: { size?: number }) {
  const h = Math.round(size * (148 / 220));
  const mono = "'IBM Plex Mono', monospace";

  return (
    <svg
      viewBox="-10 0 220 148"
      xmlns="http://www.w3.org/2000/svg"
      style={{ display: 'block', width: size, height: h, overflow: 'visible' }}
    >
      <defs>
        <marker id="arr" markerWidth="5" markerHeight="5" refX="4.5" refY="2.5" orient="auto">
          <path d="M0,0 L0,5 L5,2.5z" fill="#30363d" />
        </marker>
        <radialGradient id="glow-r" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(74,222,128,0.15)" />
          <stop offset="100%" stopColor="rgba(74,222,128,0)" />
        </radialGradient>
        <radialGradient id="glow-j" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(192,132,252,0.15)" />
          <stop offset="100%" stopColor="rgba(192,132,252,0)" />
        </radialGradient>
      </defs>

      {/* Ambient glows */}
      <ellipse cx="90" cy="18" rx="30" ry="20" fill="url(#glow-r)" />
      <ellipse cx="90" cy="114" rx="30" ry="20" fill="url(#glow-j)" />

      {/* Edges */}
      <line className="dag-edge e-0" x1="76" y1="30" x2="46" y2="57" markerEnd="url(#arr)" />
      <line className="dag-edge e-1" x1="104" y1="30" x2="134" y2="57" markerEnd="url(#arr)" />
      <line className="dag-edge e-2" x1="44" y1="78" x2="74" y2="103" markerEnd="url(#arr)" />
      <line className="dag-edge e-3" x1="136" y1="78" x2="106" y2="103" markerEnd="url(#arr)" />
      <line className="dag-edge e-4" x1="90" y1="124" x2="90" y2="135" markerEnd="url(#arr)" />

      {/* Nodes */}
      <circle className="dag-node n-scope" cx="90" cy="18" r="14" />
      <text style={{ font: `600 6px ${mono}`, fill: '#8b949e', pointerEvents: 'none', userSelect: 'none' }} x="90" y="21" textAnchor="middle">scope</text>
      <text style={{ font: `500 6px ${mono}`, fill: '#4ade80', opacity: .6, pointerEvents: 'none', userSelect: 'none' }} x="90" y="1" textAnchor="middle">researcher</text>

      <circle className="dag-node n-work-a" cx="35" cy="68" r="14" />
      <text style={{ font: `600 6px ${mono}`, fill: '#8b949e', pointerEvents: 'none', userSelect: 'none' }} x="35" y="71" textAnchor="middle">worker a</text>
      <text style={{ font: `500 6px ${mono}`, fill: '#60a5fa', opacity: .6, pointerEvents: 'none', userSelect: 'none' }} x="8" y="68" textAnchor="end">worker</text>

      <circle className="dag-node n-work-b" cx="145" cy="68" r="14" />
      <text style={{ font: `600 6px ${mono}`, fill: '#8b949e', pointerEvents: 'none', userSelect: 'none' }} x="145" y="71" textAnchor="middle">worker b</text>
      <text style={{ font: `500 6px ${mono}`, fill: '#60a5fa', opacity: .6, pointerEvents: 'none', userSelect: 'none' }} x="172" y="68" textAnchor="start">worker</text>

      <circle className="dag-node n-judge" cx="90" cy="114" r="14" />
      <text style={{ font: `600 6px ${mono}`, fill: '#8b949e', pointerEvents: 'none', userSelect: 'none' }} x="90" y="117" textAnchor="middle">judge</text>
      <text style={{ font: `500 6px ${mono}`, fill: '#c084fc', opacity: .6, pointerEvents: 'none', userSelect: 'none' }} x="108" y="117" textAnchor="start">judge</text>

      <circle className="dag-node n-output" cx="90" cy="143" r="7" />
      <text style={{ font: `600 5px ${mono}`, fill: '#8b949e', pointerEvents: 'none', userSelect: 'none' }} x="90" y="145.5" textAnchor="middle">output</text>
    </svg>
  );
}
