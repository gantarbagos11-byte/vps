import asyncio
import os
import uuid
from functools import wraps
from pathlib import Path

from aiohttp import web, ClientSession, ClientWSTimeout, WSMsgType

from pattern_engine import PatternEngine
from auth import (
    SESSION_COOKIE, create_session, authenticate, destroy_session, get_session,
    init_auth_db, list_users, create_user, set_enabled, delete_user, change_password,
)

ROOT = Path(__file__).resolve().parent / "public"
API_URL = "wss://developer.mig33.id/developer/ws"
SUBPROTOCOL = "mig33.developer.ws.v1"
PORT = int(os.environ.get("PORT", "3000"))

# Registry of upstream sockets currently used by the browser's WS1-WS10 slots.
ACTIVE_UPSTREAM = {}
# Per-login-session upstream registry. A browser session owns only its own WS1-WS10.
SESSION_UPSTREAM = {}
WS_SESSION = {}
SOCKET_LOCKS = {}
KICK_RATE_LOCKS = {}
KICK_RATE_STATE = {}
SUICIDE_KEYS = set()
# Browser sockets and suicide de-duplication are isolated by authenticated session.
SESSION_SUICIDE_KEYS = {}
SESSION_BROWSERS = {}
BROWSER_SESSION = {}
BROWSER_CLIENTS = set()


def _current_user(request):
    return get_session(request.cookies.get(SESSION_COOKIE))


def require_auth(handler):
    @wraps(handler)
    async def wrapped(request):
        user = _current_user(request)
        if not user:
            if request.path.startswith("/api/") or request.path == "/ws":
                return web.json_response({"ok": False, "error": "Login diperlukan"}, status=401)
            raise web.HTTPFound("/login")
        request["user"] = user
        request["session_id"] = request.cookies.get(SESSION_COOKIE)
        return await handler(request)
    return wrapped

# Active KICK ALL jobs grouped by room. Each job tracks targets that have
# already been confirmed kicked by an upstream room event.
ACTIVE_KICK_JOBS = {}
ACTIVE_KICK_TASKS = {}
# Actual asyncio tasks, kept separately so LOGOUT ALL can cancel stale jobs.
ACTIVE_KICK_TASK_HANDLES = {}

def _norm_room_key(room):
    return str(room or "").strip().casefold()

def _event_payload_dict(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload

def _kick_event_info(payload):
    data = _event_payload_dict(payload)
    typ = str(payload.get("type", data.get("type", data.get("event_type", "")))).strip().lower()
    room = str(payload.get("room", data.get("room", ""))).strip()
    target = str(payload.get("target_username", data.get("target_username", data.get("username", data.get("target", ""))))).strip()
    status = str(payload.get("status_message", data.get("status_message", data.get("message", "")))).strip().lower()
    return typ, room, target, status

def _is_confirmed_kicked_event(payload):
    typ, room, target, status = _kick_event_info(payload)
    if not target:
        return False
    exact = {
        "room.participant.kicked", "room.member.kicked", "room.user.kicked",
        "participant.kicked", "member.kicked", "user.kicked",
    }
    if typ in exact:
        return True
    if "vote" in typ or typ in {"room.kick.result", "room.command.result"}:
        return False
    return any(x in status for x in (
        "has been kicked", "was kicked", "have been kicked", "kicked from the room"
    ))

async def mark_confirmed_kick(payload, session_id):
    if not _is_confirmed_kicked_event(payload):
        return None
    typ, room, target, status = _kick_event_info(payload)
    room_key = _norm_room_key(room)
    if not room_key or not target or not session_id:
        return None
    target_key = target.casefold()
    job_key = (session_id, room_key)
    jobs = ACTIVE_KICK_JOBS.get(job_key, [])
    newly_marked = False
    for state in list(jobs):
        if not isinstance(state, dict):
            continue
        lock = state.get("lock")
        if lock is None:
            lock = asyncio.Lock()
            state["lock"] = lock
        async with lock:
            events = state.get("events", {})
            if target_key not in events:
                continue
            kicked_targets = state.setdefault("kicked", set())
            if target_key not in kicked_targets:
                kicked_targets.add(target_key)
                events[target_key].set()
                newly_marked = True
                callback = state.get("on_kicked")
                if callback is not None:
                    task = asyncio.create_task(callback(target))
                    tasks = state.setdefault("replacement_tasks", set())
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
    if newly_marked:
        print(f"[KICK EVENT] session={session_id[:8]} confirmed kicked room={room!r} target={target!r} type={typ}", flush=True)
    return {"room": room, "target": target, "type": typ}


# Server-authoritative kick limit: 200 kicks/second per upstream WebSocket.
# Defined directly in app.py so every kick path uses the same limit.
KICK_MAX_PER_SECOND = 200
KICK_LIMIT_LABEL = f"{KICK_MAX_PER_SECOND}/socket/second"

DOWNLOAD_DIRS = [
    Path.home() / "storage" / "downloads",
    Path.home() / "storage" / "shared" / "Download",
    Path("/sdcard/Download"),
    Path.home() / "Downloads",
    Path(__file__).resolve().parent / "configs",
]



def safe_config_filename(name):
    name = str(name or "").strip()
    if name.lower().endswith(".json"):
        name = name[:-5]
    # Keep the filename simple and safe for Android Downloads.
    name = "".join(ch if ch.isalnum() or ch in "-_ ." else "_" for ch in name).strip(" .")
    if not name:
        raise ValueError("Nama file kosong")
    return name + ".json"

def find_download_dir():
    for d in DOWNLOAD_DIRS:
        try:
            if d.exists() and d.is_dir():
                return d
        except Exception:
            pass
    # If no platform-specific folder exists, use a local project folder.
    # This makes the same build work in GitHub Codespaces/Linux as well as Termux.
    d = Path(__file__).resolve().parent / "configs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None

def find_config_file(filename):
    for d in DOWNLOAD_DIRS:
        f = d / filename
        try:
            if f.is_file():
                return f
        except Exception:
            pass
    return None

async def save_config_file(request):
    try:
        data = await request.json()
        filename = safe_config_filename(data.get("name"))
        cfg = data.get("config")
        if not isinstance(cfg, dict):
            raise ValueError("Config tidak valid")
        d = find_download_dir()
        if d is None:
            return web.json_response({"ok": False, "error": "Folder konfigurasi tidak tersedia."}, status=500)
        path = d / filename
        import json
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return web.json_response({"ok": True, "filename": filename, "path": str(path)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

async def load_config_file(request):
    try:
        filename = safe_config_filename(request.query.get("name", ""))
        path = find_config_file(filename)
        if path is None:
            return web.json_response({"ok": False, "error": f"File {filename} tidak ditemukan di Download."}, status=404)
        import json
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("Isi file bukan config JSON")
        return web.json_response({"ok": True, "filename": filename, "config": cfg})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


def register_socket(session_id, username, ws):
    if not session_id or not username:
        return
    key = username.strip().casefold()
    if not key:
        return
    SESSION_UPSTREAM.setdefault(session_id, {})[key] = ws
    WS_SESSION[id(ws)] = session_id
    # Keep a legacy global registry only for diagnostics/protection; kick jobs never use it.
    ACTIVE_UPSTREAM[(session_id, key)] = ws
    SOCKET_LOCKS.setdefault(id(ws), asyncio.Lock())
    KICK_RATE_LOCKS.setdefault(id(ws), asyncio.Lock())
    KICK_RATE_STATE.setdefault(id(ws), {"window_start": 0.0, "count": 0})

def unregister_socket(session_id, username, ws):
    if session_id:
        sockets = SESSION_UPSTREAM.get(session_id)
        if sockets is not None and username:
            key = username.strip().casefold()
            if sockets.get(key) is ws:
                sockets.pop(key, None)
            if not sockets:
                SESSION_UPSTREAM.pop(session_id, None)
        elif sockets is not None:
            for key, current in list(sockets.items()):
                if current is ws:
                    sockets.pop(key, None)
            if not sockets:
                SESSION_UPSTREAM.pop(session_id, None)
    for key, current in list(ACTIVE_UPSTREAM.items()):
        if current is ws:
            ACTIVE_UPSTREAM.pop(key, None)
    WS_SESSION.pop(id(ws), None)
    SOCKET_LOCKS.pop(id(ws), None)
    KICK_RATE_LOCKS.pop(id(ws), None)
    KICK_RATE_STATE.pop(id(ws), None)

def session_sockets(session_id):
    sockets = SESSION_UPSTREAM.get(session_id, {})
    result = []
    seen = set()
    for username, ws in list(sockets.items()):
        if ws is None or getattr(ws, "closed", False):
            continue
        if id(ws) in seen:
            continue
        seen.add(id(ws))
        result.append((username, ws))
    return result

def all_active_ws_usernames(exclude_session=None):
    names = set()
    for sid, sockets in list(SESSION_UPSTREAM.items()):
        if exclude_session is not None and sid == exclude_session:
            continue
        for name, ws in list(sockets.items()):
            if ws is not None and not getattr(ws, "closed", False):
                names.add(str(name).strip().casefold())
    return names


async def send_upstream(ws, payload):
    lock = SOCKET_LOCKS.setdefault(id(ws), asyncio.Lock())
    async with lock:
        if ws.closed:
            return False
        await ws.send_json(payload)
        return True

async def send_kick(ws, room, target):
    """Send one room.kick while enforcing the configured per-socket rate.

    The limiter is per upstream WebSocket and shared by all callers, so
    concurrent KICK ALL requests and SUICIDE cannot bypass the limit.
    """
    wsid = id(ws)
    rate_lock = KICK_RATE_LOCKS.setdefault(wsid, asyncio.Lock())
    state = KICK_RATE_STATE.setdefault(wsid, {"window_start": 0.0, "count": 0})

    while True:
        async with rate_lock:
            if ws.closed:
                return False
            now = asyncio.get_running_loop().time()
            if state["window_start"] <= 0 or now - state["window_start"] >= 1.0:
                state["window_start"] = now
                state["count"] = 0
            if state["count"] < KICK_MAX_PER_SECOND:
                state["count"] += 1
                break
            wait_for = max(0.001, 1.0 - (now - state["window_start"]))
        await asyncio.sleep(wait_for)

    return await send_upstream(ws, {
        "type": "room.kick",
        "room": room,
        "target_username": target,
    })

@require_auth
async def sandbox_kick(request):
    """Local-only kick workload simulator. Never opens or writes an upstream WebSocket."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON tidak valid"}, status=400)
    targets = []
    seen_targets = set()
    for raw in data.get("targets", []):
        target = str(raw).strip()
        key = target.casefold()
        if target and key not in seen_targets:
            seen_targets.add(key)
            targets.append(target)
        if len(targets) >= 10:
            break
    session_id = request.get("session_id")
    ws_usernames = {str(name).strip().casefold() for name, _ws in session_sockets(session_id)}
    targets = [t for t in targets if t.casefold() not in ws_usernames][:10]
    try:
        loops = max(1, min(100, int(data.get("loop", 1))))
        sockets_count = max(1, min(10, int(data.get("sockets", 10))))
        delay_target = max(0, int(data.get("delayTarget", 0))) / 1000
        delay_batch = max(0, int(data.get("delayBatch", 0))) / 1000
    except Exception:
        return web.json_response({"ok": False, "error": "Parameter sandbox tidak valid"}, status=400)
    if not targets:
        return web.json_response({"ok": False, "error": "TARGET wajib diisi untuk sandbox"}, status=400)

    job_id = "sandbox-" + uuid.uuid4().hex[:12]
    total_jobs = loops * len(targets) * sockets_count
    socket_reports = {
        f"SANDBOX-{i+1}": {
            "totalJobs": loops * len(targets),
            "dispatchedJobs": 0, "failedJobs": 0,
            "lastTarget": "", "lastLoop": 0
        }
        for i in range(sockets_count)
    }

    await publish_kick_progress(session_id, {
        "jobId": job_id, "phase": "started", "sandbox": True,
        "totalJobs": total_jobs, "dispatchedJobs": 0, "failedJobs": 0,
        "websockets": sockets_count, "targets": len(targets), "loop": loops,
        "burst": 0, "combo": "sandbox",
        "socketReports": [{"websocket": n, **v} for n, v in socket_reports.items()]
    })

    dispatched = 0
    started = asyncio.get_running_loop().time()
    for loop_no in range(1, loops + 1):
        for target in targets:
            for socket_name, stats in socket_reports.items():
                # Deliberately no upstream/network call: this is a local simulation only.
                await asyncio.sleep(0)
                stats["dispatchedJobs"] += 1
                stats["lastTarget"] = target
                stats["lastLoop"] = loop_no
                dispatched += 1
                await publish_kick_progress(session_id, {
                    "jobId": job_id, "phase": "progress", "sandbox": True,
                    "totalJobs": total_jobs, "dispatchedJobs": dispatched,
                    "failedJobs": 0, "websockets": sockets_count,
                    "target": target, "loop": loop_no, "websocket": socket_name,
                    "socketStats": dict(stats)
                })
                if delay_target:
                    await asyncio.sleep(delay_target)
        if loop_no < loops and delay_batch:
            await asyncio.sleep(delay_batch)

    elapsed = max(0.000001, asyncio.get_running_loop().time() - started)
    await publish_kick_progress(session_id, {
        "jobId": job_id, "phase": "done", "sandbox": True,
        "totalJobs": total_jobs, "dispatchedJobs": dispatched, "failedJobs": 0,
        "websockets": sockets_count, "elapsedMs": round(elapsed * 1000, 3),
        "socketReports": [{"websocket": n, **v} for n, v in socket_reports.items()]
    })
    return web.json_response({
        "ok": True, "sandbox": True, "jobId": job_id, "totalJobs": total_jobs,
        "websockets": sockets_count, "elapsedMs": round(elapsed * 1000, 3)
    })


async def publish_browser_event(session_id, payload):
    """Send authoritative state only to browsers belonging to the same login session."""
    if not isinstance(payload, dict) or not session_id:
        return
    browsers = SESSION_BROWSERS.get(session_id, set())
    dead = []
    for browser in list(browsers):
        if browser.closed:
            dead.append(browser)
            continue
        try:
            await browser.send_json(payload)
        except Exception:
            dead.append(browser)
    for browser in dead:
        browsers.discard(browser)
        BROWSER_CLIENTS.discard(browser)
        BROWSER_SESSION.pop(id(browser), None)


async def publish_kick_progress(session_id, payload):
    """Send backend kick progress only to the browser session that owns the job."""
    if not session_id:
        return
    dead = []
    message = {"type": "kick.progress", **payload}
    browsers = SESSION_BROWSERS.get(session_id, set())
    for browser in list(browsers):
        if browser.closed:
            dead.append(browser)
            continue
        try:
            await browser.send_json(message)
        except Exception:
            dead.append(browser)
    for browser in dead:
        browsers.discard(browser)
        BROWSER_CLIENTS.discard(browser)
        BROWSER_SESSION.pop(id(browser), None)


@require_auth
async def kick_loop(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON tidak valid"}, status=400)
    room = str(data.get("room", "")).strip()
    targets = [str(x).strip() for x in data.get("targets", []) if str(x).strip()][:10]
    try:
        loops = max(1, int(data.get("loop", 1)))
        burst = min(10, max(1, int(data.get("burst", 1))))
        combo = str(data.get("combo", "off")).strip().lower()
        if combo not in {"off", "combo1", "combo2"}:
            combo = "off"
        if combo in {"combo1", "combo2"}:
            burst = 0
        delay_target = max(0, int(data.get("delayTarget", 0))) / 1000
        delay_batch = max(0, int(data.get("delayBatch", 0))) / 1000
    except Exception:
        return web.json_response({"ok": False, "error": "Parameter kick tidak valid"}, status=400)
    if not room or not targets:
        return web.json_response({"ok": False, "error": "Room dan TARGET wajib diisi"}, status=400)
    session_id = request.get("session_id")
    socket_entries = session_sockets(session_id)
    own_ws_names = {name.casefold() for name, _ws in socket_entries}
    targets = [t for t in targets if t.casefold() not in own_ws_names][:10]
    if not socket_entries:
        return web.json_response({"ok": False, "error": "Tidak ada WebSocket yang aktif"}, status=409)
    if not targets:
        return web.json_response({"ok": False, "error": "Semua target termasuk WebSocket milik session ini"}, status=400)

    room_key = _norm_room_key(room)
    job_key = (session_id, room_key)
    active = ACTIVE_KICK_JOBS.get(job_key, [])
    if active or job_key in ACTIVE_KICK_TASKS:
        active_ids = [x for x in ACTIVE_KICK_TASKS.get(job_key, [])] if isinstance(ACTIVE_KICK_TASKS.get(job_key), list) else []
        return web.json_response({"ok": False, "error": "Masih ada job aktif untuk room ini", "active": True, "jobIds": active_ids}, status=409)

    # The backend owns the job lifecycle. The HTTP request only starts the job;
    # progress and completion are delivered through the persistent browser WS.
    job_id = uuid.uuid4().hex[:12]
    ACTIVE_KICK_TASKS[job_key] = [job_id]
    task = asyncio.create_task(_run_kick_job(
        job_id, session_id, room, list(targets), socket_entries, loops, burst, combo,
        delay_target, delay_batch
    ))
    ACTIVE_KICK_TASK_HANDLES.setdefault(job_key, {})[job_id] = task
    def _job_done(_task):
        current = ACTIVE_KICK_TASKS.get(job_key, [])
        if job_id in current:
            current.remove(job_id)
        if not current:
            ACTIVE_KICK_TASKS.pop(job_key, None)
        handles = ACTIVE_KICK_TASK_HANDLES.get(job_key, {})
        handles.pop(job_id, None)
        if not handles:
            ACTIVE_KICK_TASK_HANDLES.pop(job_key, None)
        # A failed worker must not leave a stale job in this user's room.
        active_states = ACTIVE_KICK_JOBS.get(job_key, [])
        if active_states:
            ACTIVE_KICK_JOBS[job_key] = [state for state in active_states if state.get("jobId") != job_id]
            if not ACTIVE_KICK_JOBS[job_key]:
                ACTIVE_KICK_JOBS.pop(job_key, None)
    task.add_done_callback(_job_done)
    return web.json_response({
        "ok": True, "accepted": True, "jobId": job_id,
        "websockets": len(socket_entries), "targets": len(targets),
        "loop": loops, "burst": burst, "combo": combo,
        "transport": "browser-ws-events"
    }, status=202)

async def _run_kick_job(job_id, session_id, room, targets, socket_entries, loops, burst, combo, delay_target, delay_batch):
        pattern = PatternEngine(targets, burst=burst, combo=combo)
        total_jobs = loops * pattern.jobs_per_socket() * len(socket_entries)
        per_socket_total = loops * pattern.jobs_per_socket()
        socket_stats = {
            ws_name: {"totalJobs": per_socket_total, "dispatchedJobs": 0, "failedJobs": 0, "lastTarget": "", "lastLoop": 0}
            for ws_name, _ in socket_entries
        }

        def socket_reports():
            return [
                {"websocket": ws_name, **stats}
                for ws_name, stats in socket_stats.items()
            ]

        progress = {"jobId": job_id, "phase": "started", "totalJobs": total_jobs,
                    "dispatchedJobs": 0, "failedJobs": 0, "websockets": len(socket_entries),
                    "targets": len(targets), "loop": loops, "burst": burst, "combo": combo,
                    "kickLimit": KICK_LIMIT_LABEL, "socketReports": socket_reports()}
        await publish_kick_progress(session_id, progress)

        total = 0
        failed = 0
        progress_lock = asyncio.Lock()

        room_key = _norm_room_key(room)
        # Immutable replacement source: exactly the original target pool (max 10).
        # BRUTE/COMBO still decide the normal dispatch pattern; replacement only
        # reacts to a confirmed kicked event and never creates a new target.
        replacement_state = {
            "jobId": job_id,
            "sessionId": session_id,
            "targets": list(targets),
            "kicked": set(),
            "claimed": set(),
            "events": {t.casefold(): asyncio.Event() for t in targets},
            "replacement_tasks": set(),
            "replacement_dispatched": 0,
            "lock": asyncio.Lock(),
        }

        async def dispatch_replacement(kicked_target):
            async with replacement_state["lock"]:
                kicked_key = str(kicked_target).strip().casefold()
                candidates = [
                    t for t in replacement_state["targets"]
                    if t.casefold() not in replacement_state["kicked"]
                    and t.casefold() != kicked_key
                    and t.casefold() not in replacement_state["claimed"]
                ]
                if not candidates:
                    return
                replacement = candidates[0]
                replacement_state["claimed"].add(replacement.casefold())
                replacement_state["replacement_dispatched"] += len(socket_entries)

            if delay_target:
                await asyncio.sleep(delay_target)

            # Replacement uses the same active WS set as the selected BRUTE/COMBO
            # job. It does not introduce another brute/combo pattern.
            jobs = [
                run_one(ws, ws_name, replacement, 0, is_replacement=True)
                for ws_name, ws in socket_entries
            ]
            await asyncio.gather(*jobs, return_exceptions=True)

        replacement_state["on_kicked"] = dispatch_replacement
        ACTIVE_KICK_JOBS.setdefault((session_id, room_key), []).append(replacement_state)

        async def run_one(ws, ws_name, target, loop_no, is_replacement=False):
            nonlocal total, failed
            current_target = target

            while current_target:
                target_key = str(current_target).strip().casefold()

                # Never send another kick to a target already confirmed as kicked.
                state = replacement_state
                async with state["lock"]:
                    if target_key in state["kicked"]:
                        return

                try:
                    result = await send_kick(ws, room, current_target)
                except Exception:
                    result = False

                async with progress_lock:
                    if result is True:
                        total += 1
                        socket_stats[ws_name]["dispatchedJobs"] += 1
                    else:
                        failed += 1
                        socket_stats[ws_name]["failedJobs"] += 1
                    socket_stats[ws_name]["lastTarget"] = current_target
                    socket_stats[ws_name]["lastLoop"] = loop_no + 1
                    current_total, current_failed = total, failed
                    current_socket = dict(socket_stats[ws_name])

                await publish_kick_progress(session_id, {
                    "jobId": job_id, "phase": "progress",
                    "totalJobs": total_jobs, "dispatchedJobs": current_total,
                    "failedJobs": current_failed,
                    "websockets": len(socket_entries),
                    "loop": loop_no + 1, "loops": loops,
                    "target": current_target, "websocket": ws_name,
                    "replacement": bool(is_replacement),
                    "kickedTargets": len(state["kicked"]),
                    "socketStats": {"websocket": ws_name, **current_socket},
                })

                # Replacement is event-driven from mark_confirmed_kick(). Do not
                # guess that a target was kicked merely because send_kick succeeded.
                return

        for loop_no in range(loops):
            for wave_index, wave in enumerate(pattern.waves()):
                jobs = [
                    run_one(ws, ws_name, target, loop_no)
                    for ws_name, ws in socket_entries
                    for target in wave
                ]
                if jobs:
                    await asyncio.gather(*jobs, return_exceptions=True)
                if wave_index + 1 < pattern.wave_count() and delay_target:
                    await asyncio.sleep(delay_target)
            if loop_no + 1 < loops and delay_batch:
                await asyncio.sleep(delay_batch)

        # Wait for replacement chains already triggered by confirmed events. A
        # replacement may itself be kicked and therefore schedule another one.
        while replacement_state.get("replacement_tasks"):
            pending = list(replacement_state["replacement_tasks"])
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        job_key = (session_id, room_key)
        active_jobs = ACTIVE_KICK_JOBS.get(job_key, [])
        if replacement_state in active_jobs:
            active_jobs.remove(replacement_state)
        if not active_jobs:
            ACTIVE_KICK_JOBS.pop(job_key, None)

        reports = socket_reports()
        await publish_kick_progress(session_id, {
            "jobId": job_id, "phase": "done", "totalJobs": total_jobs,
            "dispatchedJobs": total, "failedJobs": failed,
            "websockets": len(socket_entries), "targets": len(targets),
            "loop": loops, "burst": burst, "combo": combo,
            "replacement": True, "kickedTargets": len(replacement_state["kicked"]),
            "replacementDispatched": replacement_state["replacement_dispatched"],
            "kickLimit": KICK_LIMIT_LABEL,
            "socketReports": reports
        })
        return None

@require_auth
async def reset_troop_session(request):
    """Clear troop-side jobs/state without logging the browser application out."""
    session_id = request.get("session_id")
    cancelled = 0
    # Remove job state first so no late completion event can revive an old job.
    for job_key in list(ACTIVE_KICK_JOBS.keys()):
        if job_key[0] == session_id:
            ACTIVE_KICK_JOBS.pop(job_key, None)
    for job_key in list(ACTIVE_KICK_TASK_HANDLES.keys()):
        if job_key[0] != session_id:
            continue
        handles = ACTIVE_KICK_TASK_HANDLES.pop(job_key, {})
        for task in list(handles.values()):
            if task and not task.done():
                task.cancel()
                cancelled += 1
        ACTIVE_KICK_TASKS.pop(job_key, None)
    SESSION_SUICIDE_KEYS.pop(session_id, None)
    # Close every upstream belonging to this troop session so the next LOGIN ALL
    # always starts with fresh developer sessions.
    sockets = list((SESSION_UPSTREAM.get(session_id) or {}).values())
    for ws in sockets:
        try:
            if ws is not None and not ws.closed:
                await ws.close(code=1000, message=b"troop reset")
        except Exception:
            pass
    SESSION_UPSTREAM.pop(session_id, None)
    return web.json_response({"ok": True, "cancelledJobs": cancelled, "closedSockets": len(sockets), "session": "troop-reset"})

@require_auth
async def suicide(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON tidak valid"}, status=400)
    room = str(data.get("room", "")).strip()
    target = str(data.get("target_username", "")).strip()
    key = str(data.get("key", "")).strip()
    if not room or not target:
        return web.json_response({"ok": False, "error": "Room dan target wajib diisi"}, status=400)
    session_id = request.get("session_id")
    suicide_keys = SESSION_SUICIDE_KEYS.setdefault(session_id, set())
    if key and key in suicide_keys:
        return web.json_response({"ok": True, "duplicate": True, "websockets": 0})
    if key:
        suicide_keys.add(key)
        if len(suicide_keys) > 200:
            suicide_keys.clear()
    sockets = [ws for _name, ws in session_sockets(session_id)]
    jobs = [send_kick(ws, room, target) for ws in sockets]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    sent = sum(1 for x in results if x is True)
    return web.json_response({"ok": True, "websockets": sent, "target": target, "kickLimit": KICK_LIMIT_LABEL})




def require_admin(handler):
    @wraps(handler)
    async def wrapped(request):
        user = _current_user(request)
        if not user:
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "Login diperlukan"}, status=401)
            raise web.HTTPFound("/login")
        if user.get("role") != "admin":
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "Akses admin diperlukan"}, status=403)
            raise web.HTTPForbidden(text="Akses admin diperlukan")
        request["user"] = user
        request["session_id"] = request.cookies.get(SESSION_COOKIE)
        return await handler(request)
    return wrapped


async def login_page(request):
    return web.FileResponse(ROOT / "login.html")


async def app_page(request):
    return web.FileResponse(ROOT / "index.html")


async def admin_page(request):
    return web.FileResponse(ROOT / "admin.html")


async def auth_login(request):
    try:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
    except Exception:
        return web.json_response({"ok": False, "error": "JSON tidak valid"}, status=400)
    if not username or not password:
        return web.json_response({"ok": False, "error": "Username dan password wajib diisi"}, status=400)
    user = authenticate(username, password)
    if not user:
        return web.json_response({"ok": False, "error": "Username atau password salah"}, status=401)
    token = create_session(user)
    response = web.json_response({"ok": True, "user": user})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=12 * 60 * 60, httponly=True,
        samesite="Lax", secure=request.scheme == "https", path="/"
    )
    return response


async def auth_logout(request):
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def auth_me(request):
    user = _current_user(request)
    if not user:
        return web.json_response({"ok": False, "authenticated": False}, status=401)
    return web.json_response({"ok": True, "authenticated": True, "user": user})


@require_admin
async def admin_change_password(request):
    try:
        data = await request.json()
        change_password(request["user"]["username"], str(data.get("password", "")))
        return web.json_response({"ok": True})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


@require_admin
async def admin_users(request):
    return web.json_response({"ok": True, "users": list_users()})


@require_admin
async def admin_create_user(request):
    try:
        data = await request.json()
        create_user(str(data.get("username", "")), str(data.get("password", "")), "user")
        return web.json_response({"ok": True})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        return web.json_response({"ok": False, "error": "Data user tidak valid"}, status=400)


@require_admin
async def admin_set_user_enabled(request):
    username = request.match_info.get("username", "")
    try:
        data = await request.json()
        set_enabled(username, bool(data.get("enabled")))
        return web.json_response({"ok": True})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


@require_admin
async def admin_delete_user(request):
    username = request.match_info.get("username", "")
    try:
        delete_user(username)
        return web.json_response({"ok": True})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

async def index(request):
    if _current_user(request):
        raise web.HTTPFound("/app")
    raise web.HTTPFound("/login")

@require_auth
async def proxy(request):
    browser = web.WebSocketResponse(heartbeat=20)
    await browser.prepare(request)
    session_id = request.get("session_id") or request.cookies.get(SESSION_COOKIE)
    BROWSER_CLIENTS.add(browser)
    BROWSER_SESSION[id(browser)] = session_id
    SESSION_BROWSERS.setdefault(session_id, set()).add(browser)
    upstream = None
    session = None
    relay_task = None
    try:
        session = ClientSession()
        upstream_username = None
        # No receive timeout: the API is a long-lived WebSocket and has its own JSON ping rule.
        ws_timeout = ClientWSTimeout(ws_close=10, ws_receive=None)
        last_error = None
        for attempt in range(1, 3):
            try:
                upstream = await session.ws_connect(
                    API_URL,
                    protocols=(SUBPROTOCOL,),
                    heartbeat=20,
                    autoping=True,
                    timeout=ws_timeout,
                )
                print(f"UPSTREAM CONNECTED attempt={attempt} protocol={upstream.protocol!r} status={getattr(upstream, "_response", None).status if getattr(upstream, "_response", None) else "?"}", flush=True)
                break
            except Exception as e:
                last_error = e
                print(f"UPSTREAM CONNECT FAILED attempt={attempt}: {type(e).__name__}: {e!r}", flush=True)
                if attempt < 2:
                    await asyncio.sleep(0.5)
        if upstream is None:
            detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
            payload = {"type":"proxy.error","data":{"stage":"upstream_connect","message":detail[:500]}}
            if not browser.closed:
                await browser.send_json(payload)
                await browser.close(code=1011, message=b"upstream connection failed")
            return browser

        async def upstream_to_browser():
            async for msg in upstream:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = __import__("json").loads(msg.data)
                        confirmed = await mark_confirmed_kick(payload, session_id)
                        if confirmed:
                            await publish_browser_event(session_id, {
                                "type": "kick.target.confirmed",
                                "room": confirmed["room"],
                                "target_username": confirmed["target"],
                                "source_type": confirmed["type"],
                            })
                    except Exception:
                        pass
                    await browser.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await browser.send_bytes(msg.data)
                elif msg.type == WSMsgType.PING:
                    await upstream.pong()
                elif msg.type == WSMsgType.PONG:
                    continue
                elif msg.type == WSMsgType.CLOSE:
                    print(f"UPSTREAM CLOSE code={upstream.close_code}", flush=True)
                    break
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    print(f"UPSTREAM END type={msg.type} exception={upstream.exception()!r}", flush=True)
                    break

        relay_task = asyncio.create_task(upstream_to_browser())

        async for msg in browser:
            if msg.type == WSMsgType.TEXT:
                if upstream.closed:
                    await browser.send_json({"type":"proxy.error","data":{"stage":"upstream_send","message":"upstream socket is closed"}})
                    break
                try:
                    payload = __import__("json").loads(msg.data)
                    if payload.get("type") == "developer.login":
                        upstream_username = str(payload.get("username", "")).strip() or None
                        register_socket(session_id, upstream_username, upstream)
                except Exception:
                    pass
                await upstream.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                if upstream.closed:
                    break
                await upstream.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    except Exception as e:
        print(f"WebSocket proxy error: {type(e).__name__}: {e!r}", flush=True)
        if not browser.closed:
            try:
                await browser.send_json({"type":"proxy.error","data":{"stage":"proxy","message":f"{type(e).__name__}: {e}"[:500]}})
            except Exception:
                pass
            await browser.close(code=1011, message=b"proxy error")
    finally:
        BROWSER_CLIENTS.discard(browser)
        browsers = SESSION_BROWSERS.get(session_id, set())
        browsers.discard(browser)
        if not browsers and session_id:
            SESSION_BROWSERS.pop(session_id, None)
        BROWSER_SESSION.pop(id(browser), None)
        if relay_task is not None:
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)
        if upstream is not None:
            unregister_socket(session_id, upstream_username, upstream)
            if not upstream.closed:
                await upstream.close()
        if session is not None:
            await session.close()
    return browser

async def health(request):
    return web.json_response({
        "ok": True,
        "service": "migsock",
        "port": PORT,
        "upstream": API_URL,
        "ws_path": "/ws",
    })

init_auth_db()

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/login", login_page)
app.router.add_get("/app", require_auth(app_page))
app.router.add_get("/admin", require_admin(admin_page))
app.router.add_post("/api/auth/login", auth_login)
app.router.add_post("/api/auth/logout", require_auth(auth_logout))
app.router.add_get("/api/auth/me", auth_me)
app.router.add_get("/api/admin/users", admin_users)
app.router.add_post("/api/admin/password", admin_change_password)
app.router.add_post("/api/admin/users", admin_create_user)
app.router.add_post("/api/admin/users/{username}/enabled", admin_set_user_enabled)
app.router.add_delete("/api/admin/users/{username}", admin_delete_user)
app.router.add_get("/health", health)
app.router.add_get("/ws", proxy)
app.router.add_post("/api/kick-loop", kick_loop)
app.router.add_post("/api/sandbox-kick", sandbox_kick)
app.router.add_post("/api/troop/reset", reset_troop_session)
app.router.add_post("/api/suicide", suicide)
app.router.add_static("/static/", ROOT)

if __name__ == "__main__":
    print(f"migsock listening on 0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT)
