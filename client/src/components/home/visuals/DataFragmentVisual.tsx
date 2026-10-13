import { useEffect, useState, useMemo } from "react";

interface DataFragmentVisualProps {
  sources: string[];
  isVisible: boolean;
}

// Foreground sources — sharp and prominent
const foregroundSources = ["SAP", "AMOS", "Skywise", "ERP", "APIs", "Excel"];

// Background fragment positions — many blurred fragments to fill space
const backgroundFragments = [
  { x: 5, y: 8 },
  { x: 22, y: 5 },
  { x: 38, y: 10 },
  { x: 62, y: 7 },
  { x: 78, y: 12 },
  { x: 95, y: 6 },
  { x: 8, y: 25 },
  { x: 28, y: 22 },
  { x: 72, y: 28 },
  { x: 92, y: 24 },
  { x: 3, y: 42 },
  { x: 18, y: 38 },
  { x: 35, y: 45 },
  { x: 65, y: 42 },
  { x: 82, y: 48 },
  { x: 97, y: 40 },
  { x: 6, y: 58 },
  { x: 25, y: 62 },
  { x: 45, y: 55 },
  { x: 55, y: 60 },
  { x: 75, y: 58 },
  { x: 94, y: 62 },
  { x: 4, y: 75 },
  { x: 20, y: 78 },
  { x: 40, y: 72 },
  { x: 60, y: 76 },
  { x: 80, y: 74 },
  { x: 96, y: 78 },
  { x: 10, y: 92 },
  { x: 30, y: 88 },
  { x: 48, y: 95 },
  { x: 68, y: 90 },
  { x: 88, y: 94 },
  { x: 32, y: 32 },
  { x: 68, y: 35 },
  { x: 35, y: 68 },
  { x: 65, y: 65 },
  { x: 50, y: 25 },
  { x: 50, y: 75 },
  { x: 25, y: 50 },
  { x: 75, y: 50 },
];

// Foreground fragment positions for main sources
const foregroundFragmentConfigs = [
  { x: 18, y: 28 },
  { x: 82, y: 32 },
  { x: 12, y: 58 },
  { x: 88, y: 62 },
  { x: 25, y: 78 },
  { x: 75, y: 75 },
];

// Broken connection attempts
const brokenConnections = [
  { from: { x: 12, y: 22 }, toward: { x: 55, y: 48 }, length: 0.32, delay: 0 },
  { from: { x: 88, y: 18 }, toward: { x: 45, y: 52 }, length: 0.35, delay: 0.5 },
  { from: { x: 8, y: 78 }, toward: { x: 52, y: 48 }, length: 0.3, delay: 1 },
  { from: { x: 90, y: 82 }, toward: { x: 48, y: 50 }, length: 0.33, delay: 1.5 },
  { from: { x: 15, y: 45 }, toward: { x: 85, y: 55 }, length: 0.28, delay: 2 },
  { from: { x: 85, y: 45 }, toward: { x: 15, y: 55 }, length: 0.26, delay: 2.5 },
  { from: { x: 50, y: 10 }, toward: { x: 50, y: 90 }, length: 0.22, delay: 3 },
  { from: { x: 50, y: 90 }, toward: { x: 50, y: 10 }, length: 0.2, delay: 3.5 },
  { from: { x: 25, y: 15 }, toward: { x: 75, y: 85 }, length: 0.25, delay: 4 },
  { from: { x: 75, y: 15 }, toward: { x: 25, y: 85 }, length: 0.28, delay: 4.5 },
  { from: { x: 30, y: 60 }, toward: { x: 70, y: 40 }, length: 0.22, delay: 5 },
  { from: { x: 70, y: 60 }, toward: { x: 30, y: 40 }, length: 0.24, delay: 5.5 },
  { from: { x: 20, y: 30 }, toward: { x: 60, y: 70 }, length: 0.2, delay: 6 },
  { from: { x: 80, y: 30 }, toward: { x: 40, y: 70 }, length: 0.22, delay: 6.5 },
  { from: { x: 35, y: 25 }, toward: { x: 65, y: 75 }, length: 0.18, delay: 7 },
  { from: { x: 65, y: 25 }, toward: { x: 35, y: 75 }, length: 0.2, delay: 7.5 },
];

export function DataFragmentVisual({
  sources,
  isVisible,
}: DataFragmentVisualProps) {
  const [isActive, setIsActive] = useState(false);

  const fgSources = sources.filter((s) => foregroundSources.includes(s));
  const bgSourcePool = sources.filter((s) => !foregroundSources.includes(s));

  const backgroundItems = useMemo(() => {
    return backgroundFragments.map((pos, idx) => ({
      ...pos,
      source:
        bgSourcePool[idx % bgSourcePool.length] || bgSourcePool[0] || "Data",
      delay: idx * 0.08,
      driftIdx: idx % 9,
    }));
  }, [bgSourcePool]);

  useEffect(() => {
    if (!isVisible) return;
    const timer = setTimeout(() => setIsActive(true), 200);
    return () => clearTimeout(timer);
  }, [isVisible]);

  return (
    <div
      className="relative w-full aspect-square max-w-lg mx-auto overflow-hidden"
      style={{
        maskImage:
          "radial-gradient(ellipse 70% 70% at 50% 50%, black 35%, transparent 85%)",
        WebkitMaskImage:
          "radial-gradient(ellipse 70% 70% at 50% 50%, black 35%, transparent 85%)",
      }}
    >
      {/* SVG broken connections */}
      <svg
        className="absolute inset-0 w-full h-full"
        style={{ zIndex: 2 }}
        viewBox="0 0 100 100"
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          {brokenConnections.map((conn, idx) => {
            const dx = conn.toward.x - conn.from.x;
            const dy = conn.toward.y - conn.from.y;
            const angle = Math.atan2(dy, dx);
            const endX = conn.from.x + Math.cos(angle) * 50 * conn.length;
            const endY = conn.from.y + Math.sin(angle) * 50 * conn.length;
            return (
              <linearGradient
                key={`grad-${idx}`}
                id={`fadeGrad-${idx}`}
                x1={conn.from.x}
                y1={conn.from.y}
                x2={endX}
                y2={endY}
                gradientUnits="userSpaceOnUse"
              >
                <stop
                  offset="0%"
                  stopColor="var(--color-accent-primary)"
                  stopOpacity="0.25"
                />
                <stop
                  offset="50%"
                  stopColor="var(--color-accent-primary)"
                  stopOpacity="0.08"
                />
                <stop
                  offset="100%"
                  stopColor="var(--color-accent-primary)"
                  stopOpacity="0"
                />
              </linearGradient>
            );
          })}
        </defs>

        {brokenConnections.map((conn, idx) => {
          const dx = conn.toward.x - conn.from.x;
          const dy = conn.toward.y - conn.from.y;
          const angle = Math.atan2(dy, dx);
          const endX = conn.from.x + Math.cos(angle) * 50 * conn.length;
          const endY = conn.from.y + Math.sin(angle) * 50 * conn.length;
          return (
            <line
              key={`broken-${idx}`}
              x1={conn.from.x}
              y1={conn.from.y}
              x2={endX}
              y2={endY}
              stroke={`url(#fadeGrad-${idx})`}
              strokeWidth="0.2"
              strokeDasharray="1,0.6"
              className={`transition-opacity duration-1000 ${isActive ? "opacity-100" : "opacity-0"}`}
              style={{
                transitionDelay: `${conn.delay * 150}ms`,
                animation: isActive
                  ? `lineSearch 12s ease-in-out infinite ${conn.delay * 0.5}s`
                  : "none",
              }}
            />
          );
        })}
      </svg>

      {/* Background fragments — blurred, subtle */}
      {backgroundItems.map((item, idx) => (
        <div
          key={`bg-${idx}`}
          className={`absolute flex items-center justify-center rounded-md border blur-[2px] w-14 h-6 text-[8px] transition-all duration-1000 ${
            isActive ? "" : "opacity-0 scale-75"
          }`}
          style={{
            left: `${item.x}%`,
            top: `${item.y}%`,
            transform: "translate(-50%, -50%) scale(0.8)",
            transitionDelay: `${item.delay * 150}ms`,
            borderColor: "var(--color-accent-primary)",
            borderWidth: "1px",
            background: "transparent",
            opacity: isActive ? 0.4 : 0,
            zIndex: 1,
            boxShadow: isActive
              ? "0 0 15px rgba(76, 128, 154, 0.02)"
              : "none",
            animation: isActive
              ? `fragmentDrift${item.driftIdx} 30s ease-in-out infinite ${item.delay}s, fragmentRotate${item.driftIdx} 25s ease-in-out infinite ${item.delay}s`
              : "none",
          }}
        >
          <span
            className="font-medium tracking-wide whitespace-nowrap"
            style={{ color: "var(--color-accent-primary)" }}
          >
            {item.source}
          </span>
        </div>
      ))}

      {/* Foreground fragments — sharp and prominent */}
      {fgSources.map((source, idx) => {
        const config =
          foregroundFragmentConfigs[idx] || foregroundFragmentConfigs[0];
        return (
          <div
            key={`fg-${source}`}
            className={`absolute flex items-center justify-center rounded-md border w-24 h-11 text-[11px] transition-all duration-1000 ${
              isActive ? "" : "opacity-0 scale-90"
            }`}
            style={{
              left: `${config.x}%`,
              top: `${config.y}%`,
              transform: "translate(-50%, -50%) scale(1)",
              transitionDelay: `${(idx + 1) * 400}ms`,
              borderColor: "var(--color-accent-primary)",
              borderWidth: "1px",
              background: "transparent",
              opacity: isActive ? 1 : 0,
              zIndex: 10,
              boxShadow: isActive
                ? "0 0 25px rgba(76, 128, 154, 0.15)"
                : "none",
              animation: isActive
                ? `fragmentDrift${idx} 28s ease-in-out infinite ${idx * 0.5}s, fragmentRotate${idx} 22s ease-in-out infinite ${idx * 0.5}s`
                : "none",
            }}
          >
            <span
              className="font-semibold tracking-wide whitespace-nowrap"
              style={{ color: "var(--color-accent-primary)" }}
            >
              {source}
            </span>
          </div>
        );
      })}
    </div>
  );
}
