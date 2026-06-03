"""
Contract tests for GET /api/confidence, GET /api/confidence/explanation,
and GET /api/mappings — previously untested endpoints.
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


class TestConfidenceExplanation:
    def test_returns_200(self):
        r = client.get("/api/confidence/explanation", headers=auth())
        assert r.status_code == 200

    def test_shape(self):
        r = client.get("/api/confidence/explanation", headers=auth())
        body = r.json()
        assert "level" in body
        assert "why" in body
        assert isinstance(body["why"], list)
        assert "nextAction" in body
        assert "recommendedNextSourceId" in body

    def test_requires_auth(self):
        r = client.get("/api/confidence/explanation")
        assert r.status_code == 401


class TestConfidence:
    def test_returns_200(self):
        r = client.get("/api/confidence", headers=auth())
        assert r.status_code == 200

    def test_shape(self):
        r = client.get("/api/confidence", headers=auth())
        body = r.json()
        assert "level" in body
        assert "why" in body

    def test_requires_auth(self):
        r = client.get("/api/confidence")
        assert r.status_code == 401


class TestMappings:
    def test_returns_200(self):
        r = client.get("/api/mappings", headers=auth())
        assert r.status_code == 200

    def test_returns_list(self):
        r = client.get("/api/mappings", headers=auth())
        assert isinstance(r.json(), list)

    def test_requires_auth(self):
        r = client.get("/api/mappings")
        assert r.status_code == 401
