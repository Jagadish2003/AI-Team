"""R16-D1 — Core model gateway types (no intra-package imports).

Placed in a separate module so provider implementations can import these
types without creating a circular dependency with __init__.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class GenerationRequest:
    """Parameters for a single text-generation call."""

    prompt: str
    max_tokens: int
    timeout_ms: int = 30000


@dataclass
class GenerationResult:
    """Result of a text-generation call.

    ``text`` is None on any failure — callers must handle None.
    ``provider`` names which backend served the request (for telemetry).
    ``ok`` is False whenever the call failed or was skipped.
    """

    text: Optional[str]
    provider: str
    ok: bool


class ModelProvider(ABC):
    """Abstract base for a model provider backend.

    Concrete implementations own all endpoint + credential handling,
    entirely inside the gateway package.  No caller ever knows or cares
    which provider is active.
    """

    name: str  # declared on each concrete subclass, not as an instance attr

    @abstractmethod
    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Generate text for the given prompt.

        Must never raise — return ``GenerationResult(text=None, ok=False, ...)``
        on any error so callers can rely on a stable return type.
        """
        ...

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embedding vectors for each input string.

        Returns an empty list on failure (degrades gracefully).
        """
        ...
