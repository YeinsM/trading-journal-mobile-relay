from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

DB_PATH = Path("data/mobile_relay.sqlite3")
CLOUD_PUSH_HEADER = "x-fintech-mobile-cloud-secret"
DEVICE_SECRET_HEADER = "x-fintech-mobile-device-secret"
CLOUD_SECRET_ENV = "TRADING_JOURNAL_MOBILE_CLOUD_SYNC_SECRET"
PAYMENT_COMMISSIONS_CLOUD_SECRET_ENV = "PAYMENT_COMMISSIONS_CLOUD_SYNC_SECRET"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


@contextmanager
def _db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_tables() -> None:
    with _db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mobile_cloud_snapshots (
                installation_id TEXT PRIMARY KEY,
                device_label TEXT NOT NULL,
                device_secret TEXT NOT NULL,
                selected_account_ids_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS ix_mobile_cloud_snapshots_updated
            ON mobile_cloud_snapshots (updated_at DESC);

            CREATE TABLE IF NOT EXISTS payment_commissions_cloud_snapshots (
                installation_id TEXT PRIMARY KEY,
                device_label TEXT NOT NULL,
                device_secret TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS ix_payment_commissions_cloud_snapshots_updated
            ON payment_commissions_cloud_snapshots (updated_at DESC);
            """
        )


def _cloud_secret() -> str | None:
    return _clean_text(os.getenv(CLOUD_SECRET_ENV))


def _serialize_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    selected_ids = json.loads(row["selected_account_ids_json"] or "[]")
    return {
        "installation_id": row["installation_id"],
        "device_label": row["device_label"],
        "selected_account_ids": selected_ids,
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
        "event": payload.get("event") or {},
        "dashboard": payload.get("dashboard") or {},
    }


def _serialize_snapshot_metadata_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    selected_ids = json.loads(row["selected_account_ids_json"] or "[]")
    return {
        "installation_id": row["installation_id"],
        "device_label": row["device_label"],
        "selected_account_ids": selected_ids,
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
        "event": payload.get("event") or {},
    }


def _snapshot_is_newer_than_row(row: sqlite3.Row, event: dict[str, Any], version: int) -> bool:
    payload = json.loads(row["payload_json"])
    current_event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    incoming_updated_at = _parse_iso_datetime(event.get("updated_at"))
    current_updated_at = _parse_iso_datetime(current_event.get("updated_at")) or _parse_iso_datetime(row["updated_at"])

    if incoming_updated_at is not None and current_updated_at is not None:
        if incoming_updated_at != current_updated_at:
            return incoming_updated_at > current_updated_at

    current_version = int(row["version"] or 0)
    return version > current_version


class CloudSnapshotPushRequest(BaseModel):
    device_label: str = Field(..., min_length=2, max_length=80)
    device_secret: str = Field(..., min_length=16, max_length=128)
    selected_account_ids: list[str] = Field(default_factory=list)
    event: dict[str, Any]
    dashboard: dict[str, Any]


class CloudSnapshotResponse(BaseModel):
    installation_id: str
    device_label: str
    selected_account_ids: list[str]
    version: int
    updated_at: str
    event: dict[str, Any]
    dashboard: dict[str, Any]


class CloudSnapshotMetadataResponse(BaseModel):
    installation_id: str
    device_label: str
    selected_account_ids: list[str]
    version: int
    updated_at: str
    event: dict[str, Any]


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_tables()
    yield


app = FastAPI(
    title="Trading Journal Mobile Relay",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.put(
    "/api/trading-journal/mobile/cloud/snapshots/{installation_id}",
    response_model=CloudSnapshotResponse,
)
async def put_snapshot(installation_id: str, req: CloudSnapshotPushRequest, request: Request):
    expected_secret = _cloud_secret()
    if expected_secret is None:
        raise HTTPException(503, "El relay no tiene cloud sync configurado.")

    received_secret = _clean_text(request.headers.get(CLOUD_PUSH_HEADER))
    if received_secret != expected_secret:
        raise HTTPException(401, "Cloud sync no autorizado.")

    normalized_installation_id = _clean_text(installation_id)
    if normalized_installation_id is None:
        raise HTTPException(400, "La instalacion movil es obligatoria.")

    payload = {
        "event": req.event,
        "dashboard": req.dashboard,
    }
    now_iso = _now_iso()
    incoming_version = int(req.event.get("version") or 0)
    with _db_connection() as conn:
        existing_row = conn.execute(
            "SELECT * FROM mobile_cloud_snapshots WHERE installation_id = ?",
            (normalized_installation_id,),
        ).fetchone()
        if existing_row is not None and not _snapshot_is_newer_than_row(
            existing_row,
            req.event,
            incoming_version,
        ):
            return _serialize_snapshot_row(existing_row)

        conn.execute(
            """
            INSERT INTO mobile_cloud_snapshots (
                installation_id,
                device_label,
                device_secret,
                selected_account_ids_json,
                payload_json,
                version,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(installation_id) DO UPDATE SET
                device_label = excluded.device_label,
                device_secret = excluded.device_secret,
                selected_account_ids_json = excluded.selected_account_ids_json,
                payload_json = excluded.payload_json,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (
                normalized_installation_id,
                req.device_label,
                req.device_secret,
                json.dumps(req.selected_account_ids, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                incoming_version,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM mobile_cloud_snapshots WHERE installation_id = ?",
            (normalized_installation_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(500, "No se pudo guardar el snapshot remoto.")
    return _serialize_snapshot_row(row)


@app.get(
    "/api/trading-journal/mobile/cloud/snapshots/{installation_id}/meta",
    response_model=CloudSnapshotMetadataResponse,
)
async def get_snapshot_metadata(installation_id: str, request: Request):
    received_device_secret = _clean_text(request.headers.get(DEVICE_SECRET_HEADER))
    if received_device_secret is None:
        raise HTTPException(401, "El movil debe enviar su secreto de dispositivo.")

    with _db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mobile_cloud_snapshots WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "No hay snapshot remoto para esta instalacion.")
    if _clean_text(row["device_secret"]) != received_device_secret:
        raise HTTPException(401, "El secreto del dispositivo no coincide.")
    return _serialize_snapshot_metadata_row(row)


@app.get(
    "/api/trading-journal/mobile/cloud/snapshots/{installation_id}",
    response_model=CloudSnapshotResponse,
)
async def get_snapshot(installation_id: str, request: Request):
    received_device_secret = _clean_text(request.headers.get(DEVICE_SECRET_HEADER))
    if received_device_secret is None:
        raise HTTPException(401, "El movil debe enviar su secreto de dispositivo.")

    with _db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mobile_cloud_snapshots WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "No hay snapshot remoto para esta instalacion.")
    if _clean_text(row["device_secret"]) != received_device_secret:
        raise HTTPException(401, "El secreto del dispositivo no coincide.")
    return _serialize_snapshot_row(row)


# ---------------------------------------------------------------------------
# Payment-Commissions cloud relay endpoints
# ---------------------------------------------------------------------------

def _payment_commissions_cloud_secret() -> str | None:
    return _clean_text(os.getenv(PAYMENT_COMMISSIONS_CLOUD_SECRET_ENV))


def _serialize_commissions_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "installation_id": row["installation_id"],
        "device_label": row["device_label"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
        "event": payload.get("event") or {},
        "dashboard": payload.get("dashboard") or {},
    }


class CommissionsCloudSnapshotPushRequest(BaseModel):
    device_label: str = Field(..., min_length=2, max_length=80)
    device_secret: str = Field(..., min_length=16, max_length=128)
    event: dict[str, Any]
    dashboard: dict[str, Any]


class CommissionsCloudSnapshotResponse(BaseModel):
    installation_id: str
    device_label: str
    version: int
    updated_at: str
    event: dict[str, Any]
    dashboard: dict[str, Any]


@app.put(
    "/api/payment-commissions/mobile/cloud/snapshots/{installation_id}",
    response_model=CommissionsCloudSnapshotResponse,
)
async def put_commissions_snapshot(
    installation_id: str,
    req: CommissionsCloudSnapshotPushRequest,
    request: Request,
):
    expected_secret = _payment_commissions_cloud_secret()
    if expected_secret is None:
        raise HTTPException(503, "El relay de comisiones no tiene cloud sync configurado.")

    received_secret = _clean_text(request.headers.get(CLOUD_PUSH_HEADER))
    if received_secret != expected_secret:
        raise HTTPException(401, "Cloud sync de comisiones no autorizado.")

    normalized_id = _clean_text(installation_id)
    if normalized_id is None:
        raise HTTPException(400, "La instalacion movil es obligatoria.")

    payload = {"event": req.event, "dashboard": req.dashboard}
    now_iso = _now_iso()
    version = int(req.event.get("version") or 0)
    with _db_connection() as conn:
        conn.execute(
            """
            INSERT INTO payment_commissions_cloud_snapshots (
                installation_id,
                device_label,
                device_secret,
                payload_json,
                version,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(installation_id) DO UPDATE SET
                device_label = excluded.device_label,
                device_secret = excluded.device_secret,
                payload_json = excluded.payload_json,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (normalized_id, req.device_label, req.device_secret, json.dumps(payload, ensure_ascii=False), version, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT * FROM payment_commissions_cloud_snapshots WHERE installation_id = ?",
            (normalized_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(500, "No se pudo guardar el snapshot de comisiones.")
    return _serialize_commissions_snapshot_row(row)


@app.get(
    "/api/payment-commissions/mobile/cloud/snapshots/{installation_id}",
    response_model=CommissionsCloudSnapshotResponse,
)
async def get_commissions_snapshot(installation_id: str, request: Request):
    received_device_secret = _clean_text(request.headers.get(DEVICE_SECRET_HEADER))
    if received_device_secret is None:
        raise HTTPException(401, "El movil debe enviar su secreto de dispositivo.")

    with _db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_commissions_cloud_snapshots WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "No hay snapshot de comisiones para esta instalacion.")
    if _clean_text(row["device_secret"]) != received_device_secret:
        raise HTTPException(401, "El secreto del dispositivo no coincide.")
    return _serialize_commissions_snapshot_row(row)
