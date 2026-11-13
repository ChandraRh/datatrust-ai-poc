"""
Real-time alerts into a Google Chat space (incoming webhook).

Settings (.env locally, secrets in the cloud):
    GOOGLE_CHAT_WEBHOOK_URL   https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...   (a secret: never shown)
    DATATRUST_PUBLIC_URL      the address people open, for the buttons in the card (default http://localhost:8000)
    ALERT_MIN_SEVERITY        LOW / MEDIUM / HIGH / CRITICAL (default MEDIUM)
    ALERT_INTERVAL_SECONDS    how often to check (default 60)

How it works: every interval the server reads the open incidents (the same list as the Detect page).
  * new incident            -> one card in the space (thread per incident), with owner and a button to open it
  * incident gone (fixed)   -> "Resolved" reply in the same thread
  * first time switched on  -> one summary card instead of a burst of old incidents
app.alert_log remembers what was sent; the INSERT ... ON CONFLICT claim makes every alert go out once, even with
several app instances. Without a webhook URL nothing runs and nothing else changes.
"""
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote

import data_service as ds
import appdb

SEVERITY = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
ICON = {"CRITICAL": "🔴", "HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🔵"}
BASELINE = "__baseline__"
MAX_CARDS_PER_CHECK = 5
state = {"last_check": None, "last_error": None, "last_result": None, "running": False}


def _setting(name: str, default: str = "") -> str:
    """The project's .env read NOW (editing it needs no restart), else the environment variable (cloud secret)."""
    value = None
    try:
        from dotenv import dotenv_values
        value = dotenv_values(ds._find_project_root() / ".env").get(name) or None
    except Exception:
        value = None
    if value is None:
        value = os.getenv(name)
    return (value if value is not None else default).strip().strip('"').strip("'")


def _raw_webhook() -> str:
    return _setting("GOOGLE_CHAT_WEBHOOK_URL")


def webhook() -> str:
    url = _raw_webhook()
    return url if re.match(r"^https://chat\.googleapis\.com/v1/spaces/[^/]+/messages\?.*key=.*token=", url) else ""


def problem() -> str:
    """Why alerts are off, in plain words (never shows the URL itself)."""
    raw = _raw_webhook()
    if not raw:
        return (f"GOOGLE_CHAT_WEBHOOK_URL is not set. Add it to {ds._find_project_root() / '.env'} "
                "(saved, one line, no spaces at the start).")
    if raw.startswith("https://chat.google.com/"):
        return "GOOGLE_CHAT_WEBHOOK_URL is the address of the space page, not the webhook. Copy the webhook URL (it starts with https://chat.googleapis.com/v1/spaces/)."
    if not raw.startswith("https://chat.googleapis.com/v1/spaces/"):
        return "GOOGLE_CHAT_WEBHOOK_URL does not start with https://chat.googleapis.com/v1/spaces/ : copy it again with the copy icon."
    if "key=" not in raw or "token=" not in raw:
        return "GOOGLE_CHAT_WEBHOOK_URL is cut off: it must contain both key= and token= (copy the whole URL)."
    return ""


def public_url() -> str:
    return _setting("DATATRUST_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def min_severity() -> str:
    s = _setting("ALERT_MIN_SEVERITY", "MEDIUM").upper()
    return s if s in SEVERITY else "MEDIUM"


def masked() -> str:
    m = re.match(r"^https://chat\.googleapis\.com/v1/spaces/([^/]+)/", webhook())
    return f"space …{m.group(1)[-4:]}" if m else ""


# ---------------------------------------------------------------- sending
def send(payload: dict, thread: str = None) -> None:
    """POST one message to the space. thread groups an incident and its 'Resolved' reply."""
    import requests
    url = webhook()
    if not url:
        raise ValueError("GOOGLE_CHAT_WEBHOOK_URL is not set (or is not a Google Chat webhook URL)")
    if thread:
        url += f"&threadKey={quote(thread)}&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    r = requests.post(url, json=payload, timeout=10, headers={"Content-Type": "application/json; charset=UTF-8"})
    if r.status_code >= 300:
        raise RuntimeError(f"Google Chat answered {r.status_code}: {r.text[:200]}")


def _button(text, url):
    return {"text": text, "onClick": {"openLink": {"url": url}}}


def incident_card(inc: dict, owners: list) -> dict:
    sev = inc["severity"]
    rows = [{"textParagraph": {"text": inc["summary"][:900]}}]
    if inc.get("scope_name"):
        rows.append({"decoratedText": {"topLabel": "Source", "text": str(inc["scope_name"])}})
    if inc.get("business_date"):
        rows.append({"decoratedText": {"topLabel": "Business date", "text": str(inc["business_date"])}})
    rows.append({"decoratedText": {"topLabel": "Owner", "text": ", ".join(f"{o['name']} ({o['email']})" for o in owners[:3])
                                   if owners else "No owner recorded yet (add one on the Connect page)"}})
    rows.append({"buttonList": {"buttons": [_button("Open incident", f"{public_url()}/#investigate={quote(inc['id'])}"),
                                            _button("Impact & fix", f"{public_url()}/#impact={quote(inc['id'])}"),
                                            _button("Contact owner", f"{public_url()}/#estate-people")]}})
    return {"cardsV2": [{"cardId": inc["id"][:60], "card": {
        "header": {"title": f"{ICON.get(sev, '⚪')} {sev} · DataTrust AI", "subtitle": inc["title"][:180]},
        "sections": [{"widgets": rows}]}}]}


def text_message(text: str) -> dict:
    return {"text": text}


def resolved_text(incident_id: str, title: str) -> str:
    """The 'gone' message says why (app.incident_state.resolution): only a real fix is announced as resolved."""
    import resolution
    st = appdb.one("SELECT resolution, resolution_detail, superseded_by FROM app.incident_state WHERE incident_id = %s",
                   (incident_id,)) if appdb.table_exists("app.incident_state") else {}
    reason = st.get("resolution")
    link = f"{public_url()}/#verify"
    if reason == "superseded":
        return f"🔁 *Still failing in a newer run:* {title}\n{st.get('resolution_detail') or ''} {public_url()}/#detect"
    if not reason:
        return f"✅ *No longer reported:* {title}\nNo longer present in the latest data. {link}"
    name, verified = resolution.label(reason)
    if verified:
        return f"✅ *Resolved, {name.lower()}:* {title}\n{st.get('resolution_detail') or ''} {link}"
    return f"⚠️ *No longer reported, not verified ({name.lower()}):* {title}\n{st.get('resolution_detail') or ''} {link}"


# ---------------------------------------------------------------- the check
def _ensure(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS app")
    conn.execute("""CREATE TABLE IF NOT EXISTS app.alert_log (
                        incident_id         TEXT PRIMARY KEY,
                        severity            TEXT,
                        title               TEXT,
                        first_alerted_at    TIMESTAMP DEFAULT now(),
                        resolved_alerted_at TIMESTAMP,
                        last_error          TEXT)""")


def _owners(inc):
    try:
        from estate import store
        return store.match_people(f"{inc['title']} {inc['summary']}")
    except Exception:
        return []


def check_once() -> dict:
    """One round: alert new incidents, announce resolved ones. Safe to call any time (also from 'Check now')."""
    if not webhook():
        return {"configured": False}
    floor = SEVERITY[min_severity()]
    import scopes                                           # every connected source, each incident keeps its source
    current = {i["id"]: i for i in scopes.all_incidents() if SEVERITY.get(i["severity"], 0) >= floor}
    sent_new, sent_resolved, errors = [], [], []
    with appdb.connect() as conn:                          # DataTrust's own database
        _ensure(conn)
        conn.commit()
        known = {r["incident_id"]: r for r in conn.execute("SELECT * FROM app.alert_log").fetchall()}

        if BASELINE not in known:                               # first time: one summary, no burst of old alerts
            if conn.execute("INSERT INTO app.alert_log (incident_id, title) VALUES (%s, 'alerts switched on') "
                            "ON CONFLICT DO NOTHING RETURNING incident_id", (BASELINE,)).fetchone():
                for iid, inc in current.items():
                    conn.execute("INSERT INTO app.alert_log (incident_id, severity, title) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                 (iid, inc["severity"], inc["title"]))
                conn.commit()
                lines = "\n".join(f"{ICON.get(i['severity'], '⚪')} {i['severity']}: {i['title']}" for i in current.values()) or "No open incidents."
                try:
                    send(text_message(f"*DataTrust AI alerts are on* for this space.\nOpen incidents right now ({len(current)}):\n{lines}\n"
                                      f"New incidents will be posted here as they appear. {public_url()}"))
                except Exception as e:
                    errors.append(str(e))
            return {"configured": True, "baseline": True, "open": len(current), "errors": errors}

        new = [i for iid, i in current.items() if iid not in known or known[iid]["resolved_alerted_at"]]
        for inc in new[:MAX_CARDS_PER_CHECK]:
            reopened = inc["id"] in known
            claimed = conn.execute("""INSERT INTO app.alert_log (incident_id, severity, title) VALUES (%s, %s, %s)
                                      ON CONFLICT (incident_id) DO UPDATE SET resolved_alerted_at = NULL, first_alerted_at = now()
                                      WHERE app.alert_log.resolved_alerted_at IS NOT NULL
                                      RETURNING incident_id""", (inc["id"], inc["severity"], inc["title"])).fetchone()
            conn.commit()
            if not claimed:
                continue                                        # another instance sent it
            try:
                card = incident_card(inc, _owners(inc))
                if reopened:
                    card["cardsV2"][0]["card"]["header"]["title"] += " · reopened"
                send(card, thread=inc["id"])
                sent_new.append(inc["id"])
            except Exception as e:                              # not sent: release the claim, retry next round
                conn.execute("DELETE FROM app.alert_log WHERE incident_id = %s AND resolved_alerted_at IS NULL", (inc["id"],))
                conn.commit()
                errors.append(f"{inc['id']}: {e}")
        if len(new) > MAX_CARDS_PER_CHECK:
            rest = new[MAX_CARDS_PER_CHECK:]
            try:
                send(text_message(f"…and {len(rest)} more new incident(s): " + "; ".join(i["title"] for i in rest[:10]) + f" ({public_url()})"))
            except Exception as e:
                errors.append(str(e))

        for iid, row in known.items():
            if iid == BASELINE or iid.startswith("BRIEFING:") or row["resolved_alerted_at"] or iid in current:
                continue
            claimed = conn.execute("""UPDATE app.alert_log SET resolved_alerted_at = now()
                                      WHERE incident_id = %s AND resolved_alerted_at IS NULL RETURNING incident_id""", (iid,)).fetchone()
            conn.commit()
            if not claimed:
                continue
            try:
                send(text_message(resolved_text(iid, row["title"])), thread=iid)
                sent_resolved.append(iid)
            except Exception as e:
                conn.execute("UPDATE app.alert_log SET resolved_alerted_at = NULL WHERE incident_id = %s", (iid,))
                conn.commit()
                errors.append(f"{iid}: {e}")
    return {"configured": True, "new": sent_new, "resolved": sent_resolved, "open": len(current), "errors": errors}


def send_test() -> None:
    send({"cardsV2": [{"cardId": "datatrust-test", "card": {
        "header": {"title": "✅ DataTrust AI is connected", "subtitle": "Test message"},
        "sections": [{"widgets": [{"textParagraph": {"text": f"Alerts of severity <b>{min_severity()}</b> and higher will be posted in this space."}},
                                  {"buttonList": {"buttons": [_button("Open DataTrust AI", public_url())]}}]}]}}]})


def status() -> dict:
    recent = []
    if appdb.table_exists("app.alert_log"):
        recent = appdb.rows("""SELECT incident_id, severity, title, first_alerted_at, resolved_alerted_at FROM app.alert_log
                            WHERE incident_id <> %(b)s ORDER BY first_alerted_at DESC LIMIT 20""", {"b": BASELINE})
    return {"configured": bool(webhook()), "problem": problem(), "running": state["running"], "space": masked(), "min_severity": min_severity(),
            "interval_seconds": interval(), "public_url": public_url(), "last_check": state["last_check"],
            "last_error": state["last_error"], "last_result": state["last_result"], "recent": recent}


# ---------------------------------------------------------------- background loop (started by server.py)
def interval() -> int:
    try:
        return max(15, int(_setting("ALERT_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


def _loop():
    while True:
        if not webhook():
            time.sleep(15)
            continue
        try:
            result = check_once()
            state.update(last_check=datetime.now().isoformat(timespec="seconds"), last_result=result,
                         last_error="; ".join(result.get("errors") or []) or None)
        except Exception as e:                                  # never stop the loop (database restart, network ...)
            state.update(last_check=datetime.now().isoformat(timespec="seconds"), last_error=f"{type(e).__name__}: {e}")
        try:
            daily_briefing()
        except Exception as e:                                  # a briefing problem must never stop the alerts
            state.update(last_error=f"daily briefing: {type(e).__name__}: {e}")
        time.sleep(interval())


# ---------------------------------------------------------------- daily briefing (BRIEFING_CHAT_AT=08:30 in .env)
def briefing_text(b: dict) -> str:
    src = (b.get("facts") or {}).get("source", {}).get("name", "")
    lines = [f"*DataTrust AI briefing · {src}*", b.get("headline") or "", b.get("summary") or ""]
    if b.get("watch"):
        lines.append("Watch today:" + "".join(f"\n• {w}" for w in b["watch"][:3]))
    if b.get("good_news"):
        lines.append("Good news:" + "".join(f"\n• {w}" for w in b["good_news"][:2]))
    lines.append(public_url())
    return "\n".join(x for x in lines if x)


def daily_briefing() -> list:
    """Once a day after BRIEFING_CHAT_AT (local time), one briefing per source, posted once even with several instances."""
    at = _setting("BRIEFING_CHAT_AT")
    if not re.match(r"^\d{1,2}:\d{2}$", at or ""):
        return []
    now = datetime.now()
    h, m = (int(x) for x in at.split(":"))
    if (now.hour, now.minute) < (h, m):
        return []
    import ai
    import scopes
    sent = []
    with appdb.connect() as conn:
        for sc in scopes.list_scopes():
            key = f"BRIEFING:{sc['key']}:{now.date().isoformat()}"
            if not conn.execute("INSERT INTO app.alert_log (incident_id, title) VALUES (%s, 'daily briefing') "
                                "ON CONFLICT DO NOTHING RETURNING incident_id", (key,)).fetchone():
                continue                                         # already posted today (here or by another instance)
            conn.commit()
            try:
                send(text_message(briefing_text(ai.briefing(sc["key"]))))
                sent.append(sc["key"])
            except Exception:
                conn.execute("DELETE FROM app.alert_log WHERE incident_id = %s", (key,))
                conn.commit()
                raise
    return sent


def start_background() -> bool:
    if state["running"] or _setting("ALERTS_ENABLED", "true").lower() == "false":
        return False
    state["running"] = True
    threading.Thread(target=_loop, name="datatrust-alerts", daemon=True).start()
    return True
