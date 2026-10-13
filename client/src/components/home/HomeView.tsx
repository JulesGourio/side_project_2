import { useState, useEffect, useRef } from "react";
import { HomeContent } from "@/lib/types";
import { HomeHero } from "./HomeHero";
import { HomeSection } from "./HomeSection";
import { HomeBridge } from "./HomeBridge";

export function HomeView() {
  const [content, setContent] = useState<HomeContent | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/content/home-content.json")
      .then((res) => res.json())
      .then((data) => { setContent(data); setIsLoading(false); })
      .catch((err) => { console.error("Failed to load home content:", err); setIsLoading(false); });
  }, []);

  const handleScrollDown = () => {
    if (scrollContainerRef.current && content?.sections?.[0]) {
      const firstSection = scrollContainerRef.current.querySelector(`#${content.sections[0].id}`);
      if (firstSection) firstSection.scrollIntoView({ behavior: "smooth" });
    }
  };

  if (isLoading) {
    return (
      <div className="h-full w-full flex items-center justify-center">
        <div className="text-[var(--color-text-muted)] animate-pulse">Loading...</div>
      </div>
    );
  }

  if (!content) {
    return (
      <div className="h-full w-full flex items-center justify-center">
        <div className="text-[var(--color-text-muted)]">Failed to load content</div>
      </div>
    );
  }

  return (
    <div ref={scrollContainerRef} className="h-full w-full overflow-y-auto overflow-x-hidden">
      <HomeHero title={content.hero.title} subtitle={content.hero.subtitle} onScrollDown={handleScrollDown} />
      {content.sections.map((section, index) => {
        const nextSection = content.sections[index + 1];
        const nextSectionId = nextSection?.id ?? "cta-bridge";
        return (
          <HomeSection key={section.id} section={section} index={index} nextSectionId={nextSectionId} />
        );
      })}
      <HomeBridge title={content.cta.title} primaryButton={content.cta.primaryButton} secondaryLink={content.cta.secondaryLink} />
    </div>
  );
}
