"""In-memory audit log."""


def record_event(log: list, kind: str, payload: dict) -> None:
    """Append an event to the audit log."""
    log.append({"kind": kind, **payload})
