import os
from typing import Any, Dict, List, Literal, Optional

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
    current_step: Optional[str] = None
    # CS-4 / AT-313: discovery steps whose ingest failed during this run. The
    # frontend renders these as failed rather than completed in the step list.
    failed_steps: List[str] = Field(default_factory=list)
    # 2.0-D4 T5 (AC6): whether the run actually delivered everything it set out
    # to, and what it did not. A finished run is not necessarily a complete one,
    # and a poller that only reads `status` would never know the difference.
    # Declared here because response_model strips anything the model omits — a
    # field added to the handler alone would vanish silently.
    completeness: Optional[Dict[str, Any]] = None
