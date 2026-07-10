#!/usr/bin/env python3
"""Record private outreach evidence without putting sensitive values in shell history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import db as crm_db  # noqa: E402
import generate as crm_generate  # noqa: E402

InputFn = Callable[[str], str]
OutputFn = Callable[[str], Any]


def _now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ask(input_fn: InputFn, label: str, *, default: str | None = None, required: bool = False) -> str | None:
    suffix = f" [{default}]" if default is not None else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    if not value:
        value = default
    if required and not value:
        raise ValueError(f"{label} is required")
    return value


def _ask_bool(input_fn: InputFn, label: str, *, default: bool = False) -> bool:
    default_text = "y" if default else "n"
    value = (_ask(input_fn, f"{label} (y/n)", default=default_text, required=True) or "").lower()
    if value not in {"y", "yes", "n", "no"}:
        raise ValueError(f"{label} must be y or n")
    return value in {"y", "yes"}


def _ask_int(input_fn: InputFn, label: str) -> int | None:
    value = _ask(input_fn, label)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _ask_float(input_fn: InputFn, label: str) -> float | None:
    value = _ask(input_fn, label)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc


def collect_attempt(input_fn: InputFn) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": _ask(input_fn, "Attempt ID", default=f"attempt-{uuid.uuid4()}", required=True),
        "prospect_id": _ask(input_fn, "Prospect ID", required=True),
        "attempted_at": _ask(input_fn, "Attempted at (offset-aware ISO)", default=_now_local(), required=True),
        "channel": _ask(input_fn, "Channel (phone/email/linkedin)", required=True),
        "dnc_checked": _ask_bool(input_fn, "Applicable DNC/suppression checks completed"),
        "contact_role": _ask(input_fn, "Contact role"),
        "disposition": _ask(input_fn, "Disposition", required=True),
        "human_reached": _ask_bool(input_fn, "Human reached"),
        "substantive_conversation": _ask_bool(input_fn, "Substantive conversation"),
        "operator": _ask(input_fn, "Operator", default="Matthew", required=True),
    }
    if data["substantive_conversation"]:
        data.update(
            {
                "conversation_outcome": _ask(input_fn, "Conversation outcome", required=True),
                "pain_score": _ask_int(input_fn, "Pain score (0-3)"),
                "pain_type": _ask(input_fn, "Pain type"),
                "eligible_unsold_estimates": _ask_int(input_fn, "Eligible unsold estimates"),
                "ticket_value_band": _ask(input_fn, "Ticket value band"),
                "follow_up_owner_process": _ask(input_fn, "Follow-up owner/process"),
                "objection_category": _ask(input_fn, "Objection category"),
                "evidence_grade": _ask(input_fn, "Evidence grade (A/B/C)"),
                "artifact_ref": _ask(input_fn, "Private artifact reference"),
                "evidence_text": _ask(input_fn, "Exact quote or same-day evidence note"),
            }
        )
    data["next_action"] = _ask(input_fn, "Next action")
    return {key: value for key, value in data.items() if value is not None}


def collect_commercial_event(input_fn: InputFn) -> dict[str, Any]:
    data = {
        "id": _ask(input_fn, "Event ID", default=f"event-{uuid.uuid4()}", required=True),
        "prospect_id": _ask(input_fn, "Prospect ID", required=True),
        "attempt_id": _ask(input_fn, "Related attempt ID"),
        "event_type": _ask(input_fn, "Event type", required=True),
        "occurred_at": _ask(input_fn, "Occurred at (offset-aware ISO)", default=_now_local(), required=True),
        "offer_version": _ask(input_fn, "Offer version"),
        "price_amount": _ask_float(input_fn, "Price amount"),
        "price_currency": _ask(input_fn, "Price currency", default="USD"),
        "evidence_grade": _ask(input_fn, "Evidence grade (A/B)", required=True),
        "artifact_ref": _ask(input_fn, "Private artifact reference"),
        "evidence_text": _ask(input_fn, "Same-day evidence note"),
        "operator": _ask(input_fn, "Operator", default="Matthew", required=True),
    }
    return {key: value for key, value in data.items() if value is not None}


def _load_private_json(root: Path, input_path: Path) -> dict[str, Any]:
    resolved = input_path.expanduser().resolve()
    private_root = (root / "ops-crm" / "data" / "private").resolve()
    if not resolved.is_relative_to(private_root):
        raise ValueError("input JSON must be stored under ops-crm/data/private")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("private input JSON could not be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("private input JSON must contain one object")
    return payload


def _safe_write_error(exc: Exception, output: OutputFn) -> int:
    if isinstance(exc, sqlite3.IntegrityError):
        output("Record rejected: duplicate ID or invalid related record. No partial write was committed.")
    elif isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        output("Database is busy. No partial write was committed; wait five seconds and retry.")
    else:
        output(f"Record rejected: {exc}")
    return 1


def _refresh(root: Path, db_file: Path, output: OutputFn) -> int:
    try:
        crm_generate.refresh_exports(root, db_file)
    except Exception:
        output(
            "The SQLite record committed, but export refresh failed. "
            f"Retry: python3 ops-crm/record_outreach.py --root {root} --db {db_file} refresh"
        )
        return 2
    output("Private and public CRM exports refreshed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record private Evidence Loop V1 outreach facts")
    parser.add_argument("--root", type=Path, default=SCRIPT_DIR.parent)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("attempt", "commercial-event"):
        command = commands.add_parser(name)
        command.add_argument("--input-json", type=Path)
    unsuppress = commands.add_parser("unsuppress")
    unsuppress.add_argument("prospect_id")
    commands.add_parser("refresh")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    db_file = (args.db.expanduser().resolve() if args.db else crm_db.db_path(root).resolve())

    if args.command == "refresh":
        return _refresh(root, db_file, output)

    try:
        if args.command == "unsuppress":
            reason = _ask(input_fn, "Reason for lawful re-contact", required=True)
            with crm_db.connect(root, db_file) as conn:
                crm_db.init_db(conn)
                crm_db.unsuppress_prospect(conn, args.prospect_id, reason or "")
            output(f"Unsuppressed prospect {args.prospect_id}.")
            return _refresh(root, db_file, output)

        payload = (
            _load_private_json(root, args.input_json)
            if args.input_json
            else collect_attempt(input_fn) if args.command == "attempt" else collect_commercial_event(input_fn)
        )
        with crm_db.connect(root, db_file) as conn:
            crm_db.init_db(conn)
            if args.command == "attempt":
                record_id = crm_db.record_outreach_attempt(conn, payload)
            else:
                record_id = crm_db.record_commercial_event(conn, payload)
    except (ValueError, KeyError, sqlite3.Error) as exc:
        return _safe_write_error(exc, output)

    output(f"Recorded {args.command} {record_id} in private SQLite.")
    return _refresh(root, db_file, output)


if __name__ == "__main__":
    raise SystemExit(main())
