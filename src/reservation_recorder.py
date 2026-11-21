"""Stage 3: Secure LangChain tool for recording administrator-approved reservations.

Appends confirmed bookings to a fixed file inside `data/` using the format:
    Name | Car Number | Reservation Period | Approval Time
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import threading

from langchain.tools import tool

RESERVATIONS_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "approved_reservations.txt"
)
_WRITE_LOCK = threading.Lock()


def _clean_field(value: str) -> str:
    """Prevent log/line injection by removing line breaks and field delimiters."""
    return " ".join(value.replace("|", "/").splitlines()).strip()


def format_reservation_entry(
    name: str,
    car_number: str,
    reservation_period: str,
    approval_time: str,
) -> str:
    """Format one reservation record as: Name | Car Number | Reservation Period | Approval Time."""
    fields = (
        _clean_field(name),
        _clean_field(car_number),
        _clean_field(reservation_period),
        _clean_field(approval_time),
    )
    if not all(fields):
        raise ValueError("All reservation fields must be non-empty.")
    return " | ".join(fields)


@tool
def save_approved_reservation(
    name: str,
    car_number: str,
    reservation_period: str,
    approval_time: str | None = None,
) -> dict:
    """Record an administrator-approved parking reservation to the reservations file."""
    timestamp = approval_time or datetime.now(timezone.utc).isoformat()
    try:
        entry = format_reservation_entry(
            name=name,
            car_number=car_number,
            reservation_period=reservation_period,
            approval_time=timestamp,
        )
        RESERVATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with RESERVATIONS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return {
            "ok": True,
            "entry": entry,
            "file_path": str(RESERVATIONS_FILE),
        }
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
        }
