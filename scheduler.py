"""
scheduler.py — Job Hunter v2 collection schedules.

MORNING BLITZ (10 AM - 1 PM ET):
  High-frequency collection — jobs posted in the morning get caught fast.
  API, Playwright, Universal rotate every ~12 minutes.

AFTERNOON/EVENING (1 PM - 9 PM ET):
  Standard hourly cadence. API at :00, PW/Universal alternating at :20.

OVERNIGHT:
  API at 2 AM and 6 AM for off-hours coverage.
  Purge at 3:30 AM, health refresh at 4:15 AM, endpoint check Sunday 5:15 AM.

Schedule (all times ET):
  9:00  API
  ── MORNING BLITZ ──
  10:00 API    10:12 PW     10:30 Universal
  10:42 API    10:52 PW     11:10 Universal
  11:28 API    11:40 PW     11:58 Universal
  12:15 API    12:28 PW     12:43 Universal
  ── AFTERNOON/EVENING ──
  1:00  API    1:20  PW
  2:00  API    2:20  Universal
  3:00  API    3:20  PW
  4:00  API    4:20  Universal
  5:00  API
  6:00  API
  7:00  API
  8:00  API
  9:00  API
  ── OVERNIGHT ──
  2:00  API
  6:00  API
  3:30  Purge    4:15 Health    5:15 Sun Endpoint

Totals: 16 API, 6 PW, 6 Universal per day
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import collector
import db
import monitoring

ET = "America/New_York"


# ── Collection wrappers ──

def _collect_api():
    try:
        collector.run()
    except Exception as e:
        print(f"[scheduler] API collection failed: {e}")


def _collect_playwright():
    try:
        collector.run_playwright()
    except Exception as e:
        print(f"[scheduler] Playwright collection failed: {e}")


def _collect_universal():
    try:
        collector.run_universal()
    except Exception as e:
        print(f"[scheduler] Universal collection failed: {e}")


def _purge_job():
    try:
        n = db.purge_old_jobs()
        print(f"[scheduler] purged {n} jobs past retention")
    except Exception as e:
        print(f"[scheduler] purge failed: {e}")


def _health_refresh():
    try:
        monitoring.init_monitoring()
        result = monitoring.refresh_health_statuses()
        print(f"[scheduler] health refresh: {result}")
    except Exception as e:
        print(f"[scheduler] health refresh error: {e}")


def _endpoint_check():
    try:
        from tools.endpoint_checker import check_endpoint, store_check
        monitoring.init_monitoring()
        companies = db.resolved_companies()
        drifted = 0
        checked = 0
        for c in companies:
            result = check_endpoint(
                c["company_name"], c["ats"], c["endpoint"],
                c.get("resolved_token")
            )
            store_check(result)
            checked += 1
            if result["drift_detected"]:
                drifted += 1
            import time
            time.sleep(0.5)
        if drifted > 0:
            monitoring._alert(
                f"🔍 Weekly endpoint check: {drifted}/{checked} endpoints drifted"
            )
        print(f"[scheduler] endpoint check done: {checked} checked, {drifted} drifted")
    except Exception as e:
        print(f"[scheduler] endpoint check error: {e}")


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)

    # ═══════════════════════════════════════════
    # API COLLECTION — 16 runs/day
    # ═══════════════════════════════════════════

    # Early morning
    sched.add_job(_collect_api, CronTrigger(hour="9", minute="0", timezone=ET),
                  id="api_early", max_instances=1, coalesce=True)

    # Morning blitz API: 10:00, 10:42, 11:28, 12:15
    sched.add_job(_collect_api,
                  CronTrigger(hour="10", minute="0", timezone=ET),
                  id="api_blitz_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_api,
                  CronTrigger(hour="10", minute="42", timezone=ET),
                  id="api_blitz_2", max_instances=1, coalesce=True)
    sched.add_job(_collect_api,
                  CronTrigger(hour="11", minute="28", timezone=ET),
                  id="api_blitz_3", max_instances=1, coalesce=True)
    sched.add_job(_collect_api,
                  CronTrigger(hour="12", minute="15", timezone=ET),
                  id="api_blitz_4", max_instances=1, coalesce=True)

    # Afternoon/evening API: 1:00 - 9:00 PM hourly
    sched.add_job(_collect_api,
                  CronTrigger(hour="13-21", minute="0", timezone=ET),
                  id="api_afternoon", max_instances=1, coalesce=True)

    # Overnight API: 2:00 AM, 6:00 AM
    sched.add_job(_collect_api,
                  CronTrigger(hour="2", minute="0", timezone=ET),
                  id="api_night_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_api,
                  CronTrigger(hour="6", minute="0", timezone=ET),
                  id="api_night_2", max_instances=1, coalesce=True)

    # ═══════════════════════════════════════════
    # PLAYWRIGHT COLLECTION — 6 runs/day
    # ═══════════════════════════════════════════

    # Morning blitz PW: 10:12, 10:52, 11:40, 12:28
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="10", minute="12", timezone=ET),
                  id="pw_blitz_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="10", minute="52", timezone=ET),
                  id="pw_blitz_2", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="11", minute="40", timezone=ET),
                  id="pw_blitz_3", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="12", minute="28", timezone=ET),
                  id="pw_blitz_4", max_instances=1, coalesce=True)

    # Afternoon PW: 1:20, 3:20
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="13", minute="20", timezone=ET),
                  id="pw_afternoon_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="15", minute="20", timezone=ET),
                  id="pw_afternoon_2", max_instances=1, coalesce=True)

    # ═══════════════════════════════════════════
    # UNIVERSAL COLLECTION — 6 runs/day
    # ═══════════════════════════════════════════

    # Morning blitz Universal: 10:30, 11:10, 11:58, 12:43
    sched.add_job(_collect_universal,
                  CronTrigger(hour="10", minute="30", timezone=ET),
                  id="uni_blitz_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_universal,
                  CronTrigger(hour="11", minute="10", timezone=ET),
                  id="uni_blitz_2", max_instances=1, coalesce=True)
    sched.add_job(_collect_universal,
                  CronTrigger(hour="11", minute="58", timezone=ET),
                  id="uni_blitz_3", max_instances=1, coalesce=True)
    sched.add_job(_collect_universal,
                  CronTrigger(hour="12", minute="43", timezone=ET),
                  id="uni_blitz_4", max_instances=1, coalesce=True)

    # Afternoon Universal: 2:20, 4:20
    sched.add_job(_collect_universal,
                  CronTrigger(hour="14", minute="20", timezone=ET),
                  id="uni_afternoon_1", max_instances=1, coalesce=True)
    sched.add_job(_collect_universal,
                  CronTrigger(hour="16", minute="20", timezone=ET),
                  id="uni_afternoon_2", max_instances=1, coalesce=True)

    # ═══════════════════════════════════════════
    # MAINTENANCE JOBS
    # ═══════════════════════════════════════════

    # Daily retention purge + archive: 3:30 AM
    sched.add_job(_purge_job, CronTrigger(hour="3", minute="30", timezone=ET),
                  id="purge", max_instances=1, coalesce=True)

    # Health status refresh: 4:15 AM daily
    sched.add_job(_health_refresh, CronTrigger(hour="4", minute="15", timezone=ET),
                  id="health_refresh", max_instances=1, coalesce=True)

    # Endpoint drift check: Sunday 5:15 AM
    sched.add_job(_endpoint_check,
                  CronTrigger(day_of_week="sun", hour="5", minute="15", timezone=ET),
                  id="endpoint_check", max_instances=1, coalesce=True)

    return sched


if __name__ == "__main__":
    import time
    db.init_db()
    monitoring.init_monitoring()
    s = build_scheduler()
    s.start()
    print("[scheduler] started — Job Hunter v2")
    print()
    print("  ── MORNING BLITZ (10 AM - 1 PM ET) ──")
    print("  API:       10:00, 10:42, 11:28, 12:15")
    print("  Playwright: 10:12, 10:52, 11:40, 12:28")
    print("  Universal:  10:30, 11:10, 11:58, 12:43")
    print()
    print("  ── STANDARD ──")
    print("  API:        9:00, 1-9 PM hourly, 2 AM, 6 AM")
    print("  Playwright: 1:20 PM, 3:20 PM")
    print("  Universal:  2:20 PM, 4:20 PM")
    print()
    print("  ── MAINTENANCE ──")
    print("  Purge: 3:30 AM | Health: 4:15 AM | Endpoint: Sun 5:15 AM")
    print()
    print("[scheduler] running initial API collection...")
    _collect_api()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        s.shutdown()
