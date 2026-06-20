from app import db


def test_org_connectors_list_overlays_partial_org_rows_on_catalog(monkeypatch):
    rows = [
        {
            "id": "jira",
            "name": "Jira",
            "category": "Issues / backlog",
            "tier": "standard",
            "status": "not_configured",
            "configured": False,
            "metrics": [],
            "lastSynced": "-",
            "reads": ["Issues", "Sprints"],
            "signalStrength": 78,
        },
        {
            "id": "jira",
            "name": "Jira",
            "org_id": "default",
            "status": "connected",
        },
    ]

    monkeypatch.setattr(db, "get_all", lambda table: rows if table == "connectors" else [])

    jira = db.org_connectors_list("default")[0]

    assert jira["status"] == "connected"
    assert jira["reads"] == ["Issues", "Sprints"]
    assert jira["metrics"] == []
    assert jira["tier"] == "standard"


def test_org_connector_get_merges_partial_org_row_with_catalog(monkeypatch):
    catalog = {
        "id": "salesforce",
        "name": "Salesforce",
        "category": "CRM",
        "tier": "recommended",
        "status": "disconnected",
        "configured": False,
        "metrics": [],
        "lastSynced": "-",
        "reads": ["Case", "Opportunity"],
        "signalStrength": 94,
    }
    override = {
        "id": "salesforce",
        "name": "Salesforce",
        "org_id": "default",
        "status": "connected",
    }

    def fake_get_one(table, key):
        assert table == "connectors"
        if key == "salesforce":
            return catalog
        if key == "default::salesforce":
            return override
        return None

    monkeypatch.setattr(db, "get_one", fake_get_one)

    salesforce = db.org_connector_get("default", "salesforce")

    assert salesforce is not None
    assert salesforce["status"] == "connected"
    assert salesforce["reads"] == ["Case", "Opportunity"]
    assert salesforce["tier"] == "recommended"
