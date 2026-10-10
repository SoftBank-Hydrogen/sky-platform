"""Recoverable upload preview references and preparation ownership."""

from dataclasses import dataclass


class PreviewBusy(ValueError):
    """Another request still owns the preview preparation lease."""


class PreviewUnavailable(ValueError):
    """A preview expired, changed, or requires unsupported preparation."""


@dataclass(frozen=True)
class PreviewLease:
    id: str
    owner: str
    epoch: int
    workspace: str
