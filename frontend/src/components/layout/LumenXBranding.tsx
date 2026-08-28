interface LumenXBrandingProps {
  size?: "sm" | "md";
  showSlogan?: boolean;
  markOnly?: boolean;
}

export default function LumenXBranding({ size = "md", showSlogan = true, markOnly = false }: LumenXBrandingProps) {
  const logoSize = size === "sm" ? "w-9 h-9" : "w-14 h-14";
  const titleSize = size === "sm" ? "text-base" : "text-xl";

  return (
    <div>
      <div className="flex gap-3 items-center">
        <div className="flex-shrink-0">
          <img
            src="/manyu-aigc-logo.svg"
            alt="漫屿AIGC"
            className={`${logoSize} object-contain`}
          />
        </div>
        {!markOnly && <div className="flex flex-col justify-center">
          <div className={`font-display ${titleSize} font-bold tracking-normal text-foreground whitespace-nowrap`}>
            漫屿<span className="font-mono text-primary">AIGC</span>
          </div>
          {showSlogan && size !== "sm" ? (
            <span className="mt-0.5 text-[0.6875rem] text-text-muted">让灵感生长为故事</span>
          ) : null}
        </div>}
      </div>
    </div>
  );
}
