import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";

interface HomeHeroProps {
  title: string;
  subtitle?: string;
  onScrollDown?: () => void;
}

// Floating particles configuration
const particles = [
  { size: 3, x: 15, y: 20, delay: 0, duration: 8 },
  { size: 2, x: 80, y: 15, delay: 1, duration: 10 },
  { size: 4, x: 25, y: 70, delay: 2, duration: 9 },
  { size: 2, x: 70, y: 75, delay: 0.5, duration: 11 },
  { size: 3, x: 10, y: 50, delay: 1.5, duration: 8 },
  { size: 2, x: 85, y: 45, delay: 2.5, duration: 10 },
  { size: 3, x: 40, y: 85, delay: 0.8, duration: 9 },
  { size: 2, x: 60, y: 10, delay: 1.8, duration: 11 },
  { size: 4, x: 5, y: 35, delay: 3, duration: 8 },
  { size: 2, x: 92, y: 60, delay: 0.3, duration: 10 },
  { size: 3, x: 50, y: 5, delay: 2.2, duration: 9 },
  { size: 2, x: 35, y: 90, delay: 1.2, duration: 11 },
];

export function HomeHero({ title, subtitle, onScrollDown }: HomeHeroProps) {
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    // Trigger fade-in animation after mount
    const timer = setTimeout(() => setIsVisible(true), 500);
    return () => clearTimeout(timer);
  }, []);

  return (
    <section className="relative min-h-screen flex flex-col items-center justify-center px-6 overflow-hidden">
      {/* Layer 1: Gradient orb */}
      <div className="absolute inset-0 pointer-events-none overflow-hidden">
        <div
          className="absolute rounded-full blur-3xl"
          style={{
            width: "70vw",
            height: "50vh",
            maxWidth: "900px",
            left: "50%",
            top: "45%",
            transform: "translate(-50%, -50%)",
            background:
              "radial-gradient(ellipse at center, var(--color-accent-primary) 0%, transparent 70%)",
            opacity: 0.1,
            animation: "galaxyPulse 10s ease-in-out infinite",
          }}
        />
      </div>

      {/* Layer 2: Floating particles */}
      <div className="absolute inset-0 pointer-events-none overflow-hidden">
        {particles.map((particle, idx) => (
          <div
            key={idx}
            className="absolute rounded-full bg-[var(--color-accent-primary)]"
            style={{
              width: `${particle.size}px`,
              height: `${particle.size}px`,
              left: `${particle.x}%`,
              top: `${particle.y}%`,
              opacity: 0.3,
              animationName: "particleDrift, particleTwinkle",
              animationDuration: `${particle.duration}s, ${particle.duration * 0.7}s`,
              animationTimingFunction: "ease-in-out, ease-in-out",
              animationIterationCount: "infinite, infinite",
              animationDelay: `${particle.delay}s, ${particle.delay + 0.5}s`,
            }}
          />
        ))}
      </div>

      {/* Layer 3: Subtle center glow behind text */}
      <div
        className="absolute pointer-events-none"
        style={{
          width: "60vw",
          height: "40vh",
          maxWidth: "800px",
          left: "50%",
          top: "50%",
          transform: "translate(-50%, -50%)",
          background:
            "radial-gradient(ellipse at center, var(--color-accent-primary) 0%, transparent 60%)",
          opacity: 0.04,
          filter: "blur(60px)",
          animationName: "galaxyPulse",
          animationDuration: "6s",
          animationTimingFunction: "ease-in-out",
          animationIterationCount: "infinite",
        }}
      />

      {/* Content */}
      <div
        className={`relative z-10 text-center max-w-4xl mx-auto ${
          isVisible ? "opacity-100" : "opacity-0"
        }`}
        style={{
          transform: isVisible ? "translateY(0)" : "translateY(60px)",
          transition:
            "opacity 3.5s cubic-bezier(0.25, 0.1, 0.25, 1), transform 5s cubic-bezier(0.16, 1, 0.3, 1)",
        }}
      >
        <h1 className="text-4xl md:text-5xl lg:text-6xl xl:text-7xl font-bold text-[var(--color-text-heading)] leading-tight tracking-tight whitespace-pre-line">
          {title}
        </h1>

        {subtitle && (
          <p
            className={`mt-6 text-lg md:text-xl text-[var(--color-text-muted)] max-w-2xl mx-auto ${
              isVisible
                ? "opacity-100 translate-y-0"
                : "opacity-0 translate-y-4"
            }`}
            style={{
              transition:
                "opacity 1.5s cubic-bezier(0.4, 0, 0.2, 1) 0.7s, transform 1.5s cubic-bezier(0.4, 0, 0.2, 1) 0.7s",
            }}
          >
            {subtitle}
          </p>
        )}

        {/* Thin accent line */}
        <div
          className={`mt-10 mx-auto ${isVisible ? "opacity-100" : "opacity-0"}`}
          style={{
            width: "35%",
            height: "0.8px",
            background:
              "linear-gradient(90deg, transparent 0%, var(--color-accent-primary) 20%, var(--color-accent-primary) 80%, transparent 100%)",
            opacity: isVisible ? 0.35 : 0,
            boxShadow: "0 0 4px 0.5px var(--color-accent-primary)",
            transition: "opacity 1.5s cubic-bezier(0.4, 0, 0.2, 1) 1s",
          }}
        />
      </div>

      {/* Scroll indicator */}
      <button
        onClick={onScrollDown}
        className={`absolute bottom-12 left-1/2 -translate-x-1/2 flex flex-col items-center gap-2 text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] cursor-pointer group ${
          isVisible ? "opacity-100" : "opacity-0"
        }`}
        style={{
          transition: "opacity 0.5s ease-out 2.5s, color 0.3s ease",
        }}
        aria-label="Scroll to explore"
      >
        <span className="text-xs font-medium uppercase tracking-widest">
          Scroll to explore
        </span>
        <ChevronDown
          className="h-5 w-5 group-hover:translate-y-1 transition-transform"
          style={{ animation: "scrollBounce 2s ease-in-out infinite" }}
        />
      </button>
    </section>
  );
}
