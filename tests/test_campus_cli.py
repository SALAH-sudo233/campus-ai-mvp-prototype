import ast
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.support import ROOT, DemoServer, register

CLI = os.path.join(ROOT, "cli", "campus-cli")
loader = importlib.machinery.SourceFileLoader("campus_cli", CLI)
spec = importlib.util.spec_from_loader(loader.name, loader)
campus_cli = importlib.util.module_from_spec(spec)
loader.exec_module(campus_cli)


class CliTests(unittest.TestCase):
    def invoke(self, *arguments, base="http://127.0.0.1:1", credential=None):
        credential = credential or "cpat_" + secrets.token_urlsafe(32)
        env = {**os.environ, "CAMPUS_PAT": credential, "CAMPUS_BASE_URL": base}
        result = subprocess.run([sys.executable, CLI, *arguments], env=env, text=True,
                                input="", capture_output=True, timeout=10)
        self.assertTrue(credential not in result.stdout + result.stderr, "CLI exposed credential")
        self.assertTrue(credential not in " ".join(result.args), "CLI put credential in argv")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_imports_are_standard_library_only(self):
        source = pathlib.Path(CLI).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
        self.assertTrue(os.access(CLI, os.X_OK))

    def test_routes_cover_declared_interfaces(self):
        cases = [
            (["me"], "GET", "/api/me"),
            (["onboarding", "get"], "GET", "/api/onboarding"),
            (["onboarding", "update"], "POST", "/api/onboarding"),
            (["works", "list"], "GET", "/api/works"),
            (["works", "mine"], "GET", "/api/works/mine"),
            (["works", "get", "--id", "work"], "GET", "/api/works/work"),
            (["works", "submit"], "POST", "/api/works"),
            (["works", "update", "--id", "work"], "PATCH", "/api/works/work"),
            (["works", "withdraw", "--id", "work"], "POST", "/api/works/work/withdraw"),
            (["works", "comments", "--id", "work"], "GET", "/api/works/work/comments"),
            (["works", "comment", "--id", "work"], "POST", "/api/works/work/comments"),
            (["works", "upvote", "--id", "work"], "POST", "/api/works/work/upvote"),
            (["works", "download", "--id", "work"], "GET", "/api/works/work/download"),
            (["works", "upload"], "POST", "/api/works/upload"),
            (["works", "preview-image"], "POST", "/api/works/preview-image"),
            (["key", "rotate"], "POST", "/api/key/rotate"),
            (["consume"], "POST", "/api/consume"),
            (["logout"], "POST", "/api/logout"),
            (["accounts", "list"], "GET", "/api/admin/accounts"),
        ]
        for action in campus_cli.ORG_READS:
            cases.append((["org", action, "--org-id", "org"], "GET", "/api/orgs/org/" + action))
        for action in ("review", "feature", "unpublish"):
            cases.append((["org", action, "--org-id", "org", "--id", "work"], "POST",
                          "/api/orgs/org/works/work/" + action))
        cases.append((["org", "member", "--org-id", "org", "--id", "member"], "GET", "/api/orgs/org/members/member"))
        for arguments, method, path in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(campus_cli.route(campus_cli.parser().parse_args(arguments)), (method, path))

    def test_writes_require_confirmation_and_errors_are_sanitized(self):
        result = self.invoke("org", "unpublish", "--org-id", "college", "--id", "work", "--json")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["error"], "confirmation_required_use_yes")
        self.assertEqual(self.invoke("me", "--json").returncode, 17)
        for base in ("ftp://example.com", "http://person:pass@example.com", "http://[", "https://example.com/?secret=value"):
            self.assertEqual(self.invoke("me", base=base).returncode, 2)
        self.assertEqual(self.invoke("me", "--timeout", "nan").returncode, 2)

    def test_http_codes_reflected_credentials_and_redirect_never_leak(self):
        captured = []
        credential = "cpat_" + secrets.token_urlsafe(32)
        status = [200]

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                captured.append((self.path, self.headers.get("Authorization")))
                self.send_response(status[0])
                self.send_header("Content-Type", "application/json")
                if status[0] == 302:
                    self.send_header("Location", "/redirect-target")
                self.end_headers()
                self.wfile.write(json.dumps({"error": credential, "header": self.headers.get("Authorization")}).encode())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            for http_status, exit_code in ((200, 0), (401, 10), (403, 11), (404, 12), (409, 13),
                                            (413, 14), (429, 15), (500, 16), (503, 16), (302, 18)):
                status[0] = http_status
                result = self.invoke("--json", "me", base=base, credential=credential)
                self.assertEqual(result.returncode, exit_code)
                self.assertIsInstance(json.loads(result.stdout), dict)
            self.assertEqual(len(captured), 10)
            self.assertTrue(all(path == "/api/me" for path, _ in captured))
            self.assertTrue(all(header == "Bearer " + credential for _, header in captured))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_real_server_me_submit_comments_and_realtime_permissions(self):
        with DemoServer() as server:
            _, user = register(server, "2024001093")
            code, created = server.post("/api/me/tokens", {"requestId": "cli"}, user["token"])
            self.assertEqual(code, 201)
            credential = created["token"]
            result = self.invoke("--json", "me", base=server.base, credential=credential)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["role"], "org_member")
            result = self.invoke("works", "submit", "--data", '{"title":"CLI 作品","summary":"测试","category":"学习"}',
                                 "--yes", "--json", base=server.base, credential=credential)
            self.assertEqual(result.returncode, 0)
            work_id = json.loads(result.stdout)["workId"]
            result = self.invoke("works", "mine", "--json", base=server.base, credential=credential)
            self.assertEqual(result.returncode, 0)
            self.assertIn(work_id, [item["workId"] for item in json.loads(result.stdout)["items"]])
            self.assertEqual(self.invoke("accounts", "list", "--json", base=server.base, credential=credential).returncode, 11)
            self.assertEqual(self.invoke("works", "withdraw", "--id", work_id, "--yes", "--json",
                                         base=server.base, credential=credential).returncode, 0)

    def test_json_file_and_binary_upload_request_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            data_file = pathlib.Path(directory) / "work.json"
            data_file.write_text('{"title":"例子"}', encoding="utf-8")
            args = campus_cli.parser().parse_args(["works", "submit", "--data", "@" + str(data_file), "--request-id", "same"])
            path, body, content_type = campus_cli.request_parts(args, *campus_cli.route(args))
            self.assertEqual(json.loads(body)["requestId"], "same")
            self.assertEqual(content_type, "application/json")
            upload = pathlib.Path(directory) / "index.html"
            upload.write_bytes(b"<!doctype html><p>test</p>")
            args = campus_cli.parser().parse_args(["works", "upload", "--file", str(upload)])
            path, body, content_type = campus_cli.request_parts(args, *campus_cli.route(args))
            self.assertIn("filename=index.html", path)
            self.assertIn("softwareType=web", path)
            self.assertEqual(body, upload.read_bytes())
            self.assertEqual(content_type, "application/octet-stream")
