# Medical Monitor App

This is a separate prototype app for your hospital-bed monitoring idea. It is intentionally isolated from the existing upload platform so we can build the medical product direction without mixing domains.

## What this first version does

- Shows a live dashboard for monitored beds
- Displays patient vitals and current alert level
- Shows the assigned doctor for each patient
- Lets you simulate stable, watch, and critical conditions
- Exposes API endpoints for future Raspberry Pi/device integration

## Files

- `medical_monitor_app/app.py`: FastAPI app and dashboard page
- `medical_monitor_app/data.py`: in-memory demo patient data
- `medical_monitor_app/schemas.py`: API response models

## Run locally

```powershell
uvicorn medical_monitor_app.app:app --reload --port 8010
```

Then open:

- `http://127.0.0.1:8010/`
- `http://127.0.0.1:8010/api/health`
- `http://127.0.0.1:8010/docs`

## Suggested next build steps

1. Replace simulated patient data with SQLite or PostgreSQL storage.
2. Add staff login and patient assignment rules.
3. Connect Raspberry Pi inputs or device protocol adapters.
4. Add real notifications for doctor and nurse alerts.
5. Add audit logs and safety rules before any pilot use.
