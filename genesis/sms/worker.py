"""SMS worker: runs queued SMS builds through the shared Orchestrator and sends the outbox.

Runs embedded in `genesis serve` (default) or as its own process (`python -m genesis sms-worker`).
Jobs are leased in SQLite and kept alive by a heartbeat; if a worker dies, the lease expires and the job is
retried (bounded attempts, exponential backoff) by the next worker loop."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid

from ..config import Settings
from ..core.jobs import Orchestrator
from ..core.schemas import BuildRequest
from . import messages as M
from .observe import event
from .phone import mask
from .provider import SendError, SmsProvider
from .service import SmsService, address
from .store import SmsStore

JOB_LEASE_S = 120
HEARTBEAT_S = 15
OUTBOX_MAX_ATTEMPTS = 5
# Build errors worth retrying automatically: infrastructure hiccups, not problems with the app itself.
TRANSIENT_MARKERS = ("temporarily unavailable", "interrupted", "internal error", "could not list gemini models")


def is_transient(error: str | None) -> bool:
    e = (error or "").lower()
    return any(m in e for m in TRANSIENT_MARKERS)


class SmsWorker:
    def __init__(self, settings: Settings, store: SmsStore, orchestrator: Orchestrator, provider: SmsProvider,
                 service: SmsService, websites=None, publisher=None):
        self.s = settings
        self.store = store
        self.orch = orchestrator
        # Website builder (genesis.web.builder.WebsiteBuilder): same create_job/run_existing/cancel interface.
        self.websites = websites if websites is not None else service.web
        self.publisher = publisher  # genesis.web.deploy.publish; injectable for tests
        self.provider = provider
        self.service = service
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---------- lifecycle ----------
    def start(self) -> None:
        for name, target in (("sms-jobs", self._job_loop), ("sms-outbox", self._outbox_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        event("sms.worker_started", worker=self.worker_id, provider=self.provider.name)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)

    def _job_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(2)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                event("sms.worker_error", logging.ERROR)
                logging.getLogger("genesis.sms").exception("sms job loop error")
                self._stop.wait(5)

    def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.send_outbox():
                    self._stop.wait(1)
            except Exception:  # noqa: BLE001
                event("sms.outbox_error", logging.ERROR)
                logging.getLogger("genesis.sms").exception("sms outbox loop error")
                self._stop.wait(5)

    # ---------- jobs ----------
    def recover(self) -> int:
        """Return jobs whose worker vanished (lease expired) to the queue, or fail them after max attempts."""
        n = 0
        for job in self.store.expired_jobs():
            n += 1
            event("sms.job_recovered", logging.WARNING, ref=job["ref"], attempts=job["attempts"])
            if job["build_id"]:
                self.orch.cancel(job["build_id"])
                if self.websites is not None:
                    self.websites.cancel(job["build_id"])
            self._after_failure(job, "Interrupted: the Genesis worker stopped during this build")
        return n

    def run_once(self) -> bool:
        """Recover stale jobs, then run at most one queued job. Returns True if a job was run."""
        self.recover()
        job = self.store.claim_job(self.worker_id, JOB_LEASE_S)
        if job is None:
            return False
        self._run(job)
        return True

    def _engine(self, project: dict | None):
        """The builder for a project: the Android Orchestrator, or the website builder."""
        if project is not None and project.get("kind") == "website":
            return self.websites
        return self.orch

    def _run(self, job: dict) -> None:
        ref = job["ref"]
        user = self.store.user_by_id(job["user_id"])
        project = self.store.project(job["user_id"], job["project_id"])
        event("sms.job_started", ref=ref, attempt=job["attempts"], kind=job["kind"],
              project_kind=(project or {}).get("kind", "android"))
        if job["cancel_requested"]:
            return self._finish_cancelled(job, user)
        if job["kind"] == "publish":
            return self._publish(job, user, project)
        engine = self._engine(project)
        if engine is None:
            self.store.update_job(job["id"], status="failed", error="Website builder unavailable: not set up")
            return self._notify(job, user, f"Sorry, {ref} can't run: website building isn't set up on this Genesis server.")
        try:
            build = engine.create_job(BuildRequest(prompt=job["prompt"]), base_job_id=job["base_build_id"],
                                      change_request=job["change_request"], source="sms")
        except (KeyError, ValueError) as e:
            # The base version vanished: rebuild from the full project description instead.
            event("sms.base_missing", logging.WARNING, ref=ref, error=type(e).__name__)
            build = engine.create_job(BuildRequest(prompt=job["prompt"]), source="sms")
        self.store.update_job(job["id"], build_id=build["id"], error=None)

        done = threading.Event()

        def heartbeat() -> None:
            while not done.wait(HEARTBEAT_S):
                current = self.store.heartbeat(job["id"], self.worker_id, JOB_LEASE_S)
                if current and current["cancel_requested"]:
                    engine.cancel(build["id"])

        hb = threading.Thread(target=heartbeat, name=f"sms-heartbeat-{ref}", daemon=True)
        hb.start()
        try:
            result = engine.run_existing(build["id"], listener=lambda b, ev: self._progress(job["id"], b, ev))
        finally:
            done.set()
            hb.join(2)

        current = self.store.job(job["id"]) or job
        if current["lease_owner"] != self.worker_id or current["status"] != "running":
            # Our lease expired (e.g. the machine slept) and another loop already took this job over.
            event("sms.lease_lost", logging.WARNING, ref=ref, build=build["id"])
            return
        if result.get("status") == "succeeded":
            if engine is self.websites:
                return self._finish_website(current, user, result)
            return self._finish_success(current, user, result)
        if result.get("cancelled") or current["cancel_requested"]:
            return self._finish_cancelled(current, user)
        event("sms.build_failed", logging.WARNING, ref=ref, build=build["id"], transient=is_transient(result.get("error")))
        self._after_failure(current, result.get("error") or "Build failed")

    def _after_failure(self, job: dict, error: str) -> None:
        user = self.store.user_by_id(job["user_id"])
        if job["cancel_requested"]:
            return self._finish_cancelled(job, user)
        if is_transient(error) and job["attempts"] < job["max_attempts"]:
            delay = 30 * 2 ** (job["attempts"] - 1)
            self.store.requeue(job["id"], delay, error)
            event("sms.job_retry", logging.WARNING, ref=job["ref"], attempt=job["attempts"], delay_s=delay)
            self._notify(job, user, f"{job['ref']} hit a temporary problem ({M.failure_reason(error)}) "
                                    f"I'll retry automatically in about {max(1, delay // 60)} min.")
            return
        self.store.update_job(job["id"], status="failed", error=error[:1000])
        event("sms.job_failed", logging.WARNING, ref=job["ref"], attempts=job["attempts"])
        project = self.store.project(job["user_id"], job["project_id"])
        where = ("the website build log (data/sites) on the Genesis computer" if project and project.get("kind") == "website"
                 else "the Genesis dashboard")
        self._notify(job, user, f"Sorry, build {job['ref']} failed. {M.failure_reason(error)} "
                                f"Technical details are in {where}. Reply RETRY to try again, "
                                "or text a simpler description.")

    def _finish_cancelled(self, job: dict, user: dict | None) -> None:
        self.store.update_job(job["id"], status="cancelled", error="Cancelled by user")
        event("sms.job_cancelled", ref=job["ref"])
        self._notify(job, user, f"Build {job['ref']} cancelled.")

    def _finish_success(self, job: dict, user: dict | None, build: dict) -> None:
        project = self.store.project(job["user_id"], job["project_id"])
        apk = build.get("apk") or {}
        artifact = self.orch.store.path(build["id"]) / "artifact" / apk.get("filename", "")
        if not apk or not artifact.is_file():
            # Never announce success without the file the link will serve.
            return self._after_failure(job, "Build reported success but the APK file is missing")
        if project:
            self.store.update_project(project["id"], name=build.get("app_name") or project["name"],
                                      last_success_build_id=build["id"])
            project = self.store.project(job["user_id"], job["project_id"])
        self.store.update_job(job["id"], status="succeeded", error=None)
        event("sms.job_succeeded", ref=job["ref"], build=build["id"], bytes=apk.get("size"))
        url = self.service.download_url(job["user_id"], build["id"], relative_ok=bool(user and user["channel"] == "web"))
        size = f"{apk.get('size', 0) / 1_048_576:.1f} MB"
        verb = "updated" if job["kind"] == "change" else "ready"
        text = (f"Your {M.name_of(project)} is {verb}!\nBuild: {job['ref']}\nStatus: Successful\n"
                + (f"Download (Android APK, {size}): {url}\n" if url else
                   "Download links are not configured (PUBLIC_BASE_URL). Get the APK from the Genesis dashboard.\n")
                + "Text a change (e.g. \"add a dark theme\"), or STATUS / HELP.")
        self._notify(job, user, text)

    def _finish_website(self, job: dict, user: dict | None, build: dict) -> None:
        if not self.websites.site_ok(build["id"]):
            return self._after_failure(job, "Build reported success but the website files are missing")
        project = self.store.project(job["user_id"], job["project_id"])
        if project:
            self.store.update_project(project["id"], name=build.get("app_name") or project["name"],
                                      last_success_build_id=build["id"])
            project = self.store.project(job["user_id"], job["project_id"])
        self.store.update_job(job["id"], status="succeeded", error=None)
        event("sms.job_succeeded", ref=job["ref"], build=build["id"], kind="website")
        url = self.service.preview_url(job["user_id"], build["id"], relative_ok=bool(user and user["channel"] == "web"))
        verb = "updated" if job["kind"] == "change" else "ready"
        lines = [f"Your website {M.name_of(project)} is {verb}!", f"Build: {job['ref']}",
                 "Status: generated and checked on the Genesis server (not published)"]
        lines.append(f"Private preview (expires in {self.s.sms_link_ttl_hours}h): {url}" if url else
                     "Preview links are not configured (PUBLIC_BASE_URL).")
        if project and project.get("deploy_url"):
            lines.append(f"The public site {project['deploy_url']} still shows the previously published version.")
        hint = "Text a change (e.g. \"make the hero darker\")"
        if user and user["role"] == "owner" and not self.service.publish_problem():
            hint += ", or \"publish it\" to put it online"
        lines.append(hint + ".")
        self._notify(job, user, "\n".join(lines))

    def _publish(self, job: dict, user: dict | None, project: dict | None) -> None:
        """Run an owner-approved publish of one exact website version. Never retried automatically."""
        from ..web.deploy import PublishError, publish

        build_id = job["base_build_id"]
        problem = self.service.publish_problem()
        if project is None or project.get("kind") != "website" or not user or user["role"] != "owner":
            problem = problem or "not an owner-approved website"
        if not problem and (self.websites is None or not self.websites.site_ok(build_id)):
            problem = "the approved version is no longer available"
        if problem:
            self.store.update_job(job["id"], status="failed", error=f"Publish refused: {problem}")
            return self._notify(job, user, f"{job['ref']}: not published ({problem}).")
        try:
            out = (self.publisher or publish)(self.s.netlify_auth_token, self.websites.site_dir(build_id),
                                               brand=project.get("name") or "", site_id=project.get("deploy_site_id"))
        except PublishError as e:
            self.store.update_job(job["id"], status="failed", error=f"Netlify publish failed: {e}")
            event("sms.publish_failed", logging.WARNING, ref=job["ref"])
            return self._notify(job, user, f"{job['ref']}: publishing failed ({e}). Your private preview still works.")
        self.store.update_project(project["id"], deploy_url=out["url"], deploy_site_id=out["site_id"])
        self.store.update_job(job["id"], status="succeeded", build_id=build_id, error=None)
        event("sms.published", ref=job["ref"])
        self._notify(job, user, f"Published! {M.name_of(project)} is live at {out['url']}\nBuild: {job['ref']}")

    def _progress(self, job_id: int, build: dict, ev: str) -> None:
        """Build listener: turn real stage changes into rate-limited SMS updates."""
        table = M.WEB_PROGRESS if build.get("kind") == "website" else M.PROGRESS
        if ev in ("succeeded", "failed") or self.s.sms_progress == "off" or ev not in table:
            return
        key, major, text = table[ev]
        if self.s.sms_progress == "major" and not major:
            return
        job = self.store.job(job_id)
        if job is None or job["cancel_requested"]:
            return
        if ev == "building" and build.get("attempts"):
            key = f"building-{len(build['attempts']) + 1}"
            text = f"Rebuilding with the fix (attempt {len(build['attempts']) + 1})."
        now = time.time()
        if key == job["last_progress_key"]:
            return
        if ev != "repairing" and now - job["last_progress_at"] < self.s.sms_progress_min_interval:
            return
        self.store.update_job(job_id, last_progress_key=key, last_progress_at=now)
        self._notify(job, self.store.user_by_id(job["user_id"]), f"{job['ref']}: {text}")

    def _notify(self, job: dict, user: dict | None, text: str) -> None:
        if user is None or user["status"] != "active":
            return
        self.store.enqueue_sms(address(user["phone"], user["channel"]), M.fit("Genesis: " + text), user["id"], job["id"],
                               user["reply_from"] if user["channel"] != "web" else None)

    # ---------- outbox ----------
    def send_outbox(self) -> int:
        msgs = self.store.claim_outbound()
        callback = (f"{self.s.public_base_url}/api/webhooks/twilio/status"
                    if self.s.public_base_url.startswith("https://") and self.provider.name == "twilio" else None)
        for m in msgs:
            user = self.store.user_by_id(m["user_id"]) if m["user_id"] else None
            if user is not None and user["status"] == "opted_out":
                self.store.outbound_failed(m["id"], "recipient opted out", None)
                continue
            try:
                res = self.provider.send(m["phone"], m["body"], callback if not m["phone"].startswith("web:") else None,
                                         m["sender"])
            except SendError as e:
                retry = None if e.permanent or m["attempts"] >= OUTBOX_MAX_ATTEMPTS else 10 * 2 ** m["attempts"]
                self.store.outbound_failed(m["id"], str(e), retry)
                event("sms.send_failed", logging.WARNING if retry else logging.ERROR, msg=m["id"],
                      phone=mask(m["phone"]), code=e.code, permanent=e.permanent, retry_in=retry)
                continue
            self.store.outbound_sent(m["id"], res.sid, res.status)
            event("sms.sent", msg=m["id"], sid=res.sid, phone=mask(m["phone"]), chars=len(m["body"]))
        return len(msgs)

    def drain(self, max_jobs: int = 10) -> None:
        """Run queued jobs and send all pending messages synchronously (CLI simulation and tests)."""
        self.send_outbox()
        for _ in range(max_jobs):
            if not self.run_once():
                break
            self.send_outbox()
        self.send_outbox()
