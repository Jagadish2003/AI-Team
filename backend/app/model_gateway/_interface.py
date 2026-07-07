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

    #: Whether this provider emits its own per-call telemetry event.
    #:
    #: The gateway's instrumented generate()/embed() wrappers record one
    #: telemetry event per call so every model call is observable.  A provider
    #: that already records its own per-call event (e.g. the D2
    #: HostedModelProvider) sets this True so the gateway SKIPS its emission and
    #: a single logical call produces exactly one event — never two.  Providers
    #: that don't self-report leave it False and stay observable via the gateway.
    emits_own_telemetry: bool = False

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

    def validate(self) -> None:
        """Optional startup configuration check for the SELECTED provider.

        Called by ``validate_provider_config()`` at app startup for whichever
        providers ``MODEL_GENERATION_PROVIDER`` / ``MODEL_EMBEDDING_PROVIDER``
        resolve to.  A provider may be a registered *name* yet still be
        misconfigured (e.g. selected with no endpoint URL); implementations
        should log a clear warning here so a boot-time misconfiguration is
        visible instead of silently degrading every runtime call to ok=False.

        Must never raise — startup must not be blocked by a validation hook.
        The default is a no-op for providers with nothing to validate.
        """
        return None

    def embedding_identity(self) -> "tuple[str, str]":
        """Return ``(model_identity, model_version)`` stamped on every vector this
        provider produces (R18-B1 T3 / AC8).

        The retrieval substrate records this pair PER VECTOR so vectors produced
        by different embedding models are never compared against each other — a
        model change invalidates compatibility. ``model_identity`` must therefore
        change whenever the underlying embedding model changes, so it includes the
        concrete model / deployment name, not just the provider name.

        Resolved live (like the endpoint/credential accessors) so a config repin
        takes effect without a restart. The default returns ``(self.name, "")`` —
        adequate for providers that do not embed; providers that embed via a
        named, versioned model override this.

        Must never raise — a stamping-identity lookup is on the embedding pipeline
        path, which must never block a run.
        """
        return (self.name, "")
