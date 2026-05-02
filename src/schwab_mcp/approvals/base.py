from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


logger = logging.getLogger(__name__)


class ApprovalDecision(str, Enum):
    """Decision returned by an approval workflow."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    """Details about a write tool invocation requiring approval."""

    id: str
    tool_name: str
    request_id: str
    client_id: str | None
    arguments: Mapping[str, str]


def format_arguments(arguments: Mapping[str, str]) -> str:
    """Render approval arguments as a fenced text block.

    Backticks in values are attacker-controlled (LLM-supplied) and would close
    a markdown code fence early, re-enabling live formatting. Substitute a
    visually similar non-metacharacter so the fence cannot be broken. Shared
    by all approval backends so the redaction-safe rendering stays consistent.
    """
    if not arguments:
        return "```\n<none>\n```"

    lines = [f"{key} = {value}" for key, value in arguments.items()]
    body = "\n".join(lines).replace("`", "ˋ")
    return f"```\n{body}\n```"


class ApprovalManager(abc.ABC):
    """Interface for asynchronous approval backends."""

    async def start(self) -> None:
        """Perform any startup/connection work."""

    async def stop(self) -> None:
        """Clean up resources."""

    @abc.abstractmethod
    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        """Require approval for the provided request."""


class NoOpApprovalManager(ApprovalManager):
    """Approval manager that always approves requests.

    Used when ``--jesus-take-the-wheel`` is set. Every auto-approval is logged
    at WARNING so there is at least an audit trail in stderr/logs of which
    write tools fired and with what (already-redacted) arguments.
    """

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        logger.warning(
            "Auto-approving write tool '%s' (request=%s, approval=%s) with no "
            "human review: %s",
            request.tool_name,
            request.request_id,
            request.id,
            dict(request.arguments),
        )
        return ApprovalDecision.APPROVED


__all__ = [
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "NoOpApprovalManager",
    "format_arguments",
]
