import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FocusCard } from "../components/stack_builder";
import type {
  FocusCard as FocusCardType,
  FocusId,
} from "../types/stack_builder";

const cards: FocusCardType[] = [
  {
    id: "member_customer_service",
    title: "Member and Customer Service",
    subtext: "Front-line service, cases, requests, and follow-up work.",
    useWhen: "customer-facing follow-up is the main pain.",
    notWhen: "approval gates or internal queues are the blocker.",
    icon: "ti-users",
  },
  {
    id: "core_operations",
    title: "Core Operations",
    subtext: "Operational queues, processing work, and exception handling.",
    icon: "ti-settings",
  },
  {
    id: "approvals_compliance",
    title: "Approvals and Compliance",
    subtext: "Review cycles, compliance checks, approvals, and controls.",
    icon: "ti-shield-check",
  },
  {
    id: "cross_system_handoffs",
    title: "Cross-System Handoffs",
    subtext: "Transfers of work between tools, teams, and systems.",
    icon: "ti-arrows-exchange",
  },
  {
    id: "back_office_productivity",
    title: "Back-Office Productivity",
    subtext: "Manual work, backlog friction, and productivity bottlenecks.",
    icon: "ti-briefcase",
  },
  {
    id: "engineering_change",
    title: "Engineering and Change",
    subtext: "Release flow, change work, delivery queues, and dependencies.",
    icon: "ti-code",
  },
  {
    id: "enterprise_wide",
    title: "Enterprise-Wide",
    subtext: "A broader scan across teams, workflows, and source systems.",
    icon: "ti-building",
    wide: true,
  },
];

function renderFocusCard(
  card: FocusCardType = cards[0],
  options: { selected?: boolean; tabIndex?: number; onSelect?: (id: FocusId) => void } = {},
) {
  const onSelect = options.onSelect ?? vi.fn<(id: FocusId) => void>();

  const { container } = render(
    <FocusCard
      card={card}
      selected={options.selected ?? false}
      onSelect={onSelect}
      tabIndex={options.tabIndex}
    />,
  );

  return {
    onSelect,
    radio: within(container).getByRole("radio"),
  };
}

describe("SB-3 v1.1 FocusCard acceptance criteria", () => {
  it("renders the selected state with the theme accent styling", () => {
    const { radio } = renderFocusCard(cards[0], { selected: true });

    expect(radio).toHaveAttribute("aria-checked", "true");
    expect(radio).toHaveAttribute("tabindex", "0");
    expect(radio).toHaveClass(
      "flex",
      "h-full",
      "items-start",
      "rounded-lg",
      "border-accent/60",
      "bg-accent/15",
      "focus-visible:ring-accent/35",
    );
    expect(radio.className).not.toContain("bg-panel2");

    expect(radio.querySelector("i")).toBeNull();
    expect(screen.getByText(cards[0].title)).toHaveClass("text-accent");
    expect(screen.getByText(cards[0].subtext)).toHaveClass("text-accent");
  });

  it("renders the default and hover state with neutral panel styling", () => {
    const { radio } = renderFocusCard(cards[1], { selected: false });

    expect(radio).toHaveAttribute("aria-checked", "false");
    expect(radio).toHaveClass(
      "border-border",
      "bg-panel",
      "hover:border-accent/50",
      "hover:bg-accent/5",
      "transition-colors",
      "duration-150",
    );

    expect(radio.querySelector("i")).toBeNull();
    expect(screen.getByText(cards[1].title)).toHaveClass("text-text");
    expect(screen.getByText(cards[1].subtext)).toHaveClass("text-muted");
  });

  it("renders boundary copy when the focus card provides it", () => {
    renderFocusCard(cards[0]);

    expect(screen.getByText("Use when:")).toBeInTheDocument();
    expect(screen.getByText("Not when:")).toBeInTheDocument();
    expect(screen.getByText(/customer-facing follow-up is the main pain/i)).toBeInTheDocument();
    expect(screen.getByText(/approval gates or internal queues are the blocker/i)).toBeInTheDocument();
  });

  it("keeps standard and wide cards on the same top-aligned layout", () => {
    const { radio: standard } = renderFocusCard(cards[2]);

    expect(standard).not.toHaveClass("col-span-2");
    expect(standard).toHaveClass("flex", "items-start", "justify-start");
    expect(standard.firstElementChild).toHaveClass("w-full");
    expect(standard.querySelector("i")).toBeNull();

    const { radio: wide } = renderFocusCard(cards[6]);

    expect(wide).toHaveClass("col-span-2");
    expect(wide).toHaveClass("flex", "items-start", "justify-start");
    expect(wide.firstElementChild).toHaveClass("w-full");
    expect(wide.querySelector("i")).toBeNull();
  });

  it("supports the tabIndex prop while preserving the default of 0", () => {
    const { radio: defaultTabIndex } = renderFocusCard(cards[3]);
    expect(defaultTabIndex).toHaveAttribute("tabindex", "0");

    const { radio: rovingTabIndex } = renderFocusCard(cards[4], { tabIndex: -1 });
    expect(rovingTabIndex).toHaveAttribute("tabindex", "-1");
  });

  it("calls onSelect with FocusId for click, Enter, and Space", () => {
    const onSelect = vi.fn<(id: FocusId) => void>();
    const { radio } = renderFocusCard(cards[5], { onSelect });

    fireEvent.click(radio);
    fireEvent.keyDown(radio, { key: "Enter" });
    fireEvent.keyDown(radio, { key: " " });

    expect(onSelect).toHaveBeenCalledTimes(3);
    expect(onSelect).toHaveBeenNthCalledWith(1, cards[5].id);
    expect(onSelect).toHaveBeenNthCalledWith(2, cards[5].id);
    expect(onSelect).toHaveBeenNthCalledWith(3, cards[5].id);
  });

  it("renders seven cards in the Screen 1 two-column radiogroup shape", () => {
    const onSelect = vi.fn<(id: FocusId) => void>();

    render(
      <div role="radiogroup" aria-label="Discovery focus" className="grid grid-cols-2 gap-3">
        {cards.map((card) => (
          <FocusCard
            key={card.id}
            card={card}
            selected={card.id === "enterprise_wide"}
            onSelect={onSelect}
          />
        ))}
      </div>,
    );

    const group = screen.getByRole("radiogroup", { name: "Discovery focus" });
    const radios = screen.getAllByRole("radio");

    expect(group).toHaveClass("grid", "grid-cols-2", "gap-3");
    expect(radios).toHaveLength(7);
    expect(radios[6]).toHaveClass("col-span-2");
    expect(radios[6]).toHaveAttribute("aria-checked", "true");
  });
});
