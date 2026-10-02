# MigSock communication/state optimization

Changes in this build:

1. `POST /api/kick-loop` is now a job-start endpoint. It returns HTTP 202 after the backend accepts the job instead of keeping the browser HTTP request open until the whole cycle completes.
2. The persistent browser `/ws` connection remains the event channel for progress and completion.
3. A per-room active-job guard prevents accidental duplicate jobs while the previous job is running.
4. Confirmed-kick state is broadcast as an authoritative `kick.target.confirmed` delta to all connected browser clients, so multiple open UIs can converge without refreshing the full target list.
5. Existing original-target replacement logic, target cap, WS exclusion, and server-side kick rate limit are preserved.
6. No increase to the configured kick rate limit was made.
7. The frontend now treats the HTTP response as job acceptance and relies on `kick.progress` / `kick.target.confirmed` events for live state.

Validation performed:
- `python -m py_compile app.py`
- Node syntax check of the inline frontend script
- `/health` endpoint check
- `/api/kick-loop` validation with no active WS
- `/api/sandbox-kick` local simulation
