import { useState } from "react";

interface BrandLogoProps {
  className?: string;
  compact?: boolean;
}

interface BrandIconProps {
  className?: string;
}

const LOGO_SOURCES = ["/brand/logo.png", "/brand/logo.svg"];
const ICON_SOURCES = ["/brand/icon.png", "/brand/icon.svg"];

export function BrandLogo({ className = "", compact = false }: BrandLogoProps) {
  const [logoIndex, setLogoIndex] = useState(0);
  const logoSrc = LOGO_SOURCES[logoIndex];
  const logoFailed = !logoSrc;

  if (logoFailed) {
    return (
      <span className={`inline-flex items-center gap-0 text-xl font-extrabold tracking-tight ${className}`}>
        <span className="text-brand-blue">Uni</span>
        <span className="text-brand-red">Graph</span>
      </span>
    );
  }

  return (
    <img
      src={logoSrc}
      alt="UniGraph"
      onError={() => setLogoIndex((prev) => prev + 1)}
      className={`h-8 w-auto ${compact ? "max-w-[120px]" : "max-w-[180px]"} object-contain ${className}`}
      loading="eager"
      decoding="async"
    />
  );
}

export function BrandIcon({ className = "" }: BrandIconProps) {
  const [iconIndex, setIconIndex] = useState(0);
  const iconSrc = ICON_SOURCES[iconIndex];
  const iconFailed = !iconSrc;

  if (iconFailed) {
    return (
      <span
        className={`inline-flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-blue to-brand-red text-xs font-bold text-white ${className}`}
        aria-label="UniGraph"
      >
        UG
      </span>
    );
  }

  return (
    <img
      src={iconSrc}
      alt="UniGraph icon"
      onError={() => setIconIndex((prev) => prev + 1)}
      className={`h-8 w-8 rounded-lg object-cover ${className}`}
      loading="eager"
      decoding="async"
    />
  );
}
