from pathlib import Path

from app.outcome_surfaces import (
    build_empty_outcome_report_section,
    scan_outcome_vocabulary,
)


ROOT = Path(__file__).resolve().parents[3]


def test_seeded_outcome_vocabulary_violation_fails():
    violations = scan_outcome_vocabulary(
        {
            "api": "AgentIQ delivered 40% savings",
            "ui": "Movement against baseline following the recorded action",
        }
    )

    assert {item["pattern"] for item in violations} >= {
        "agent_iq_claimed_result",
        "financial_claim",
    }


def test_outcome_templates_across_api_ui_report_and_export_pass():
    components_dir = ROOT / "frontend" / "src" / "components" / "outcomes"
    ui_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in components_dir.glob("*.tsx")
    }

    export_source = (
        ROOT / "frontend" / "src" / "utils" / "exportPdf.ts"
    ).read_text(encoding="utf-8")
    export_outcome_block = export_source.split("// -- Outcome Movement", 1)[1].split(
        "Top Quick Wins",
        1,
    )[0]

    payload = {
        "api": build_empty_outcome_report_section("run_vocab"),
        "ui": ui_sources,
        "report": build_empty_outcome_report_section("run_vocab"),
        "export": export_outcome_block,
    }

    assert scan_outcome_vocabulary(payload) == []
