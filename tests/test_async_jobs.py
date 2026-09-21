"""Unit tests for Stage C async analyze jobs (Task T18)."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dossier_engine.jobs import JobStore  # noqa: E402
from dossier_engine.webserver import Handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


SAMPLE = {
    "product_name": "Test Product",
    "target_market": "US",
    "product_category": "dietary_supplement",
    "ingredients": [
        {
            "name": "Ashwagandha",
            "latin_name": "Withania somnifera",
            "stated_dose": "300 mg",
        }
    ],
    "claimed_benefits": ["supports stress relief"],
    "use_auth": False,
}


def _fake_analyze(payload, *, on_progress=None):
    if on_progress:
        on_progress("validate", 5, "Validating…", None)
        on_progress("per_ingredient", 40, "Evidence…", None)
        on_progress("done", 100, "Complete", None)
    return {
        "product_name": payload.get("product_name"),
        "category": "dietary_supplement",
        "target_market": "US",
        "auth_enabled": False,
        "auth_requested": False,
        "report_markdown": "# ok",
        "dossier": {"schema_version": "1.0.0"},
        "per_ingredient": [],
        "safety": [],
        "pharmacology": [],
        "authentication_log": [],
        "formulation_flags": [],
        "regulatory_flags": [],
    }


def _slow_analyze(payload, *, on_progress=None):
    time.sleep(2.0)
    return _fake_analyze(payload, on_progress=on_progress)


class JobStoreTests(unittest.TestCase):
    def test_submit_poll_succeeds(self):
        store = JobStore(max_workers=1, default_timeout_s=10, retention_s=30)
        try:
            rec = store.submit_analyze(payload=SAMPLE, runner=_fake_analyze)
            deadline = time.time() + 5
            while time.time() < deadline:
                cur = store.get(rec.job_id)
                if cur.status in ("succeeded", "failed", "timed_out"):
                    break
                time.sleep(0.05)
            cur = store.get(rec.job_id)
            self.assertEqual(cur.status, "succeeded")
            self.assertEqual(cur.result["product_name"], "Test Product")
            view = store.public_view(cur)
            self.assertEqual(view["progress"]["percent"], 100)
            self.assertTrue(view["poll_url"].startswith("/api/jobs/"))
        finally:
            store._executor.shutdown(wait=False, cancel_futures=True)

    def test_idempotent_key_reuses_job(self):
        store = JobStore(max_workers=1, default_timeout_s=10, retention_s=30)
        try:
            a = store.submit_analyze(
                payload=SAMPLE, runner=_fake_analyze, idempotency_key="k1"
            )
            b = store.submit_analyze(
                payload=SAMPLE, runner=_fake_analyze, idempotency_key="k1"
            )
            self.assertEqual(a.job_id, b.job_id)
        finally:
            store._executor.shutdown(wait=False, cancel_futures=True)

    def test_timeout(self):
        store = JobStore(max_workers=1, default_timeout_s=0.25, retention_s=30)
        try:
            rec = store.submit_analyze(
                payload=SAMPLE, runner=_slow_analyze, timeout_s=0.25
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                cur = store.get(rec.job_id)
                if cur.status in ("timed_out", "failed", "succeeded"):
                    break
                time.sleep(0.05)
            self.assertEqual(store.get(rec.job_id).status, "timed_out")
        finally:
            store._executor.shutdown(wait=False, cancel_futures=True)


class HttpJobEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Patch STORE used by Handler
        from dossier_engine import jobs as jobmod
        from dossier_engine import webserver as ws

        cls._orig_store = jobmod.STORE
        cls.store = JobStore(max_workers=2, default_timeout_s=30, retention_s=60)
        jobmod.STORE = cls.store
        cls._orig_analyze = ws._analyze
        ws._analyze = _fake_analyze
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        from dossier_engine import jobs as jobmod
        from dossier_engine import webserver as ws

        cls.httpd.shutdown()
        jobmod.STORE = cls._orig_store
        ws._analyze = cls._orig_analyze
        cls.store._executor.shutdown(wait=False, cancel_futures=True)

    def _request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        return resp.status, data

    def test_health_async_flag(self):
        status, data = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("async_jobs"))
        self.assertIn("analyze_job_timeout_s", data)

    def test_sync_analyze_still_works(self):
        status, data = self._request("POST", "/api/analyze", SAMPLE)
        self.assertEqual(status, 200)
        self.assertEqual(data["product_name"], "Test Product")

    def test_job_submit_and_poll(self):
        status, body = self._request("POST", "/api/jobs/analyze", SAMPLE)
        self.assertEqual(status, 202)
        self.assertIn("job_id", body)
        job_id = body["job_id"]
        deadline = time.time() + 5
        final = None
        while time.time() < deadline:
            st, final = self._request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(st, 200)
            if final["status"] in ("succeeded", "failed", "timed_out"):
                break
            time.sleep(0.05)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["result"]["product_name"], "Test Product")
        st, result = self._request("GET", f"/api/jobs/{job_id}/result")
        self.assertEqual(st, 200)
        self.assertEqual(result["product_name"], "Test Product")

    def test_idempotency_header(self):
        h = {"Idempotency-Key": "c-demo-1"}
        _, a = self._request("POST", "/api/jobs/analyze", SAMPLE, headers=h)
        _, b = self._request("POST", "/api/jobs/analyze", SAMPLE, headers=h)
        self.assertEqual(a["job_id"], b["job_id"])

    def test_unknown_job(self):
        st, data = self._request(
            "GET", "/api/jobs/00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(st, 404)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
