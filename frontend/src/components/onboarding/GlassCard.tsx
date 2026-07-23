/**
 * GlassCard — reusable frosted-glass surface for the onboarding experience.
 *
 * Styling lives in the theme-aware `.onboarding-glass` helper (styles.css) which
 * is built from the app's own colour tokens, so it renders as a light frosted
 * panel in the light theme and a dark glass panel in the dark theme with no
 * hardcoded colours. Rounded 24px corners, soft border, premium shadow.
 */
import React from "react";

const GlassCard = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function GlassCard({ children, className = "", ...rest }, ref) {
    return (
      <div
        ref={ref}
        className={`onboarding-glass rounded-[24px] ${className}`}
        {...rest}
      >
        {children}
      </div>
    );
  }
);

export default GlassCard;
