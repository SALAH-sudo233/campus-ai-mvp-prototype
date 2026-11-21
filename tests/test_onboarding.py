import threading
import unittest

from tests.support import DemoServer, register


class OnboardingTests(unittest.TestCase):
    def test_progress_skip_complete_exit_and_conflict(self):
        with DemoServer() as server:
            code, data = register(server, "2024001006")
            self.assertEqual(code, 201, data)
            token = data["token"]
            code, payload = server.get("/api/onboarding", token)
            self.assertEqual(code, 200, payload)
            progress = payload["progress"]
            self.assertTrue(progress["autoOpenPending"])
            self.assertEqual(progress["status"], "not_started")
            code, started = server.post("/api/onboarding", {
                "action": "start", "stepId": "team-basics", "expectedRevision": progress["revision"]
            }, token)
            self.assertEqual(code, 200, started)
            progress = started["progress"]
            code, skipped = server.post("/api/onboarding", {
                "action": "skip", "stepId": "team-basics", "expectedRevision": progress["revision"]
            }, token)
            self.assertEqual(code, 200, skipped)
            progress = skipped["progress"]
            self.assertEqual(progress["skippedCount"], 1)
            self.assertEqual(progress["completedCount"], 0)
            code, completed = server.post("/api/onboarding", {
                "action": "complete", "stepId": "reuse-teams", "completionKind": "demo",
                "expectedRevision": progress["revision"], "draft": {"productChoice": "demo"}
            }, token)
            self.assertEqual(code, 200, completed)
            self.assertEqual(completed["progress"]["completedCount"], 1)
            self.assertEqual(completed["progress"]["skippedCount"], 1)
            code, exited = server.post("/api/onboarding", {
                "action": "exit", "stepId": "first-work", "expectedRevision": completed["progress"]["revision"]
            }, token)
            self.assertEqual(code, 200, exited)
            self.assertEqual(exited["progress"]["exitCount"], 1)
            code, duplicate = server.post("/api/onboarding", {
                "action": "complete", "stepId": "reuse-teams", "completionKind": "demo",
                "expectedRevision": exited["progress"]["revision"]
            }, token)
            self.assertEqual(code, 200, duplicate)
            self.assertFalse(duplicate["changed"])

    def test_concurrent_submit_has_single_winner(self):
        with DemoServer() as server:
            code, data = register(server, "2024001007")
            token = data["token"]
            progress = server.get("/api/onboarding", token)[1]["progress"]
            payload = {"action": "complete", "stepId": "team-basics", "completionKind": "demo",
                       "expectedRevision": progress["revision"]}
            results = []
            lock = threading.Lock()
            def submit():
                result = server.post("/api/onboarding", payload, token)
                with lock:
                    results.append(result)
            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            codes = sorted(code for code, _ in results)
            self.assertIn(codes, ([200, 409], [200, 200]))
            current = server.get("/api/onboarding", token)[1]["progress"]
            self.assertEqual(current["completedCount"], 1)

    def test_invalid_payloads_are_rejected(self):
        with DemoServer() as server:
            code, data = register(server, "2024001008")
            token = data["token"]
            revision = data["onboarding"]["revision"]
            code, body = server.post("/api/onboarding", {"action": "start", "stepId": "nope",
                                                          "expectedRevision": revision}, token)
            self.assertEqual(code, 400, body)
            code, body = server.post("/api/onboarding", {"action": "complete", "stepId": "team-basics",
                                                          "completionKind": "real", "expectedRevision": revision}, token)
            self.assertEqual(code, 400, body)
            code, body = server.post("/api/onboarding", {"action": "save_draft", "expectedRevision": revision,
                                                          "draft": {"x": "y" * 17000}}, token)
            self.assertEqual(code, 400, body)

    def test_skip_all_and_finish_are_recorded(self):
        with DemoServer() as server:
            code, skipped_user = register(server, "2024001020")
            self.assertEqual(code, 201)
            token = skipped_user["token"]
            progress = server.get("/api/onboarding", token)[1]["progress"]
            code, skipped = server.post("/api/onboarding", {
                "action": "skip_all", "expectedRevision": progress["revision"]
            }, token)
            self.assertEqual(code, 200, skipped)
            self.assertEqual(skipped["progress"]["status"], "finished_with_skips")
            self.assertEqual(skipped["progress"]["completedCount"], 0)
            self.assertEqual(skipped["progress"]["skippedCount"], 4)
            self.assertFalse(skipped["progress"]["autoOpenPending"])
            self.assertIsNotNone(skipped["progress"]["finishedAt"])

            code, finished_user = register(server, "2024001021")
            self.assertEqual(code, 201)
            token = finished_user["token"]
            progress = server.get("/api/onboarding", token)[1]["progress"]
            for step_id in ("team-basics", "reuse-teams", "first-work", "explore"):
                code, started = server.post("/api/onboarding", {
                    "action": "start", "stepId": step_id, "expectedRevision": progress["revision"]
                }, token)
                self.assertEqual(code, 200, started)
                code, completed = server.post("/api/onboarding", {
                    "action": "complete", "stepId": step_id, "completionKind": "demo",
                    "expectedRevision": started["progress"]["revision"]
                }, token)
                self.assertEqual(code, 200, completed)
                progress = completed["progress"]
            code, finished = server.post("/api/onboarding", {
                "action": "finish", "destination": "gallery", "expectedRevision": progress["revision"]
            }, token)
            self.assertEqual(code, 200, finished)
            self.assertEqual(finished["progress"]["status"], "completed")
            self.assertEqual(finished["progress"]["completedCount"], 4)
            self.assertIsNotNone(finished["progress"]["finishedAt"])


if __name__ == "__main__":
    unittest.main()
