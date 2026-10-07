import { useEffect, useRef, useState } from 'react';

interface AnimatedCounterProps {
  value: number;
  duration?: number;
  format?: 'number' | 'currency' | 'percentage';
  prefix?: string;
  suffix?: string;
  decimals?: number;
  className?: string;
}

export function AnimatedCounter({
  value,
  duration = 2000,
  format = 'number',
  prefix = '',
  suffix = '',
  decimals = 0,
  className = '',
}: AnimatedCounterProps) {
  const [count, setCount] = useState(0);
  const [hasStarted, setHasStarted] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting && !hasStarted) setHasStarted(true);
        });
      },
      { threshold: 0.3, rootMargin: '-50px' },
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, [hasStarted]);

  useEffect(() => {
    if (!hasStarted) return;
    let startTime: number | null = null;
    let animationFrame: number;
    const animate = (timestamp: number) => {
      if (!startTime) startTime = timestamp;
      const progress = Math.min((timestamp - startTime) / duration, 1);
      const easeOut = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
      if (decimals > 0 || value < 100) {
        setCount(easeOut * value);
      } else {
        setCount(Math.floor(easeOut * value));
      }
      if (progress < 1) {
        animationFrame = requestAnimationFrame(animate);
      } else {
        setCount(value);
      }
    };
    animationFrame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animationFrame);
  }, [hasStarted, value, duration, decimals]);

  const formatNumber = (num: number): string => {
    if (format === 'currency') {
      return num.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
    }
    if (format === 'percentage') return num.toFixed(decimals);
    if (decimals > 0 || value < 100) return num.toFixed(decimals);
    return num.toLocaleString('en-US', { maximumFractionDigits: 0 });
  };

  return (
    <span ref={ref} className={`tabular-nums ${className}`} style={{ fontVariantNumeric: 'tabular-nums' }}>
      {prefix}{formatNumber(count)}{suffix}
    </span>
  );
}
