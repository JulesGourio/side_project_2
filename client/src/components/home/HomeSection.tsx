import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  TrendingUp,
  Brain,
  Layers,
  Shield,
  Zap,
  Target,
  Globe,
  Lock,
  type LucideIcon,
} from "lucide-react";
import { HomeSectionData } from "@/lib/types";
import { AnimatedCounter } from "./AnimatedCounter";
import { AerospaceGlobeVisual } from "./visuals/AerospaceGlobeVisual";
import { StakesWaterfallVisual } from "./visuals/StakesWaterfallVisual";
import { DataFragmentVisual } from "./visuals/DataFragmentVisual";

// Icon mapping from string names to lucide components
const iconMap: Record<string, LucideIcon> = {
  TrendingUp,
  Brain,
  Layers,
  Shield,
  Zap,
  Target,
  Globe,
  Lock,
};

interface HomeSectionProps {
  section: HomeSectionData;
  index: number;
  nextSectionId?: string;
}

export function HomeSection({
  section,
  index: _index,
  nextSectionId,
}: HomeSectionProps) {
  const [isVisible, setIsVisible] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);

  const handleScrollToNext = () => {
    if (nextSectionId) {
      document
        .getElementById(nextSectionId)
        ?.scrollIntoView({ behavior: "smooth" });
    }
  };

  // Intersection Observer for entrance animations
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            setIsVisible(true);
          }
        });
      },
      { threshold: 0.15, rootMargin: "-50px" },
    );

    if (sectionRef.current) {
      observer.observe(sectionRef.current);
    }

    return () => observer.disconnect();
  }, []);

  // ── Cards layout ──────────────────────────────────────────────
  if (section.layout === "cards" && section.cards) {
    return (
      <section
        ref={sectionRef}
        id={section.id}
        className="relative min-h-screen flex flex-col px-6 md:px-12 lg:px-16"
      >
        <div className="flex-1 flex items-center py-16 md:py-24">
          <div className="max-w-7xl mx-auto w-full">
            {/* Centered header */}
            <div
              className={`text-center mb-16 transition-all duration-1000 ease-out ${
                isVisible
                  ? "opacity-100 translate-y-0"
                  : "opacity-0 -translate-y-8"
              }`}
            >
              <span className="text-xs font-semibold text-[var(--color-accent-primary)] uppercase tracking-widest">
                {section.subtitle}
              </span>
              <h2 className="mt-4 text-4xl md:text-5xl lg:text-6xl font-bold text-[var(--color-text-heading)] tracking-tight whitespace-pre-line">
                {section.title}
              </h2>
              {section.description && (
                <p className="mt-4 text-base md:text-lg text-[var(--color-text-muted)] max-w-2xl mx-auto leading-relaxed">
                  {section.description}
                </p>
              )}
            </div>

            {/* Card grid */}
            <div className={`grid grid-cols-1 sm:grid-cols-2 ${section.cards.length === 3 ? "lg:grid-cols-3" : "lg:grid-cols-4"} gap-5`}>
              {section.cards.map((card, idx) => {
                const IconComponent = iconMap[card.icon] ?? Zap;
                const isFirst = idx === 0;

                return (
                  <div
                    key={idx}
                    className={`relative rounded-xl p-6 flex flex-col gap-4 transition-all duration-700 ${
                      isVisible
                        ? "opacity-100 translate-y-0"
                        : "opacity-0 translate-y-8"
                    } ${
                      isFirst
                        ? "border border-[var(--color-text-muted)]/30 bg-[var(--color-text-heading)]/[0.03]"
                        : "border border-[var(--color-text-muted)]/10"
                    }`}
                    style={{ transitionDelay: `${300 + idx * 120}ms` }}
                  >
                    {/* Icon */}
                    <div className="text-[var(--color-text-muted)]">
                      <IconComponent className="h-5 w-5" strokeWidth={1.5} />
                    </div>

                    {/* Title */}
                    <h3 className="text-lg font-semibold text-[var(--color-text-heading)]">
                      {card.title}
                    </h3>

                    {/* Animated counter metric */}
                    {card.metric && card.metric.value !== null && (
                      <div>
                        <p className="text-3xl md:text-4xl font-bold text-[var(--color-accent-primary)] tracking-tight leading-none">
                          <AnimatedCounter
                            value={card.metric.value}
                            prefix={card.metric.prefix}
                            suffix={card.metric.suffix}
                            format={card.metric.format}
                            decimals={card.metric.decimals ?? 0}
                          />
                        </p>
                        {card.metric.label && (
                          <p className="mt-1 text-xs text-[var(--color-text-muted)] uppercase tracking-wide font-medium">
                            {card.metric.label}
                          </p>
                        )}
                      </div>
                    )}

                    {/* Optional static large value */}
                    {!card.metric && card.value && (
                      <div>
                        <p className="text-3xl md:text-4xl font-bold text-[var(--color-accent-primary)] tracking-tight leading-none">
                          {card.value}
                        </p>
                        {card.valueLabel && (
                          <p className="mt-1 text-xs text-[var(--color-text-muted)] uppercase tracking-wide font-medium">
                            {card.valueLabel}
                          </p>
                        )}
                      </div>
                    )}

                    {/* Description */}
                    {card.description && (
                      <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
                        {card.description}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Scroll to next arrow */}
        {nextSectionId && (
          <div className="pb-20 flex justify-center">
            <button
              onClick={handleScrollToNext}
              className={`text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] cursor-pointer group transition-opacity duration-500 ${
                isVisible ? "opacity-100" : "opacity-0"
              }`}
              aria-label="Scroll to next section"
            >
              <ChevronDown
                className="h-5 w-5 group-hover:translate-y-1 transition-transform"
                style={{ animation: "scrollBounce 2s ease-in-out infinite" }}
              />
            </button>
          </div>
        )}
      </section>
    );
  }

  // ── Left / Right layout (existing) ───────────────────────────

  // Render visual component based on type
  const renderVisual = () => {
    if (!section.visual) return null;
    switch (section.visual.type) {
      case "globe":
        return <AerospaceGlobeVisual isVisible={isVisible} />;
      case "waterfall":
        return <StakesWaterfallVisual isVisible={isVisible} />;
      case "fragmentation":
        return (
          <DataFragmentVisual
            sources={section.visual.sources || []}
            isVisible={isVisible}
          />
        );
      default:
        return null;
    }
  };

  const isGlobeVisual = section.visual?.type === "globe";

  // Content element
  const contentElement = (
    <div
      className={`space-y-6 transition-all duration-1000 ease-out ${
        isVisible
          ? "opacity-100 translate-x-0"
          : `opacity-0 ${section.layout === "left" ? "-translate-x-16" : "translate-x-16"}`
      }`}
      style={{ transitionDelay: "200ms" }}
    >
      {/* Badge */}
      <div className="inline-block">
        <span className="text-xs font-semibold text-[var(--color-accent-primary)] uppercase tracking-widest">
          {section.subtitle}
        </span>
      </div>

      {/* Title */}
      <h2 className="text-4xl md:text-5xl lg:text-6xl font-bold text-[var(--color-text-heading)] tracking-tight whitespace-pre-line">
        {section.title}
      </h2>

      {/* Statements */}
      {section.statements && section.statements.length > 0 && (
        <div className="space-y-3 pt-2">
          {section.statements.map((statement, idx) => (
            <p
              key={idx}
              className={`text-lg md:text-xl text-[var(--color-text-primary)] leading-relaxed transition-all duration-700 ${
                isVisible
                  ? "opacity-100 translate-y-0"
                  : "opacity-0 translate-y-4"
              } ${statement === "Until now." ? "font-semibold text-[var(--color-accent-primary)] pt-2" : ""}`}
              style={{ transitionDelay: `${400 + idx * 150}ms` }}
            >
              {statement}
            </p>
          ))}
        </div>
      )}

      {/* Metrics */}
      {section.metrics && section.metrics.length > 0 && (
        <div
          className={`pt-6 ${
            section.metrics.length === 1 || section.id === "scale"
              ? "space-y-8"
              : "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6"
          }`}
        >
          {section.metrics.map((metric, idx) => (
            <div
              key={idx}
              className={`transition-all duration-700 ${
                isVisible
                  ? "opacity-100 translate-y-0"
                  : "opacity-0 translate-y-6"
              }`}
              style={{ transitionDelay: `${600 + idx * 150}ms` }}
            >
              <div className="text-5xl md:text-6xl lg:text-7xl font-bold text-[var(--color-accent-primary)] tracking-tight">
                {metric.value !== null ? (
                  <AnimatedCounter
                    value={metric.value}
                    prefix={metric.prefix}
                    suffix={metric.suffix}
                    format={metric.format}
                    decimals={
                      metric.decimals ?? (metric.value < 100 ? 1 : 0)
                    }
                  />
                ) : (
                  <span>
                    {metric.prefix}
                    {metric.suffix}
                  </span>
                )}
              </div>
              <p className="mt-2 text-sm md:text-base text-[var(--color-text-muted)] font-medium uppercase tracking-wide">
                {metric.label}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  // Visual element
  const visualElement = (
    <div
      className={`transition-all duration-1000 ease-out ${
        isVisible
          ? "opacity-100 translate-x-0"
          : `opacity-0 ${section.layout === "left" ? "translate-x-16" : "-translate-x-16"}`
      } ${isGlobeVisual ? "flex items-center justify-center min-h-[450px] lg:min-h-[550px]" : ""}`}
      style={{ transitionDelay: "400ms" }}
    >
      {renderVisual()}
    </div>
  );

  return (
    <section
      ref={sectionRef}
      id={section.id}
      className="relative min-h-screen flex flex-col px-6 md:px-12 lg:px-16"
    >
      {/* Main content — centered vertically */}
      <div className="flex-1 flex items-center py-16 md:py-24">
        <div className="max-w-7xl mx-auto w-full">
          <div
            className={`grid grid-cols-1 lg:grid-cols-2 gap-12 lg:gap-16 items-center ${
              section.layout === "right" ? "lg:grid-flow-dense" : ""
            }`}
          >
            {section.layout === "left" ? (
              <>
                <div className="lg:pr-8">{contentElement}</div>
                <div className="lg:pl-8">{visualElement}</div>
              </>
            ) : (
              <>
                <div className="lg:col-start-2 lg:pl-8">{contentElement}</div>
                <div className="lg:col-start-1 lg:row-start-1 lg:pr-8">
                  {visualElement}
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Scroll to next arrow */}
      {nextSectionId && (
        <div className="pb-20 flex justify-center">
          <button
            onClick={handleScrollToNext}
            className={`text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] cursor-pointer group transition-opacity duration-500 ${
              isVisible ? "opacity-100" : "opacity-0"
            }`}
            aria-label="Scroll to next section"
          >
            <ChevronDown
              className="h-5 w-5 group-hover:translate-y-1 transition-transform"
              style={{ animation: "scrollBounce 2s ease-in-out infinite" }}
            />
          </button>
        </div>
      )}
    </section>
  );
}
