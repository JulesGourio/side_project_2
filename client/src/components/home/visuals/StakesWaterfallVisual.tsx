import { useEffect, useState } from "react";
import { AnimatedCounter } from "../AnimatedCounter";

interface StakesWaterfallVisualProps {
  isVisible: boolean;
}

const waterfallData = [
  { label: "Planned Margin", value: 55, type: "start" },
  { label: "AOG Penalties", value: -8.5, type: "loss" },
  { label: "Extra Repair Costs", value: -7.5, type: "loss" },
  { label: "Actual Margin", value: 39, type: "end" },
];

export function StakesWaterfallVisual({
  isVisible,
}: StakesWaterfallVisualProps) {
  const [animationStep, setAnimationStep] = useState(0);
  const [showSummary, setShowSummary] = useState(false);

  useEffect(() => {
    if (!isVisible) return;

    const stepInterval = setInterval(() => {
      setAnimationStep((prev) => {
        if (prev >= waterfallData.length) {
          clearInterval(stepInterval);
          return prev;
        }
        return prev + 1;
      });
    }, 800);

    const summaryTimer = setTimeout(() => setShowSummary(true), 4000);

    return () => {
      clearInterval(stepInterval);
      clearTimeout(summaryTimer);
    };
  }, [isVisible]);

  const maxValue = 55;
  const getWidth = (value: number) => (Math.abs(value) / maxValue) * 100;

  return (
    <div className="w-full max-w-xl mx-auto space-y-8">
      {waterfallData.map((item, idx) => {
        const isActive = animationStep > idx;
        const width = getWidth(Math.abs(item.value));
        const isLoss = item.type === "loss";
        const isEnd = item.type === "end";

        return (
          <div
            key={item.label}
            className={`transition-all duration-1000 ease-out ${
              isActive
                ? "opacity-100 translate-x-0"
                : "opacity-0 -translate-x-12"
            }`}
            style={{ transitionDelay: `${idx * 200}ms` }}
          >
            {/* Label and value row */}
            <div className="flex justify-between items-baseline mb-3">
              <span className="text-xs uppercase tracking-widest text-[var(--color-text-muted)]">
                {item.label}
              </span>
              <span
                className={`tabular-nums transition-all duration-700 ${
                  isEnd
                    ? "text-2xl font-semibold text-[var(--color-accent-primary)]"
                    : isLoss
                      ? "text-xl font-light text-[var(--color-text-primary)]/60"
                      : "text-2xl font-semibold text-[var(--color-accent-primary)]"
                }`}
              >
                {isLoss ? "-" : ""}
                {isActive ? (
                  <AnimatedCounter
                    value={Math.abs(item.value)}
                    prefix="$"
                    suffix="M"
                    decimals={1}
                    duration={1200}
                  />
                ) : (
                  "$0.0M"
                )}
              </span>
            </div>

            {/* Bar */}
            <div className="relative h-1.5 bg-[var(--color-border)]/10 rounded-full overflow-hidden">
              <div
                className={`absolute top-0 left-0 h-full rounded-full transition-all duration-1000 ease-out ${
                  isEnd ? "shadow-[0_0_12px_rgba(76,128,154,0.4)]" : ""
                }`}
                style={{
                  backgroundColor: "var(--color-accent-primary)",
                  width: isActive ? `${width}%` : "0%",
                  opacity: isLoss ? 0.35 : 1,
                }}
              />
            </div>

            {/* Connector */}
            {idx < waterfallData.length - 1 && (
              <div
                className={`flex justify-center py-4 transition-opacity duration-700 ${
                  isActive ? "opacity-20" : "opacity-0"
                }`}
                style={{ transitionDelay: `${idx * 200 + 400}ms` }}
              >
                <div className="w-px h-4 bg-[var(--color-accent-primary)]" />
              </div>
            )}
          </div>
        );
      })}

      {/* Summary */}
      <div
        className={`pt-6 border-t border-[var(--color-border)]/20 transition-all duration-1000 ease-out ${
          showSummary
            ? "opacity-100 translate-y-0"
            : "opacity-0 translate-y-4"
        }`}
      >
        <div className="flex items-baseline justify-between">
          <span className="text-xs uppercase tracking-widest text-[var(--color-text-muted)]">
            Margin Erosion
          </span>
          <span className="text-3xl font-light tabular-nums text-[var(--color-text-primary)]">
            {showSummary ? (
              <>
                -
                <AnimatedCounter
                  value={16}
                  prefix="$"
                  suffix="M"
                  decimals={0}
                  duration={1500}
                />
              </>
            ) : (
              "-$0M"
            )}
          </span>
        </div>
        <div
          className={`mt-2 text-right text-xs text-[var(--color-text-muted)]/60 transition-opacity duration-700 ${
            showSummary ? "opacity-100" : "opacity-0"
          }`}
          style={{ transitionDelay: "500ms" }}
        >
          29% below target
        </div>
      </div>
    </div>
  );
}
