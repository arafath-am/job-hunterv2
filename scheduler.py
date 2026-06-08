"""
scheduler.py — runs the collector on your two cadences and purges old jobs.

  * 09:00–20:00 ET : every 25 minutes   (active US market window)
  * 20:00–09:00 ET : every 60 minutes   (overnight)
  * 03:30 ET daily : purge jobs older than retention window

The 699 (Workday/Taleo/custom) will get their own jobs/cadence later; this
scheduler only drives whatever is in `companies` with resolve_status='resolved',
so adding them later needs no scheduler change.

Requires: apscheduler   (pip install apscheduler)
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import collector
import db

ET = "America/New_York"


def _collect_job():
    try:
        collector.run()
    except Exception as e:
        print(f"[scheduler] collector run failed: {e}")


def _purge_job():
    try:
        n = db.purge_old_jobs()
        print(f"[scheduler] purged {n} jobs past retention")
    except Exception as e:
        print(f"[scheduler] purge failed: {e}")


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)

    # active window: every 25 min between 09:00 and 19:59 ET
    sched.add_job(_collect_job, CronTrigger(hour="9-19", minute="0,25,50", timezone=ET),
                  id="collect_active", max_instances=1, coalesce=True)

    # overnight: top of every hour from 20:00 to 08:00 ET
    sched.add_job(_collect_job, CronTrigger(hour="0-8,20-23", minute="0", timezone=ET),
                  id="collect_overnight", max_instances=1, coalesce=True)

    # daily retention purge
    sched.add_job(_purge_job, CronTrigger(hour="3", minute="30", timezone=ET),
                  id="purge", max_instances=1, coalesce=True)

    return sched


if __name__ == "__main__":
    # standalone mode: run scheduler in the foreground (e.g., own systemd service)
    import time
    db.init_db()
    s = build_scheduler()
    s.start()
    print("[scheduler] started (active 25m 9-20 ET, overnight hourly, purge 03:30 ET)")
    print("[scheduler] running one collection now...")
    _collect_job()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        s.shutdown()
