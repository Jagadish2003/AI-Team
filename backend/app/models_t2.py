import os
from typing import Dict, List, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

RunStatus = Literal["running", "complete", "partial", "failed"]
SystemStatus = Literal["ok", "failed", "skipped"]


def get_default_mode() -> str:
    return os.getenv("INGEST_MODE", "offline").strip().lower()


class RunInputs(BaseModel):
    connectedSources: List[str] = Field(default_factory=list)
    uploadedFiles: List[str] = Field(default_factory=list)
    sampleWorkspaceEnabled: bool = False


class StartRunResponse(BaseModel):
    runId: str
    status: Literal["running"]
    startedAt: str


class StartRunRequest(BaseModel):
    """
    Single request body for POST /api/runs/start.

    This merges RunInputs + ComputeRequest into one model because FastAPI supports
    only one JSON body per request.
    """

    connectedSources: List[str] = Field(default_factory=list)
    uploadedFiles: List[str] = Field(default_factory=list)
    sampleWorkspaceEnabled: bool = False
    mode: Literal["offline", "live"] = Field(default_factory=get_default_mode)
    systems: List[str] = Field(
        default_factory=lambda: ["salesforce", "servicenow", "jira"]
    )


class ComputeRequest(BaseModel):
    mode: Literal["offline", "live"] = Field(default_factory=get_default_mode)
    systems: List[str] = Field(
        default_factory=lambda: ["salesforce", "servicenow", "jira"]
    )


class StatusResponse(BaseModel):
    runId: str
    status: RunStatus
    modeUsed: Optional[str] = None
    systemsUsed: List[str] = Field(default_factory=list)
    perSystem: Dict[str, SystemStatus] = Field(default_factory=dict)
    counts: Dict[str, int] = Field(default_factory=dict)
    errors: Dict[str, str] = Field(default_factory=dict)
    updatedAt: Optional[str] = None
    isReplay: bool = False
