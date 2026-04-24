from pydantic import BaseModel, Field
from typing import List

class LinkedCluster(BaseModel):
    id: str
    key: str
    normalizedKey: str
    sources: List[str] = Field(default_factory=list)
    evidenceIds: List[str] = Field(default_factory=list)
    summary: str
    tsEpochMax: int
