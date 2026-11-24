"""Made-up repair history for the demo, so the report and queues have something to show.

About four months of RMAs (tickets 6001 and up) moving through every stage:
most shipped, delivered and reported, the rest open at each stage, a few of
them waiting a long time. Only used by tests/demo_server.py, only on an empty
database, and never on tickets 7101-7104 (those are left for visitors to add).

The tickets are generated the same way on every start (fixed random seed), so
the fake Zendesk can answer for them whether or not this run did the seeding.
Times are relative to now.
"""

import random
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb
from werkzeug.security import generate_password_hash

FIRST_TICKET = 6001
DAYS = 120
REPORTS_WAITING = 3  # newest shipped RMAs left in the Final reports queue

# Past techs, so "Work by person" has more than one bar. Added inactive with a
# random PIN: they never show up on the sign-in screen.
SAMPLE_TECHS = ("Kyler Grant", "Maria Lopez", "Jess Tran")

FIRST = ("Sam", "Lee", "Dana", "Chris", "Pat", "Jordan", "Alex", "Morgan", "Casey", "Riley", "Taylor",
         "Jamie", "Drew", "Avery", "Quinn", "Reese", "Blake", "Cameron", "Hayden", "Logan")
LAST = ("Ortiz", "Park", "Wells", "Moore", "Nguyen", "Fischer", "Hall", "Brooks", "Reyes", "Price",
        "Kim", "Shaw", "Bennett", "Cole", "Hughes", "Lund", "Ramos", "Stone", "Ward", "Young")
RIFLES = (("Ridgeline FFT", ("6.5 Creedmoor", "6.5 PRC", "7mm PRC", ".300 Win Mag")),
          ("Mesa", ("6.5 Creedmoor", ".308 Win", "7mm Rem Mag")),
          ("MPR", ("6.5 Creedmoor", "6.5 PRC", ".308 Win")),
          ("Traverse", ("6.5 PRC", "7mm PRC", ".300 PRC")),
          ("Evoke", (".270 Win", ".30-06", "6.5 Creedmoor")))
# (subject, what the customer wrote, problem labels, solution labels, root cause label)
CASES = (
    ("Won't group under 2 MOA", "The rifle opened up to 2.5-3 MOA with the ammo that used to shoot under 1 MOA.",
     ("Accuracy / will not group",), ("Re-bedded action",), "Manufacturing defect: machining / assembly"),
    ("Accuracy fell off after a season", "Groups have gotten bigger every trip this fall. Scope checked fine.",
     ("Accuracy / will not group", "Stock or bedding"), ("Re-bedded action",), "Normal wear"),
    ("Bolt sticking on extraction", "The bolt is stiff to lift after firing and a case stayed in the chamber twice.",
     ("Feeding or extraction", "Bolt or action"), ("Replaced part",), "Component or supplier part failure"),
    ("Failure to feed from magazine", "Rounds nose-dive into the feed ramp about one time in five.",
     ("Feeding or extraction",), ("Replaced part",), "Component or supplier part failure"),
    ("Trigger creep", "Trigger used to break clean at about 3 lb, now it creeps and feels heavier.",
     ("Trigger",), ("Adjusted trigger",), "Normal wear"),
    ("Trigger pull too heavy from the factory", "Brand new rifle, trigger feels like 5 lb or more.",
     ("Trigger",), ("Adjusted trigger",), "Manufacturing defect: machining / assembly"),
    ("Stock finish peeling", "The finish is peeling near the grip after one season.",
     ("Finish / cosmetic",), ("Refinished",), "Component or supplier part failure"),
    ("Cerakote wearing on the barrel", "Finish is wearing through where the rifle rides in the truck rack.",
     ("Finish / cosmetic",), ("Refinished",), "Normal wear"),
    ("Cracked stock after a fall", "Slipped on a ridge and the stock cracked behind the action.",
     ("Stock or bedding",), ("Replaced part",), "Customer damage or modification"),
    ("Arrived with a cracked stock", "The rifle arrived from the dealer with a crack in the forend.",
     ("Stock or bedding",), ("Replaced part",), "Shipping damage"),
    ("Barrel fouling fast, accuracy drops", "Copper builds up fast and accuracy drops after 20 rounds.",
     ("Accuracy / will not group",), ("Replaced barrel",), "Component or supplier part failure"),
    ("Pressure signs with factory ammo", "Flattened primers and sticky bolt lift with one brand of ammo.",
     ("Bolt or action",), ("Cleaned and test fired, no fault found",), "Ammunition related"),
    ("Please check over before elk season", "No specific problem, just want it checked before a big hunt.",
     ("No fault found",), ("Cleaned and test fired, no fault found",), "No fault found"),
    ("Safety hard to move", "The safety is very stiff to move off safe.",
     ("Trigger",), ("Replaced part",), "Manufacturing defect: machining / assembly"),
)
CARRIERS = (("UPS", "1Z999AA1{:010d}"), ("FedEx", "7{:011d}"), ("USPS", "94001118992{:011d}"))


def _name(rng):
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def sample_tickets():
    """ticket_id -> dict(subject, name, complaint, model, caliber, serial, inbound): the same every run."""
    rng = random.Random(4242)
    out = {}
    for i in range(150):
        case = rng.randrange(len(CASES))
        model, calibers = rng.choice(RIFLES)
        out[FIRST_TICKET + i] = {
            "case": case,
            "subject": CASES[case][0],
            "complaint": CASES[case][1],
            "name": _name(rng),
            "model": model,
            "caliber": rng.choice(calibers),
            "serial": f"CA{rng.randint(10000, 99999)}",
            "inbound": "1Z999AA1{:010d}".format(rng.randint(0, 10**10 - 1)),
        }
    return out


def seed(dsn: str) -> int:
    """Fill an EMPTY repair database with sample history. Returns how many RMAs were added (0 if not empty)."""
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    tickets = sample_tickets()
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        if cur.execute("SELECT count(*) FROM repair.rma").fetchone()[0]:
            return 0

        techs = []
        for name in SAMPLE_TECHS:
            row = cur.execute(
                "INSERT INTO repair.tech (name, pin_hash, active) VALUES (%s, %s, false) "
                "ON CONFLICT (lower(name)) DO UPDATE SET name = EXCLUDED.name RETURNING id, name",
                (name, generate_password_hash(secrets.token_hex(16))),
            ).fetchone()
            techs.append(row)
        demo = cur.execute("SELECT id, name FROM repair.tech WHERE active ORDER BY id LIMIT 1").fetchone()
        if demo:
            techs.append(demo)

        def choice_ids(kind):
            return {label: cid for cid, label in cur.execute(
                "SELECT id, label FROM repair.choice WHERE kind = %s AND active", (kind,)).fetchall()}

        problems, solutions, causes = choice_ids("problem"), choice_ids("solution"), choice_ids("root_cause")
        points = list(choice_ids("inspection_point"))
        accessories = choice_ids("accessory")

        def at_work(day_start):
            """A time during the shop day (8am-5pm Mountain is 15:00-24:00 UTC)."""
            return day_start.replace(hour=rng.randint(15, 23), minute=rng.randint(0, 59), second=0, microsecond=0)

        def event(tid, kind, stage, label, when, tech=None, detail=None):
            tech = tech or rng.choice(techs)
            cur.execute(
                "INSERT INTO repair.event (ticket_id, kind, stage, label, tech_id, tech_name, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tid, kind, stage, label, tech[0], tech[1], Jsonb(detail or {}), when),
            )

        stage_labels = dict(cur.execute("SELECT key, label FROM repair.stage").fetchall())
        ids = sorted(tickets)
        # Spread the tickets over the last DAYS days, weekdays only, busier lately.
        days = [d for d in range(DAYS, 0, -1) if (now - timedelta(days=d)).weekday() < 5]
        weights = [1 + (DAYS - d) / DAYS for d in days]
        start_days = sorted(rng.choices(days, weights=weights, k=len(ids)), reverse=True)

        # A few rifles stuck on the bench for weeks (awaiting parts), so the
        # "open now" chart shows some that are late.
        long_ago = [tid for tid, day in zip(ids, start_days) if 20 <= day <= 50]
        held = {tid: rng.choice(("inspected", "repaired")) for tid in rng.sample(long_ago, min(3, len(long_ago)))}

        count = 0
        shipped = []  # (shipped_at, ticket_id)
        for tid, day in zip(ids, start_days):
            t = tickets[tid]
            _subject, _complaint, problem_labels, solution_labels, cause_label = CASES[t["case"]]
            added = at_work(now - timedelta(days=day))
            adder = rng.choice(techs)
            cur.execute(
                "INSERT INTO repair.rma (ticket_id, subject, requester_name, requester_email, added_at, added_by, "
                " zd_serial, zd_model, zd_caliber, zd_inbound_tracking, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tid, t["subject"], t["name"], "customer@example.com", added, adder[1],
                 t["serial"], t["model"], t["caliber"], t["inbound"], added),
            )
            event(tid, "added", None, "Added to repair queue", added, adder)

            # How long each step takes; a few rifles get stuck somewhere.
            stuck = rng.random() < 0.06
            gaps = {"received": rng.randint(2, 12), "inspected": rng.randint(0, 4),
                    "repaired": rng.randint(1, 8) + (rng.randint(15, 30) if stuck else 0),
                    "tested": rng.randint(0, 2), "shipped": rng.randint(0, 3)}
            fields, when, stage = {}, added, "queued"
            for key in ("received", "inspected", "repaired", "tested", "shipped"):
                if stage == held.get(tid):
                    fields.update(flag="Awaiting parts", flag_at=when)
                    break
                nxt = at_work(when + timedelta(days=gaps[key]))
                if nxt > now:
                    break
                when, stage = nxt, key
                fields[f"{key}_at"] = when
                if key == "received":
                    damaged = rng.random() < 0.06
                    fields.update(arrival_damage=damaged, arrived_loaded=rng.random() < 0.03,
                                  serial_number=t["serial"], serial_match=True, model=t["model"],
                                  caliber=t["caliber"], inbound_tracking=t["inbound"], inbound_match=True)
                    if damaged:
                        fields["arrival_notes"] = "Box crushed on one corner."
                    if accessories and rng.random() < 0.5:
                        for label in rng.sample(sorted(accessories), rng.randint(1, min(2, len(accessories)))):
                            cur.execute("INSERT INTO repair.rma_choice VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                        (tid, accessories[label]))
                elif key == "inspected":
                    for label in problem_labels:
                        if label in problems:
                            cur.execute("INSERT INTO repair.rma_choice VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                        (tid, problems[label]))
                    issue_points = set(rng.sample(points, rng.randint(0, 2))) if points else set()
                    fields.update(
                        complaint_match=rng.choices(["yes", "partly", "no"], [8, 2, 1])[0],
                        inspection=Jsonb([{"label": p, "result": "issue" if p in issue_points else "ok"}
                                          for p in points]),
                        headspace=rng.choices(["pass", "not_checked"], [9, 1])[0],
                        trigger_pull_found=round(rng.uniform(2.6, 5.4), 1),
                    )
                elif key == "repaired":
                    for label in solution_labels:
                        if label in solutions:
                            cur.execute("INSERT INTO repair.rma_choice VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                        (tid, solutions[label]))
                    fields.update(root_cause_id=causes.get(cause_label),
                                  coverage=rng.choices(["warranty", "customer_pay", "goodwill"], [7, 2, 1])[0],
                                  work_done=", ".join(solution_labels),
                                  trigger_pull_after=round(rng.uniform(2.8, 3.4), 1))
                    if "Replaced part" in solution_labels:
                        fields["parts_used"] = rng.choice(("Extractor", "Firing pin spring", "Stock", "Bolt shroud"))
                elif key == "tested":
                    required = rng.random() < 0.6
                    fields.update(function_check="pass", test_required=required)
                    if required:
                        passed = rng.random() < 0.9
                        fields.update(test_result="pass" if passed else "fail",
                                      group_size=round(rng.uniform(0.45, 1.05), 2) if passed else 2.3,
                                      group_unit="moa", test_distance=100, test_ammo="Hornady 143 ELD-X")
                        if not passed:  # failed the test: back to the bench, then a pass
                            when = at_work(when + timedelta(days=rng.randint(1, 3)))
                            fields.update(test_result="pass", group_size=round(rng.uniform(0.6, 1.0), 2),
                                          tested_at=when)
                elif key == "shipped":
                    carrier, fmt = rng.choice(CARRIERS)
                    fields.update(tracking_number=fmt.format(rng.randint(0, 10**10 - 1)),
                                  return_carrier=carrier, ship_serial=t["serial"])
                event(tid, "stage", key, stage_labels.get(key, key.title()), when)

            if stage == "shipped":
                delivered = fields["shipped_at"] + timedelta(days=rng.randint(2, 5))
                if delivered < now:
                    fields.update(delivery_status="delivered", delivered_at=delivered,
                                  delivery_detail="Delivered, front door", delivery_checked_at=delivered)
                    # The demo has no tracking service, so deliveries are marked by hand.
                    event(tid, "delivery", None, "Delivered", delivered,
                          detail={"how": "manual", "not_emailed": True})
                else:
                    fields.update(delivery_status="in_transit", delivery_checked_at=now)
                shipped.append((fields["shipped_at"], tid))

            fields["stage"] = stage
            fields["updated_at"] = when
            sets = ", ".join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE repair.rma SET {sets} WHERE ticket_id = %s", (*fields.values(), tid))
            count += 1

        # Final reports went out soon after shipping, except the newest few,
        # which wait in the Final reports queue for a visitor to review and send.
        shipped.sort()
        for shipped_at, tid in shipped[:-REPORTS_WAITING]:
            t = tickets[tid]
            sent = min(shipped_at + timedelta(hours=rng.randint(4, 30)), now)
            tech = rng.choice(techs)
            body = f"Repair report for ticket #{tid}\n\nRifle: {t['model']} {t['caliber']}"
            cur.execute("INSERT INTO repair.final_report (ticket_id, sent_at, sent_by, sent_body) "
                        "VALUES (%s, %s, %s, %s)", (tid, sent, tech[1], body))
            event(tid, "report", None, "Final repair report sent", sent, tech)
        conn.commit()
    return count
