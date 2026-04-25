from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

DB_PATH = Path("data/mobile_relay.sqlite3")
CLOUD_PUSH_HEADER = "x-fintech-mobile-cloud-secret"
DEVICE_SECRET_HEADER = "x-fintech-mobile-device-secret"
CLOUD_SECRET_ENV = "TRADING_JOURNAL_MOBILE_CLOUD_SYNC_SECRET"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
    with _db_connection() as conn:
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
                int(req.event.get("version") or 0),
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