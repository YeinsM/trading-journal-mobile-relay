# Trading Journal Mobile Relay

Servicio remoto minimo para snapshots del movil.

## Objetivo

- Recibir snapshots desde FinTech local cuando haya internet.
- Guardar el ultimo snapshot por `installation_id`.
- Permitir que la app movil lo consulte despues con su `device_secret`.

## Variables de entorno

- `TRADING_JOURNAL_MOBILE_CLOUD_SYNC_SECRET` — secreto que autoriza a FinTech a subir snapshots.

## Ejecutar

```powershell
cd C:\Users\Home\Desktop\TechBrains\FinTech\services\trading-journal-mobile-relay
..\..\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8010
```

## Endpoints

- `GET /api/health`
- `PUT /api/trading-journal/mobile/cloud/snapshots/{installation_id}`
- `GET /api/trading-journal/mobile/cloud/snapshots/{installation_id}`