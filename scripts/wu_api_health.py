"""Circuit breaker persistente per le API Weather Underground.

I workflow di aggiornamento e discovery condividono questa stessa memoria,
così un blocco quota interrompe entrambi fino al prossimo controllo.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("data/wunderground_app_api_health.json")
UTC = timezone.utc
BLOCK_HTTP = {401, 403, 429}


def utc_now():
    return datetime.now(UTC)


def stamp(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# Probe sparse (8h, 16h, then 24h): an expired quota must not
# black out the map for another full day without even one retry.
# Also recalculate older 24h cooldowns written before this change.
COOLDOWN_HOURS = (8, 16, 24)


def in_cooldown(state, now=None):
    until = parse_stamp(state.get("blocked_until"))
    failed_at = parse_stamp(state.get("last_failure_at"))
    try:
        count = int(state.get("consecutive_throttles") or 0)
    except (ValueError, TypeError):
        count = 0
    if until and failed_at and count > 0 and state.get("last_http_status") in BLOCK_HTTP:
        effective = failed_at + timedelta(hours=COOLDOWN_HOURS[min(count - 1, 2)])
        until = min(until, effective)
    return bool(until and (now or utc_now()) < until)


def record_failure(state, status, reason=""):
    now = utc_now()
    state = dict(state)
    consecutive = min(10, int(state.get("consecutive_throttles", 0) or 0) + 1)
    # Una sola prova dopo 8h, poi 16h e 24h se il blocco persiste.
    # Nessuna interrogazione ripetuta durante il periodo di protezione.
    wait_h = COOLDOWN_HOURS[min(consecutive - 1, 2)]
    state.update(
        status="cooldown",
        last_http_status=int(status or 0),
        last_error=str(reason)[:160],
        last_failure_at=stamp(now),
        blocked_until=stamp(now + timedelta(hours=wait_h)),
        consecutive_throttles=consecutive,
    )
    return state


def record_success(state):
    state = dict(state)
    state.update(status="active", blocked_until=None, consecutive_throttles=0,
                 last_success_at=stamp(utc_now()), last_http_status=None, last_error=None)
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n",
                          encoding="utf-8")
