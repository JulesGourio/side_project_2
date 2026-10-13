import { useState, useEffect } from "react";
import { SpatialNetworkBackground } from "@/components/background/SpatialNetworkBackground";
import { useThemeContext } from "@/contexts/ThemeContext";
import { ArrowRight } from "lucide-react";

export function AboutView() {
  const { colors, animatedBackground } = useThemeContext();
  const [visibleSections, setVisibleSections] = useState<Set<string>>(new Set());
  const [scrollY, setScrollY] = useState(0);

  useEffect(() => {
    const handleScroll = (e: Event) => {
      const target = e.target as HTMLElement;
      if (target) setScrollY(target.scrollTop);
    };
    const scrollContainer = document.querySelector(".about-scroll-container");
    if (scrollContainer) {
      scrollContainer.addEventListener("scroll", handleScroll);
      return () => scrollContainer.removeEventListener("scroll", handleScroll);
    }
  }, []);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) setVisibleSections((prev) => new Set(prev).add(entry.target.id));
        });
      },
      { threshold: 0.1, rootMargin: "0px 0px -100px 0px" },
    );
    document.querySelectorAll("[data-section]").forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, []);

  const isVisible = (id: string) => visibleSections.has(id);

  return (
    <div className="relative w-full overflow-hidden bg-[var(--color-background)] -mt-[var(--header-height)]" style={{ height: 'calc(100% + var(--header-height))' }}>

      {/* Content */}
      <div className="relative h-full overflow-y-auto scroll-smooth about-scroll-container">
        {/* Sticky Hero Background */}
        <div className="sticky top-0 w-full h-screen overflow-hidden bg-[var(--color-background)] z-0">
          {/* Three.js Spatial Network — visible when video is absent */}
          <SpatialNetworkBackground
            particleCount={animatedBackground.particleCount}
            connectionDistance={animatedBackground.connectionDistance}
            primaryColor={colors.animatedBgColor}
            secondaryColor={colors.animatedBgColor}
            particleOpacity={animatedBackground.particleOpacity}
            lineOpacity={animatedBackground.lineOpacity}
            particleSize={animatedBackground.particleSize}
            lineWidth={animatedBackground.lineWidth}
            animationSpeed={animatedBackground.animationSpeed}
          />
          <video
            className="w-full h-full object-cover transition-all duration-300"
            style={{
              filter: `blur(${Math.min(scrollY / 10, 30)}px) brightness(0.9)`,
              transform: `scale(${1 + scrollY / 2000})`,
            }}
            autoPlay
            loop
            muted
            playsInline
          >
            <source src="/videos/about_video.mp4" type="video/mp4" />
          </video>

          {/* Hero text */}
          <div
            className="absolute top-24 md:top-32 right-8 md:right-16 max-w-xl transition-all duration-300 ease-out animate-float-in-right"
            style={{
              opacity: Math.max(1 - scrollY / 120, 0),
              transform: `translateY(${scrollY / 1.5}px) scale(${Math.max(1 - scrollY / 400, 0.95)})`,
              visibility: scrollY > 150 ? 'hidden' : 'visible',
            }}
          >
            <div className="px-6 py-4 bg-[var(--color-background)] rounded-xl shadow-2xl" style={{ opacity: 0.9 }}>
              <h1 className="text-3xl md:text-5xl font-bold text-[var(--color-text-heading)] mb-3 leading-tight">
                Document Comparison
              </h1>
              <p className="text-base md:text-lg text-[var(--color-text-primary)] leading-relaxed">
                Detect changes between document versions and keep your knowledge base up to date — automatically
              </p>
            </div>
          </div>
        </div>

        {/* Scrolling content panel */}
        <div className="relative z-10">
          <div
            className="max-w-7xl mx-auto px-6 md:px-8 py-16 md:py-24 bg-[var(--color-background)] rounded-t-3xl shadow-2xl transition-transform duration-200 ease-out"
            style={{
              transform: `translateY(${Math.max(-scrollY / 8, -60)}px)`,
              opacity: Math.min(0.95, scrollY / 100 * 0.95),
            }}
          >
            <div className="space-y-32">

              {/* Section 1: Architecture */}
              <div
                id="foundations"
                data-section
                className={`grid md:grid-cols-2 gap-12 md:gap-16 items-center transition-all duration-1000 ${
                  isVisible('foundations') ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-12'
                }`}
              >
                <div className="space-y-6">
                  <div className="inline-block px-3 py-1 bg-[var(--color-accent-primary)]/10 rounded-full">
                    <span className="text-xs font-semibold text-[var(--color-text-primary)] uppercase tracking-wide">How it works</span>
                  </div>
                  <h2 className="text-3xl md:text-4xl font-bold text-[var(--color-text-heading)] leading-tight">How it works</h2>
                  <ul className="space-y-3 mt-6">
                    {['Upload two versions of any PDF document', 'Claude AI analyses every change in real time', 'Your knowledge assistant identifies impacted documents', 'Results and PDFs are saved to a Unity Catalog Volume'].map((item, i) => (
                      <li key={i} className="flex items-start gap-3 text-[var(--color-text-primary)]">
                        <div className="flex-shrink-0 w-1.5 h-1.5 rounded-full bg-[var(--color-accent-primary)] mt-2" />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="relative aspect-[4/3] rounded-2xl overflow-hidden shadow-2xl hover:shadow-3xl transition-all duration-500 hover:scale-105">
                  <img src="/images/latecoere_aerostructures.jpg" alt="Latecoere Aerostructures" className="w-full h-full object-cover" />
                </div>
              </div>

              {/* Section 2: Revision Impact */}
              <div
                id="analytics"
                data-section
                className={`grid md:grid-cols-2 gap-12 md:gap-16 items-center transition-all duration-1000 md:grid-flow-dense ${
                  isVisible('analytics') ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-12'
                }`}
              >
                <div className="md:col-start-2 space-y-6">
                  <div className="inline-block px-3 py-1 bg-[var(--color-accent-primary)]/10 rounded-full">
                    <span className="text-xs font-semibold text-[var(--color-text-primary)] uppercase tracking-wide">Impact Search</span>
                  </div>
                  <h2 className="text-3xl md:text-4xl font-bold text-[var(--color-text-heading)] leading-tight">Know exactly what needs updating</h2>
                  <p className="text-lg text-[var(--color-text-primary)] leading-relaxed">
                    After detecting changes, the app queries your knowledge assistant to find every document in your knowledge base that references the revised content — with an explanation of the gap and what needs to change.
                  </p>
                </div>
                <div className="md:col-start-1 md:row-start-1 relative aspect-[4/3] rounded-2xl overflow-hidden shadow-2xl hover:shadow-3xl transition-all duration-500 hover:scale-105">
                  <img src="/images/latecoere_satellite.png" alt="Latecoere Interconnection Systems" className="w-full h-full object-cover" />
                </div>
              </div>

              {/* Section 3: Knowledge Assistant */}
              <div
                id="innovation"
                data-section
                className={`grid md:grid-cols-2 gap-12 md:gap-16 items-center transition-all duration-1000 ${
                  isVisible('innovation') ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-12'
                }`}
              >
                <div className="space-y-6">
                  <div className="inline-block px-3 py-1 bg-[var(--color-accent-primary)]/10 rounded-full">
                    <span className="text-xs font-semibold text-[var(--color-text-primary)] uppercase tracking-wide">Built on Databricks</span>
                  </div>
                  <h2 className="text-3xl md:text-4xl font-bold text-[var(--color-text-heading)] leading-tight">Secure, governed, and ready for production</h2>
                  <p className="text-lg text-[var(--color-text-primary)] leading-relaxed">
                    The app runs as a Databricks App, using the service principal for authentication and Unity Catalog Volumes for storage. All files and analysis results are saved with full data lineage — no data leaves your workspace.
                  </p>
                </div>
                <div className="relative aspect-[4/3] rounded-2xl overflow-hidden shadow-2xl hover:shadow-3xl transition-all duration-500 hover:scale-105">
                  <img src="/images/latecoere_innovation.jpg" alt="Latecoere Innovation" className="w-full h-full object-cover" />
                </div>
              </div>

            </div>

            {/* CTA Section */}
            <div className="mt-32 text-center">
              <div className="max-w-3xl mx-auto p-12 md:p-16 bg-[#0C1C3E] rounded-3xl shadow-xl">
                <h2 className="text-3xl md:text-4xl font-bold text-white mb-4">
                  Start your first comparison
                </h2>
                <p className="text-lg text-white/90 mb-8">
                  Upload two PDF versions and get a full change analysis in under 30 seconds.
                </p>
                <a
                  href="/compare"
                  className="inline-flex items-center gap-2 px-8 py-4 bg-white text-[#0C1C3E] font-semibold rounded-xl hover:shadow-2xl hover:scale-105 transition-all duration-300 group"
                >
                  Start Comparing
                  <ArrowRight className="h-5 w-5 group-hover:translate-x-1 transition-transform" />
                </a>
              </div>
            </div>

            {/* Bottom Spacing */}
            <div className="h-16" />
          </div>
        </div>
      </div>

      <style>{`
        @keyframes fade-in {
          from { opacity: 0; transform: translateY(-20px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes float-in-right {
          from { opacity: 0; transform: translateX(100px); }
          to { opacity: 1; transform: translateX(0); }
        }
        .animate-fade-in { animation: fade-in 1s ease-out; }
        .animate-float-in-right {
          animation: float-in-right 2s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
        }
      `}</style>
    </div>
  );
}
