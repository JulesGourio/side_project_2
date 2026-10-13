// Home Page Content Types (driven by home-content.json)

export interface HomeMetric {
  value: number | null;
  label: string;
  prefix?: string;
  suffix?: string;
  format?: "number" | "currency" | "percentage";
  decimals?: number;
}

export interface HomeVisual {
  type: "globe" | "waterfall" | "fragmentation";
  sources?: string[];
}

export interface HomeCard {
  icon: string;
  title: string;
  value?: string;
  valueLabel?: string;
  description?: string;
  metric?: HomeMetric;
}

export interface HomeSectionData {
  id: string;
  subtitle: string;
  title: string;
  description?: string;
  layout: "left" | "right" | "cards";
  statements?: string[];
  metrics?: HomeMetric[];
  visual?: HomeVisual;
  cards?: HomeCard[];
}

export interface HomeHeroData {
  title: string;
  subtitle?: string;
}

export interface HomeCTAButton {
  label: string;
  action: string;
}

export interface HomeCTA {
  title: string;
  primaryButton: HomeCTAButton;
  secondaryLink?: HomeCTAButton;
}

export interface HomeContent {
  hero: HomeHeroData;
  sections: HomeSectionData[];
  cta: HomeCTA;
}
