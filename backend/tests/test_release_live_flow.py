"""Real Next.js -> HMAC v3 -> FastAPI -> local PostgreSQL; external auth is a stub."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.test_analytics_release import migrate
from tests.test_schema_preflight import scratch_db, target_env, needs_postgres, execute, run_status


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@needs_postgres
def test_analytics_and_pdf_download_through_both_production_servers(scratch_db, target_env, tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert (root / "frontend/.next/BUILD_ID").exists(), "Run the production frontend build before integration tests"
    migrate(scratch_db, target_env)
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 0
    execute(scratch_db,
            "INSERT INTO zconnect_companies(id,name) VALUES ('other','Other company')",
            "INSERT INTO zconnect_order_facts(company_id,unify_order_id,customer_key,invoice_date,currency,net_amount,line_count,source) VALUES ('default','D1','Demo','2026-10-21','EUR',10,0,'invoice_created'), ('other','O1','Private','2026-10-21','EUR',99,0,'invoice_created')")

    user = {"id": "isolated-user", "email": "demo@example.invalid", "app_metadata": {"role": "admin", "company_id": "default"}}

    class AuthStub(BaseHTTPRequestHandler):
        def send(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            assert self.path == "/auth/v1/user"
            self.send(user)

        def do_POST(self):
            assert self.path.startswith("/auth/v1/token?")
            self.send({"access_token": "isolated-token", "refresh_token": "isolated-refresh", "expires_in": 3600, "user": user})

        def log_message(self, *_args):
            pass

    auth = HTTPServer(("127.0.0.1", 0), AuthStub)
    thread = threading.Thread(target=auth.serve_forever, daemon=True)
    thread.start()
    backend_port, frontend_port = free_port(), free_port()
    env = os.environ.copy()
    env.update(DATABASE_URL=scratch_db, ZCONNECT_ENVIRONMENT="test",
               ZCONNECT_BACKEND_SECRET=secrets.token_urlsafe(48),
               BACKEND_URL=f"http://127.0.0.1:{backend_port}",
               SUPABASE_URL=f"http://127.0.0.1:{auth.server_port}",
               SUPABASE_ANON_KEY="isolated", SUPABASE_SERVICE_ROLE_KEY="isolated",
               UNIFY_BASE_URL="http://127.0.0.1:9", ZOHO_BASE_URL="http://127.0.0.1:9",
               UNIFY_CLIENT_ID="", UNIFY_CLIENT_SECRET="", ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="", ZOHO_REFRESH_TOKEN="",
               NEXT_TELEMETRY_DISABLED="1")
    processes, logs = [], []
    base = f"http://127.0.0.1:{frontend_port}"

    def request(url, data=None, headers=None):
        try:
            return urlopen(Request(url, data=data, headers=headers or {}), timeout=30)
        except HTTPError as error:
            return error

    try:
        for args, cwd, name in (
            ([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(backend_port)], root / "backend", "backend"),
            ([shutil.which("node"), str(root / "frontend/node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", str(frontend_port)], root / "frontend", "frontend"),
        ):
            log = (tmp_path / f"{name}.log").open("wb")
            logs.append(log)
            processes.append(subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW))
        for url in (f"http://127.0.0.1:{backend_port}/", base + "/api/auth/session"):
            for attempt in range(60):
                try:
                    with request(url):
                        break
                except OSError:
                    assert all(p.poll() is None for p in processes), "Integration server exited"
                    time.sleep(0.25)
            else:
                pytest.fail("Integration server startup timed out")

        with request(base + "/api/auth/login", json.dumps({"email": user["email"], "password": "test-only"}).encode(), {"Origin": base, "Content-Type": "application/json"}) as response:
            assert response.status == 200
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            assert "HttpOnly" in response.headers["Set-Cookie"] and "Secure" in response.headers["Set-Cookie"]
        headers = {"Cookie": cookie}
        query = "?date_from=2026-10-01&date_to=2026-10-21"
        with request(base + "/api/analytics/summary" + query, headers=headers) as response:
            assert response.status == 200
            assert json.load(response)["kpis"]["invoiced_value"] == "10.00"
        with request(base + "/api/analytics/summary" + query + "&company_id=other", headers=headers) as response:
            assert response.status == 403
        with request(base + "/api/reports/weekly.pdf?period=custom&date_from=2026-10-01&date_to=2026-10-21", headers=headers) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("application/pdf")
            assert "attachment" in response.headers["Content-Disposition"]
            assert response.headers["Cache-Control"] == "no-store"
            pdf = response.read()
            assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-100:]
            (tmp_path / "integration-report.pdf").write_bytes(pdf)
        with request(base + "/api/reports/weekly.pdf?company_id=other", headers=headers) as response:
            assert response.status == 403
        with request(f"http://127.0.0.1:{backend_port}/api/analytics/summary") as response:
            assert response.status == 401
        with request(base + "/analytics", headers=headers) as response:
            assert response.status == 200
    finally:
        for process in reversed(processes):
            process.terminate()
            process.wait(timeout=20)
        for log in logs:
            log.close()
        auth.shutdown()
        auth.server_close()
