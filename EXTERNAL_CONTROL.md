# External registration control

The WebUI exposes authenticated endpoints for a Private service to control
registration capacity without submitting a large fixed batch.

Set the same token on the external register and in Private:

```powershell
$env:REGISTER_CONTROL_TOKEN = "replace-with-a-long-random-token"
python web.py --host 0.0.0.0 --port 5000
```

Alternatively, set `REGISTER_CONTROL_TOKEN` in `config/register.py`.

Endpoints:

```text
GET  /api/external/control/status
POST /api/external/control/reconcile
```

Authentication accepts either `Authorization: Bearer <token>` or
`X-Register-Control-Token: <token>`.

The reconcile payload is an object containing `enabled`, `target_inflight`,
`workers`, `min_interval_seconds`, and an optional `reason`. The controller
only fills the difference between the target and current pending/running jobs.
Reducing the target stops new submissions; it does not terminate registrations
already running.
