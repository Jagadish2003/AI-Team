/**
 * OnboardingModal — the premium first-login product tour.
 *
 * A centered floating glassmorphism card layered over the already-loaded
 * dashboard (rendered behind it via AuthGuard's <Outlet/>). It does NOT navigate,
 * fetch, or touch auth — completing or skipping simply calls
 * `useOnboarding().dismiss()`, which persists the flag and unmounts the layer,
 * leaving the user on the dashboard that was already there.
 *
 * Accessibility:
 *   - role="dialog" aria-modal, labelled by the slide title.
 *   - ESC dismisses; ←/→ navigate; Enter advances (Go To Dashboard on the last).
 *   - Focus is moved into the card on open, trapped while open (Tab cycles), and
 *     restored to the previously focused element on close.
 *   - Scroll on <body> is locked while open.
 *
 * Performance: no animation library — CSS keyframes/transitions only, gated by
 * prefers-reduced-motion. The dashboard behind it is untouched, so opening the
 * tour adds no load cost beyond this small component.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";

import { useAuth } from "../../context/AuthContext";
import { useOnboarding } from "../../context/OnboardingContext";
import { profileNameFromEmail } from "../../utils/profileName";
import GlassCard from "./GlassCard";
import OnboardingSlide from "./OnboardingSlide";
import ProgressIndicator from "./ProgressIndicator";
import { SLIDES } from "./slides";

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';

export default function OnboardingModal() {
  const { isOpen, dismiss } = useOnboarding();
  const { user } = useAuth();
  const name = profileNameFromEmail(user?.email);

  const [step, setStep] = useState(0);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  const total = SLIDES.length;
  const isFirst = step === 0;
  const isLast = step === total - 1;
  const slide = SLIDES[step];

  const goNext = useCallback(() => {
    setStep((s) => Math.min(s + 1, total - 1));
  }, [total]);

  const goBack = useCallback(() => {
    setStep((s) => Math.max(s - 1, 0));
  }, []);

  const finish = useCallback(() => {
    dismiss();
  }, [dismiss]);

  // Reset to the first slide each time the tour opens (auto-show or replay), and
  // remember/restore focus around the open lifecycle.
  useEffect(() => {
    if (isOpen) {
      setStep(0);
      lastFocusedRef.current = document.activeElement as HTMLElement | null;
      // Move focus onto the primary action after the card mounts.
      const t = window.setTimeout(() => primaryRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
    // On close, restore focus to whatever was focused before opening.
    lastFocusedRef.current?.focus?.();
    return undefined;
  }, [isOpen]);

  // Lock body scroll while the tour is open.
  useEffect(() => {
    if (!isOpen) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [isOpen]);

  // Keyboard: ESC to dismiss, arrows to navigate, and a Tab focus trap.
  useEffect(() => {
    if (!isOpen) return undefined;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        finish();
        return;
      }
      if (e.key === "ArrowRight") {
        if (!isLast) {
          e.preventDefault();
          goNext();
        }
        return;
      }
      if (e.key === "ArrowLeft") {
        if (!isFirst) {
          e.preventDefault();
          goBack();
        }
        return;
      }
      if (e.key === "Tab" && cardRef.current) {
        const nodes = Array.from(
          cardRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)
        ).filter((el) => el.offsetParent !== null);
        if (nodes.length === 0) return;
        const firstEl = nodes[0];
        const lastEl = nodes[nodes.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey && active === firstEl) {
          e.preventDefault();
          lastEl.focus();
        } else if (!e.shiftKey && active === lastEl) {
          e.preventDefault();
          firstEl.focus();
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, isFirst, isLast, goNext, goBack, finish]);

  const primaryLabel = useMemo(() => {
    if (isFirst) return "Get Started";
    if (isLast) return "Go To Dashboard";
    return "Next";
  }, [isFirst, isLast]);

  if (!isOpen) return null;

  return (
    <div
      className="onboarding-scrim onboarding-anim-scrim fixed inset-0 z-[60] flex items-center justify-center p-4 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-title"
      data-testid="onboarding-overlay"
      onMouseDown={(e) => {
        // Clicking the scrim (outside the card) skips the tour.
        if (e.target === e.currentTarget) finish();
      }}
    >
      <GlassCard
        ref={cardRef}
        className="onboarding-anim-card relative w-full max-w-lg p-8 sm:p-10"
      >
        {/* Slide content — keyed by step so the entrance animation re-triggers.
            The Welcome slide (isFirst) is a pure introduction: no Skip, just the
            heading, description, and Get Started. */}
        <div className="pt-2" key={slide.id}>
          <OnboardingSlide slide={slide} name={name} />
        </div>

        {/* Progress */}
        <div className="mt-10">
          <ProgressIndicator total={total} current={step} onSelect={setStep} />
        </div>

        {/* Footer navigation.
            - Welcome slide:      [ Get Started ]
            - Middle slides:      [ Back ]            [ Skip ] [ Next ]
            - Final slide:        [ Back ]            [ Go To Dashboard ] */}
        <div className="mt-9 flex items-center justify-between gap-3">
          <div className="min-w-[84px]">
            {!isFirst && (
              <button
                type="button"
                onClick={goBack}
                data-testid="onboarding-back"
                className="inline-flex items-center gap-1.5 rounded-lg border border-border/70 bg-panel/30 px-4 py-2.5 text-sm font-medium text-muted transition-colors hover:bg-panel2/60 hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              >
                <ArrowLeft size={15} />
                Back
              </button>
            )}
          </div>

          <div className="flex items-center gap-3">
            {!isFirst && !isLast && (
              <button
                type="button"
                onClick={finish}
                data-testid="onboarding-skip"
                className="rounded-lg px-3.5 py-2.5 text-sm font-medium text-muted transition-colors hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                aria-label="Skip product tour"
              >
                Skip
              </button>
            )}
            <button
              ref={primaryRef}
              type="button"
              onClick={isLast ? finish : goNext}
              data-testid="onboarding-primary"
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent bg-accent px-5 py-2.5 text-sm font-semibold text-textwhite shadow-sm transition-[background-color,box-shadow,transform] hover:bg-accent/90 active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            >
              {primaryLabel}
              {!isLast && <ArrowRight size={15} />}
            </button>
          </div>
        </div>
      </GlassCard>
    </div>
  );
}
