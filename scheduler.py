"""
scheduler.py — collection schedules:
  * API collection: every 25 min (9am-8pm ET), hourly (8pm-9am ET)
  * Playwright collection: specific times only (heavy, browser-based)
  * Purge: 3:30 AM ET daily
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import collector
import db

ET = "America/New_York"

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

def _purge_job():
    try:
        n = db.purge_old_jobs()
        print(f"[scheduler] purged {n} jobs past retention")
    except Exception as e:
        print(f"[scheduler] purge failed: {e}")

def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)

    # API collection: every 25 min during active hours
    sched.add_job(_collect_api, CronTrigger(hour="9-19", minute="0,25,50", timezone=ET),
                  id="collect_active", max_instances=1, coalesce=True)

    # API collection: overnight hourly
    sched.add_job(_collect_api, CronTrigger(hour="0-8,20-23", minute="0", timezone=ET),
                  id="collect_overnight", max_instances=1, coalesce=True)

    # Playwright collection: specific times only
    # 9:30, 10:30, 13:30, 16:00, 17:00, 18:30, 20:30 ET
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="9,10", minute="30", timezone=ET),
                  id="pw_morning", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="13", minute="30", timezone=ET),
                  id="pw_afternoon1", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="16,17", minute="0", timezone=ET),
                  id="pw_afternoon2", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="18", minute="30", timezone=ET),
                  id="pw_evening1", max_instances=1, coalesce=True)
    sched.add_job(_collect_playwright,
                  CronTrigger(hour="20", minute="30", timezone=ET),
                  id="pw_evening2", max_instances=1, coalesce=True)

    # Daily retention purge
    sched.add_job(_purge_job, CronTrigger(hour="3", minute="30", timezone=ET),
                  id="purge", max_instances=1, coalesce=True)

    return sched

if __name__ == "__main__":
    import time
    db.init_db()
    s = build_scheduler()
    s.start()
    print("[scheduler] started")
    print("  API: every 25m (9am-8pm ET), hourly (overnight)")
    print("  Playwright: 9:30, 10:30, 1:30pm, 4pm, 5pm, 6:30pm, 8:30pm ET")
    print("  Purge: 3:30am ET")
    print("[scheduler] running initial API collection...")
    _collect_api()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        s.shutdown()
