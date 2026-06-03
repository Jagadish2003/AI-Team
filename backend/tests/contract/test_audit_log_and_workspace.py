"""
Contract tests for GET /api/audit-log, GET /api/workspace/members,
and POST /api/workspace/members — previously untested endpoints.
"""
import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_JWT", "dev-token-change-me")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")

from app.main import app

client = TestClient(app)


def auth():
    return {"Authorization": f"Bearer {os.environ['DEV_JWT']}"}


class TestAuditLog:
    def test_returns_200(self):
        r = client.get("/api/audit-log", headers=auth())
        assert r.status_code == 200

    def test_returns_list(self):
        r = client.get("/api/audit-log", headers=auth())
        assert isinstance(r.json(), list)

    def test_pagination_params(self):
        r = client.get("/api/audit-log?limit=5&offset=0", headers=auth())
        assert r.status_code == 200
        assert len(r.json()) <= 5

    def test_requires_auth(self):
        r = client.get("/api/audit-log")
        assert r.status_code == 401


class TestWorkspaceMembers:
    def test_list_members_returns_200(self):
        r = client.get("/api/workspace/members", headers=auth())
        assert r.status_code == 200

    def test_list_members_returns_list(self):
        r = client.get("/api/workspace/members", headers=auth())
        assert isinstance(r.json(), list)

    def test_add_member(self):
        r = client.post(
            "/api/workspace/members",
            headers=auth(),
            json={"user_id": "test-user@example.com", "role": "viewer"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == "test-user@example.com"
        assert body["role"] == "viewer"

    def test_add_member_invalid_role(self):
        r = client.post(
            "/api/workspace/members",
            headers=auth(),
            json={"user_id": "test@example.com", "role": "superuser"},
        )
        assert r.status_code == 400

    def test_add_member_missing_user_id(self):
        r = client.post(
            "/api/workspace/members",
            headers=auth(),
            json={"role": "viewer"},
        )
        assert r.status_code == 400

    def test_requires_auth(self):
        r = client.get("/api/workspace/members")
        assert r.status_code == 401
