"""
"Brief me": a 30-second spoken summary of the data's health, built from the same numbers the pages show
(no AI model, so every number is exact). Returns sentences for the screen and the same sentences written for speech
(rupees and dates spelled out the way a person says them).
"""
import re
from datetime import date, datetime

import data_service as ds

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def say_inr(v) -> str:
    v = float(v or 0)
    sign = "minus " if v < 0 else ""
    v = abs(v)
    if v >= 1e7:
        return f"{sign}{v / 1e7:.2f} crore rupees".replace(".00 ", " ")
    if v >= 1e5:
        return f"{sign}{v / 1e5:.2f} lakh rupees".replace(".00 ", " ")
    return f"{sign}{round(v):,} rupees"


def say_date(d) -> str:
    try:
        d = date.fromisoformat(str(d)[:10])
        return f"{d.day} {MONTHS[d.month - 1]}"
    except ValueError:
        return str(d)


def _ago(ts) -> str:
    try:
        mins = (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds() / 60
    except ValueError:
        return ""
    if mins < 2:
        return "just now"
    if mins < 90:
        return f"{round(mins)} minutes ago"
    if mins < 48 * 60:
        return f"{round(mins / 60)} hours ago"
    return f"{round(mins / 1440)} days ago"


def build() -> dict:
    """[{text, speech}] sentences, most important first."""
    out = []

    def add(text, speech=None):
        spoken = re.sub(r"\b1 ((?:\w+ )?\w+)\(s\)", r"1 \1", speech or text).replace("(s)", "s").replace("_", " ")
        out.append({"text": text, "speech": spoken})

    add("Here is your DataTrust briefing.")                  # the page adds good morning / afternoon (user's own clock)

    t = ds.trust_score()
    top = max(t["reasons"], key=lambda r: r["points"]) if t["reasons"] else None
    add(f"Trust score is {t['score']} out of 100: {t['label'].lower()}."
        + (f" The biggest factor: {top['reason']}." if top else " Nothing is pulling it down."))

    st = ds.pipeline_status()
    batch, layers = st.get("batch") or {}, st.get("layers") or []
    failed = [l for l in layers if l["status"] != "SUCCESS"]
    if layers:
        when = _ago(max((l.get("ended_at") or l.get("started_at") or "") for l in layers))
        add(f"The last pipeline run ({batch.get('batch_id', '')}) " + (
            f"had a problem: {', '.join(l['layer'].lower() + ' ' + l['status'].lower() for l in failed)}." if failed
            else f"succeeded{', ' + when if when else ''}: bronze, silver and gold all loaded."),
            f"The last pipeline run " + (f"had a problem: {', '.join(l['layer'].lower() + ' ' + l['status'].lower() for l in failed)}."
                                         if failed else f"succeeded{', ' + when if when else ''}. Bronze, silver and gold all loaded."))

    inc = ds.incidents()
    high = [i for i in inc if i["severity"] in ("CRITICAL", "HIGH")]
    if not inc:
        add("There are no open incidents.")
    else:
        add(f"{len(inc)} open incident{'s' if len(inc) != 1 else ''}, {len(high)} of them high priority.")
        first = (high or inc)[0]
        if first["type"] == "SETTLEMENT_MISMATCH":
            m, rej = first["metrics"], first.get("rejected_explained") or {}
            add(f"Most urgent: revenue gap on {first['business_date']}: {m['payment_difference']} payments, "
                f"{ds.inr(m['amount_difference'])} settled by the gateway but missing in silver"
                + (f", because they failed rule {rej.get('rules')}." if rej.get("n") else "."),
                f"Most urgent: a revenue gap on {say_date(first['business_date'])}. {m['payment_difference']} payments, "
                f"worth {say_inr(m['amount_difference'])}, were settled by the gateway but are missing in silver"
                + (f", because they failed the {str(rej.get('rules')).replace('_', ' ').lower()} rule." if rej.get("n") else "."))
        else:
            add(f"Most urgent: {first['title']}.", f"Most urgent: {first['title'].replace('_', ' ')}.")

    if ds.table_exists("audit.dq_reprocess"):
        held = ds.one("""SELECT COUNT(*) AS n, COUNT(DISTINCT business_date) AS days,
                                COALESCE(SUM(CASE WHEN entity = 'payments' AND raw_record::jsonb->>'amount' ~ '^-?[0-9.]+$'
                                             THEN (raw_record::jsonb->>'amount')::numeric END), 0) AS amount
                         FROM silver.rejected_records""")
        pend = ds.one("SELECT COUNT(*) AS n FROM audit.dq_reprocess WHERE status = 'PENDING'")["n"]
        if held["n"]:
            add(f"{held['n']} rows are held back by quality rules on {held['days']} date(s)"
                + (f", including {ds.inr(held['amount'])} of payments" if held["amount"] else "") + "."
                + (f" {pend} re-insert request(s) wait for approval." if pend else ""),
                f"{held['n']} rows are held back by quality rules on {held['days']} date{'s' if held['days'] != 1 else ''}"
                + (f", including {say_inr(held['amount'])} of payments" if held["amount"] else "") + "."
                + (f" {pend} re-insert request{'s' if pend != 1 else ''} {'are' if pend != 1 else 'is'} waiting for approval." if pend else ""))
    if ds.table_exists("portal.access_requests"):
        acc = ds.one("SELECT COUNT(*) AS n FROM portal.access_requests WHERE status = 'PENDING'")["n"]
        if acc:
            add(f"{acc} access request(s) wait for an admin.",
                f"{acc} access request{'s are' if acc != 1 else ' is'} waiting for an admin.")

    daily = ds.rows("SELECT business_date::text AS d, gross_revenue AS g FROM gold.daily_revenue ORDER BY 1 DESC LIMIT 2")
    if daily:
        d0 = daily[0]
        change = ""
        if len(daily) > 1 and daily[1]["g"]:
            pct = (float(d0["g"]) - float(daily[1]["g"])) / float(daily[1]["g"]) * 100
            change = f", {'up' if pct >= 0 else 'down'} {abs(pct):.0f}% on the day before"
        add(f"Latest business day {d0['d']}: gross revenue {ds.inr(d0['g'])}{change}.",
            f"On the latest business day, {say_date(d0['d'])}, gross revenue was {say_inr(d0['g'])}{change.replace('%', ' percent')}.")

    if high:
        add("Suggested next step: open Detect and approve the fix for the top incident.")
    elif inc:
        add("Suggested next step: review the open incidents on the Detect page.")
    else:
        add("All good: the data can be trusted today.")
    return {"sentences": out, "generated_at": datetime.now().isoformat(timespec="seconds")}
