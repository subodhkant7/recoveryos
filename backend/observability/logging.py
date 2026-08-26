"""
Centralized Structured JSON Logging and Secret Redaction Subsystem.

Provides machine-readable JSON logging with contextual request/workflow identifiers
and deterministic recursive redaction of credentials, API keys, tokens, and PII.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from backend.config import config

# Contextual variables for request and workflow correlation
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_request_id", default="")
current_workflow_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_workflow_id", default="")
current_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_tenant_id", default="")

# Canonical Workflow Lifecycle & Operability Event Names
EVENT_WORKFLOW_DISPATCHED = "WORKFLOW_DISPATCHED"
EVENT_WORKFLOW_PUBLISH_FAILED = "WORKFLOW_PUBLISH_FAILED"
EVENT_WORKFLOW_CONSUMED = "WORKFLOW_CONSUMED"
EVENT_WORKFLOW_CLAIMED = "WORKFLOW_CLAIMED"
EVENT_WORKFLOW_DUPLICATE = "WORKFLOW_DUPLICATE"
EVENT_WORKFLOW_EXECUTION_STARTED = "WORKFLOW_EXECUTION_STARTED"
EVENT_WORKFLOW_EXECUTION_COMPLETED = "WORKFLOW_EXECUTION_COMPLETED"
EVENT_WORKFLOW_EXECUTION_FAILED = "WORKFLOW_EXECUTION_FAILED"
EVENT_WORKFLOW_RETRYABLE_FAILURE = "WORKFLOW_RETRYABLE_FAILURE"
EVENT_WORKFLOW_OCC_MISMATCH = "WORKFLOW_OCC_MISMATCH"
EVENT_WORKFLOW_TERMINAL_SKIP = "WORKFLOW_TERMINAL_SKIP"
EVENT_WORKFLOW_DLQ = "WORKFLOW_DLQ"
EVENT_WORKFLOW_ACK = "WORKFLOW_ACK"
EVENT_WORKFLOW_NACK = "WORKFLOW_NACK"
EVENT_WORKFLOW_RECOVERED = "WORKFLOW_RECOVERED"

SENSITIVE_KEY_PATTERNS = re.compile(
    r"(?i)(token|jwt|auth|secret|password|key|api_key|private|credential|bearer|access_token|refresh_token)"
)
JWT_PATTERN = re.compile(r"eyJ[a-zA-Z0-9_.-]{10,}")
API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z-_.]+")


def redact_sensitive_data(val: Any) -> Any:
    """
    Recursively traverse dictionaries, lists, and strings to redact sensitive information.
    """
    if isinstance(val, dict):
        redacted = {}
        for k, v in val.items():
            if SENSITIVE_KEY_PATTERNS.search(str(k)):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    elif isinstance(val, (list, tuple, set)):
        return [redact_sensitive_data(item) for item in val]
    elif isinstance(val, str):
        # Mask inline JWTs and Google API Keys
        masked = JWT_PATTERN.sub("[REDACTED_JWT]", val)
        masked = API_KEY_PATTERN.sub("[REDACTED_API_KEY]", masked)
        return masked
    return val


class StructuredJsonFormatter(logging.Formatter):
    """
    Formats standard log records as structured JSON lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": "recoveryos",
            "environment": config.environment,
            "message": redact_sensitive_data(record.getMessage()),
        }

        # Context correlation identifiers
        req_id = current_request_id.get("")
        if req_id:
            log_entry["request_id"] = req_id

        wf_id = current_workflow_id.get("") or getattr(record, "workflow_id", "")
        if wf_id:
            log_entry["workflow_id"] = wf_id

        tenant_id = current_tenant_id.get("") or getattr(record, "tenant_id", "")
        if tenant_id:
            log_entry["tenant_id"] = tenant_id

        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "UnknownException",
                "message": redact_sensitive_data(str(record.exc_info[1])),
                "traceback": self.formatException(record.exc_info),
            }

        # Include structured extra fields if present
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            log_entry["extra"] = redact_sensitive_data(record.extra_fields)

        return json.dumps(log_entry)


def setup_logging() -> None:
    """
    Configures root logger with the StructuredJsonFormatter without duplicating handlers.
    """
    root_logger = logging.getLogger()
    
    # Avoid duplicate handlers on re-init
    for handler in list(root_logger.handlers):
        if isinstance(handler.formatter, StructuredJsonFormatter):
            return
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
