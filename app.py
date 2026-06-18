"""
app.py — Job Hunter web app (FastAPI).

Run the web server:
    uvicorn app:app --host 0.0.0.0 --port 8080
or:
    python app.py                      # starts uvicorn + the scheduler

Create users (no public signup — this is for you, your brother, friends):
    python app.py createuser <username> <password>

The scheduler starts automatically with the web server. If you'd rather run
the collector as its own service, run `python scheduler.py` separately and set
JOBHUNTER_RUN_SCHEDULER=0 here.

Requires: fastapi uvicorn jinja2 apscheduler requests openpyxl python-multipart
"""
import os
import sys
import threading

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import db
import monitoring
import export

SECRET = os.environ.get("JOBHUNTER_SECRET", "change-me-please-set-JOBHUNTER_SECRET")
RUN_SCHEDULER = os.environ.get("JOBHUNTER_RUN_SCHEDULER", "1") == "1"
RETENTION = db.RETENTION_DAYS

app = FastAPI(title="Job Hunter")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

STATUSES = ["interested", "applied", "interview", "offer", "rejected"]


def current_user(request: Request):
    uid = request.session.get("user_id")
    if uid:
        return {"id": uid, "username": request.session.get("username", "")}
    return None


@app.on_event("startup")
def _startup():
    db.init_db()
    if RUN_SCHEDULER:
        try:
            import scheduler
            sched = scheduler.build_scheduler()
            sched.start()
            app.state.scheduler = sched
            print("[app] scheduler started")
            # kick off one collection shortly after boot so there's data fast
            threading.Timer(5.0, scheduler._collect_api).start()
        except Exception as e:
            print(f"[app] could not start scheduler: {e}")


# ----------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.get_user(username)
    if not user or not db.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."},
            status_code=401)
    request.session["user_id"] = user["id"]
    request.session["username"] = user["username"]
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ----------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, keyword: str = "", company: str = "",
              location: str = "", cap_exempt: str = "", days: int = RETENTION,
              page: int = 1, focus: str = "", sort: str = "newest"):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        return RedirectResponse("/login", status_code=302)
    days = max(1, min(int(days or RETENTION), RETENTION))
    result = db.query_jobs(keyword=keyword, company=company, location=location,
                           cap_exempt=cap_exempt, days=days, page=page, focus=focus, sort=sort)
    tracked = db.tracked_keys(user["id"])
    stats = db.job_stats()
    co_counts = db.company_job_counts()
    return templates.TemplateResponse(request, "jobs.html", {
        "user": user, "result": result, "tracked": tracked,
        "stats": stats, "co_counts": co_counts, "retention": RETENTION, "statuses": STATUSES,
        "filters": {"keyword": keyword, "company": company, "location": location,
                    "cap_exempt": cap_exempt, "days": days, "focus": focus, "sort": sort},
    })


# ----------------------------------------------------------------- tracking
@app.post("/track")
async def track(request: Request):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        raise HTTPException(401)
    data = await request.json()
    job = {k: data.get(k, "") for k in
           ("ats", "company", "ext_id", "title", "location", "url")}
    status = data.get("status", "interested")
    if status not in STATUSES:
        status = "interested"
    db.track_application(user["id"], job, status=status)
    return JSONResponse({"ok": True})


@app.post("/update")
async def update(request: Request):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        raise HTTPException(401)
    data = await request.json()
    db.update_application(user["id"], data["job_key"],
                          status=data.get("status"), notes=data.get("notes"))
    return JSONResponse({"ok": True})


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        return RedirectResponse("/login", status_code=302)
    apps = db.list_applications(user["id"])
    return templates.TemplateResponse(request, "tracker.html", {
        "user": user, "apps": apps, "statuses": STATUSES})


@app.get("/export.xlsx")
def export_xlsx(request: Request):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        return RedirectResponse("/login", status_code=302)
    data = export.build_tracker_xlsx(user["id"], user["username"])
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="job_tracker.xlsx"'})


@app.post("/collect")
def manual_collect(request: Request):
    """Trigger a collection run on demand (handy for testing)."""
    if not current_user(request):
        raise HTTPException(401)
    import collector
    threading.Thread(target=collector.run, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "collection started"})


# ----------------------------------------------------------------- CLI
def _cli():
    if len(sys.argv) >= 2 and sys.argv[1] == "createuser":
        if len(sys.argv) != 4:
            print("usage: python app.py createuser <username> <password>")
            sys.exit(1)
        db.init_db()
        ok = db.create_user(sys.argv[2], sys.argv[3])
        print("created" if ok else "user already exists")
        sys.exit(0)




# ── Health Dashboard ──
@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    user = current_user(request)
    if not user or user.get("id") != 1:  # admin only
        return RedirectResponse("/login", 303)
    monitoring.init_monitoring()
    data = monitoring.get_health_data()
    return templates.TemplateResponse(request, "health.html", {"data": data, "user": user, "active": "health"})

if __name__ == "__main__":
    _cli()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
