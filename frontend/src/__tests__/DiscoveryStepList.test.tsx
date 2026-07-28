// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  DiscoveryStepList,
  orderSourcesByConnectLog,
  parseConnectOrder,
} from "../pages/DiscoveryRunPage";

describe("DiscoveryStepList step state (CS-4 T5)", () => {
  it("marks an in-progress step active and earlier steps completed", () => {
    render(<DiscoveryStepList currentStep="sn" />);

    // Backend emission order: sf_crm → sn → jira → sf_ncino → … so only
    // sf_crm precedes "sn" → 1 completed; "sn" itself → active.
    expect(screen.getAllByLabelText("completed").length).toBe(1);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });

  it("shows the terminal Complete step as completed (check) only once the run has finished", () => {
    render(<DiscoveryStepList currentStep="complete" runComplete />);

    // Legacy (no connected-source list) shows every known source stage; all of
    // them — including "Complete" — render the completed check on a finished run.
    const items = screen.getAllByRole("listitem").length;
    expect(screen.getAllByLabelText("completed").length).toBe(items);
    // No spinner remains once the run has finished.
    expect(screen.queryByLabelText("active")).not.toBeInTheDocument();
  });

  it("keeps Complete as a spinner while the run is still computing at the complete step", () => {
    render(<DiscoveryStepList currentStep="complete" runComplete={false} />);

    // Every preceding step is done, but Complete is not ticked yet.
    const items = screen.getAllByRole("listitem").length;
    expect(screen.getAllByLabelText("completed").length).toBe(items - 1);
    // Complete shows the active spinner until the run reaches 100%.
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });

  it("marks every step completed on a finished run even when current_step is stale/early", () => {
    // Regression: the backend does not always advance current_step to "complete"
    // for a finished run — it can be left at an early stage (e.g. "sf_crm"). A
    // finished run (runComplete) must still show every step done and NEVER a
    // spinner on step 0 or a pending circle.
    render(
      <DiscoveryStepList
        currentStep="sf_crm"
        runComplete
        connectedSources={["salesforce", "servicenow", "jira"]}
      />
    );
    expect(screen.queryByLabelText("active")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("pending")).not.toBeInTheDocument();
    const items = screen.getAllByRole("listitem").length;
    expect(screen.getAllByLabelText("completed").length).toBe(items);
  });

  it("renders a Slack stage so a connected Slack source shows in Discovery Progress", () => {
    // Slack is a connected source, ingested after the systems of record and
    // BEFORE the pack-specific second pass, so its step is active while
    // current_step === "slack" and the three systems of record precede it.
    render(<DiscoveryStepList currentStep="slack" />);

    expect(screen.getByText("Slack")).toBeInTheDocument();
    // sf_crm, sn, jira precede Slack → 3 completed; Slack itself active. The pack
    // pass (sf_ncino) now comes after Slack, so it is still pending.
    expect(screen.getAllByLabelText("completed").length).toBe(3);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });
});

describe("DiscoveryStepList Salesforce product labelling (CS-4)", () => {
  it("defaults the second Salesforce pass to nCino Lending when no product is declared", () => {
    render(<DiscoveryStepList currentStep="sf_crm" />);
    expect(screen.getByText("nCino Lending")).toBeInTheDocument();
    expect(
      screen.getByText("Ingesting nCino loan origination signals")
    ).toBeInTheDocument();
  });

  it("relabels the second Salesforce pass to Service Cloud when declared", () => {
    render(
      <DiscoveryStepList currentStep="sf_crm" salesforceProduct="salesforce_sc" />
    );
    expect(screen.getByText("Service Cloud")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Ingesting case management, service request, and SLA signals"
      )
    ).toBeInTheDocument();
    // The nCino copy must no longer appear once Service Cloud is declared.
    expect(screen.queryByText("nCino Lending")).not.toBeInTheDocument();
  });

  it("shows a pack step per declared product for a multi-pack run", () => {
    // R191-P1: a Salesforce workspace declaring Service Cloud + nCino runs both
    // packs, so BOTH pack steps appear in the Discovery Progress list.
    render(
      <DiscoveryStepList
        currentStep="sf_crm"
        salesforceProducts={["salesforce_sc", "salesforce_ncino"]}
      />
    );
    expect(screen.getByText("Service Cloud")).toBeInTheDocument();
    expect(screen.getByText("nCino Lending")).toBeInTheDocument();
    expect(
      screen.getByText("Ingesting nCino loan origination signals")
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Ingesting case management, service request, and SLA signals"
      )
    ).toBeInTheDocument();
  });

  it("places the Service Cloud (pack) pass after all connected sources", () => {
    render(
      <DiscoveryStepList
        currentStep="sf_ncino"
        salesforceProduct="salesforce_sc"
      />
    );
    // The pack pass (sf_ncino) is emitted last among the ingest steps, after
    // every connected source (sf_crm, sn, jira, slack, teams, confluence,
    // sharepoint, github, java_app, dotnet_app) → all ten source stages
    // completed, confirming the selected pack renders after all sources.
    expect(screen.getAllByLabelText("completed").length).toBe(10);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });
});

describe("DiscoveryStepList dynamic connected-source progress", () => {
  it("shows a stage for every connected source (matching the run's sources)", () => {
    render(
      <DiscoveryStepList
        currentStep="slack"
        connectedSources={["salesforce", "servicenow", "jira", "slack"]}
      />
    );
    // Every connected source has a stage, plus the processing stages.
    expect(screen.getByText("Salesforce CRM")).toBeInTheDocument();
    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(screen.getByText("Jira")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();
    expect(screen.getByText("Pattern Detection")).toBeInTheDocument();
    expect(screen.getByText("Complete")).toBeInTheDocument();
  });

  it("omits sources that are not connected", () => {
    // Only Salesforce + Slack connected → ServiceNow and Jira must not appear.
    render(
      <DiscoveryStepList
        currentStep="slack"
        connectedSources={["salesforce", "slack"]}
      />
    );
    expect(screen.getByText("Salesforce CRM")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();
    expect(screen.queryByText("ServiceNow")).not.toBeInTheDocument();
    expect(screen.queryByText("Jira")).not.toBeInTheDocument();
    // Processing stages remain.
    expect(screen.getByText("Pattern Detection")).toBeInTheDocument();

    // Only sf_crm (0) precedes Slack (3) here → 1 completed; Slack active. The
    // pack pass (sf_ncino) is rendered after Slack and is still pending.
    expect(screen.getAllByLabelText("completed").length).toBe(1);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });

  it("accepts display names as well as ids (case-insensitive)", () => {
    render(
      <DiscoveryStepList
        currentStep="sn"
        connectedSources={["ServiceNow", "Jira"]}
      />
    );
    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(screen.getByText("Jira")).toBeInTheDocument();
    expect(screen.queryByText("Salesforce CRM")).not.toBeInTheDocument();
    expect(screen.queryByText("Slack")).not.toBeInTheDocument();
  });

  it("renders a generic stage for a connected source with no dedicated step", () => {
    // A connector without its own pipeline step (e.g. SAP) still appears so
    // every connected source is represented in progress. (Teams/Confluence/
    // SharePoint/GitHub now each have a dedicated step and are covered above.)
    render(
      <DiscoveryStepList
        currentStep="complete"
        runComplete
        connectedSources={["SAP"]}
      />
    );
    expect(screen.getByText("SAP")).toBeInTheDocument();
    // Once the run is complete the generic source stage is ticked too.
    // Generic SAP + Pattern Detection + Entity Enrichment + Complete = 4.
    expect(screen.getAllByLabelText("completed").length).toBe(4);
  });

  it("shows only processing stages when no sources are connected", () => {
    render(<DiscoveryStepList currentStep={null} connectedSources={[]} />);
    expect(screen.queryByText("Salesforce CRM")).not.toBeInTheDocument();
    expect(screen.queryByText("Slack")).not.toBeInTheDocument();
    expect(screen.getByText("Pattern Detection")).toBeInTheDocument();
    expect(screen.getByText("Entity Enrichment")).toBeInTheDocument();
    expect(screen.getByText("Complete")).toBeInTheDocument();
  });

  it("renders source stages in the connected-source list order (mirrors the log)", () => {
    // The caller passes sources in Discovery Log CONNECT order; the progress
    // stages must appear in exactly that order (systems of record and generic
    // connectors interleaved as logged), then the pack pass, then processing.
    render(
      <DiscoveryStepList
        currentStep={null}
        connectedSources={[
          "servicenow",
          "jira",
          "teams",
          "confluence",
          "sharepoint",
          "github",
          "salesforce",
        ]}
        salesforceProduct="salesforce_sc"
      />
    );
    const labels = screen
      .getAllByRole("listitem")
      .map((li) => li.querySelector("span")?.textContent ?? "");
    expect(labels).toEqual([
      "ServiceNow",
      "Jira",
      "Microsoft Teams",
      "Confluence",
      "SharePoint",
      "GitHub",
      "Salesforce CRM",
      "Service Cloud", // pack second pass, appended after every connected source
      "Pattern Detection",
      "Entity Enrichment",
      "Complete",
    ]);
  });
});

describe("parseConnectOrder", () => {
  it("parses the live 'authenticated connectors' log line in order", () => {
    const order = parseConnectOrder([
      { stage: "QUEUED", message: "Discovery run queued." },
      {
        stage: "CONNECT",
        message:
          "Using authenticated connectors: servicenow, jira, teams, confluence, sharepoint, github",
      },
      { stage: "INGEST", message: "Ingesting data from enterprise systems" },
    ]);
    expect(order).toEqual([
      "servicenow",
      "jira",
      "teams",
      "confluence",
      "sharepoint",
      "github",
    ]);
  });

  it("parses the offline 'Connected sources' log line", () => {
    expect(
      parseConnectOrder([
        { stage: "CONNECT", message: "Connected sources: salesforce, slack" },
      ])
    ).toEqual(["salesforce", "slack"]);
  });

  it("returns null when there is no CONNECT event yet", () => {
    expect(
      parseConnectOrder([{ stage: "QUEUED", message: "Discovery run queued." }])
    ).toBeNull();
  });
});

describe("orderSourcesByConnectLog", () => {
  it("reorders the run's sources to follow the log order", () => {
    const ordered = orderSourcesByConnectLog(
      ["salesforce", "jira", "servicenow", "github", "teams"],
      ["servicenow", "jira", "teams", "github"]
    );
    // Logged sources sort by log position; salesforce (not logged) trails.
    expect(ordered).toEqual([
      "servicenow",
      "jira",
      "teams",
      "github",
      "salesforce",
    ]);
  });

  it("maps Salesforce product variants to the salesforce log rank", () => {
    const ordered = orderSourcesByConnectLog(
      ["salesforce_sc", "servicenow"],
      ["servicenow", "salesforce"]
    );
    expect(ordered).toEqual(["servicenow", "salesforce_sc"]);
  });

  it("returns the input unchanged when there is no log order", () => {
    const input = ["salesforce", "jira"];
    expect(orderSourcesByConnectLog(input, null)).toEqual(input);
  });
});
