from __future__ import annotations
from typing import List, Literal, TypedDict, Dict

Confidence = Literal["LOW", "MEDIUM", "HIGH"]
Decision = Literal["UNREVIEWED", "APPROVED", "REJECTED"]
Tier = Literal["Quick Win", "Strategic", "Complex"]

class PermissionItem(TypedDict, total=False):
    id: str
    label: str
    required: bool
    satisfied: bool
    readiness: Literal["READY", "PENDING", "MISSING"]

class RoadmapDependency(TypedDict, total=False):
    id: str
    label: str
    status: Literal["READY", "PENDING", "MISSING"]

class OpportunityCandidate(TypedDict, total=False):
    id: str
    title: str
    category: str
    tier: Tier
    decision: Decision
    impact: int
    effort: int
    confidence: Confidence
    requiredPermissions: List[PermissionItem]

class RoadmapStage(TypedDict, total=False):
    id: Literal["NEXT_30", "NEXT_60", "NEXT_90"]
    title: str
    summary: str
    opportunities: List[OpportunityCandidate]
    requiredPermissions: List[PermissionItem]
    dependencies: List[RoadmapDependency]
    readiness: Literal["High", "Moderate", "Low"]

class PilotRoadmapModel(TypedDict, total=False):
    stages: List[RoadmapStage]
    selectedCount: int
    permissionsRequiredCount: int
    dependenciesCount: int
    overallReadiness: Literal["High", "Moderate", "Low"]
