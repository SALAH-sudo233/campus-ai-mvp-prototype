import unittest

import config
import importer
from tests.support import DemoServer, register

HEADER = "external_ref,student_id,email,name,role,status\n"


def csv_bytes(*rows):
    return (HEADER + "".join(rows)).encode("utf-8")


class ImportTests(unittest.TestCase):
    def test_preview_does_not_write_and_commit_is_idempotent(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, org = server.post("/api/orgs", {"name": "导入学院", "slug": "import-college"}, admin)
            self.assertEqual(code, 201, org)
            org_id = org["id"]
            before = server.get("/api/orgs/%s/members" % org_id, admin)[1]["members"]
            body = csv_bytes("ext-1,2024999001,2024999001@example.edu.cn,张三,学生,active\n"
                             "ext-2,2024999002,2024999002@example.edu.cn,李四,教师,disabled\n")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % org_id, raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview["counts"]["accepted"], 2)
            after = server.get("/api/orgs/%s/members" % org_id, admin)[1]["members"]
            self.assertEqual(len(before), len(after))
            self.assertNotIn("2024999001@example.edu.cn", str(preview))
            code, first = server.post("/api/orgs/%s/imports/%s/commit" % (org_id, preview["batchId"]),
                                      {"requestId": "commit-1"}, admin)
            self.assertEqual(code, 200, first)
            self.assertEqual(first["counts"]["accepted"], 2)
            members = server.get("/api/orgs/%s/members" % org_id, admin)[1]["members"]
            self.assertEqual(len(members), 2)
            code, second = server.post("/api/orgs/%s/imports/%s/commit" % (org_id, preview["batchId"]),
                                       {"requestId": "commit-1"}, admin)
            self.assertEqual(code, 200)
            self.assertEqual(second["counts"], first["counts"])
            self.assertEqual(len(server.get("/api/orgs/%s/members" % org_id, admin)[1]["members"]), 2)
            audit = server.get("/api/orgs/%s/audit" % org_id, admin)[1]["events"]
            self.assertEqual(sum(1 for event in audit if event["action"] == "import.commit"), 1)

    def test_partial_failure_duplicates_and_formula_prefix(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, org = server.post("/api/orgs", {"name": "校验学院", "slug": "check-college"}, admin)
            org_id = org["id"]
            body = csv_bytes(
                "ext-a,2024999101,a@example.edu.cn,正常学生,学生,active\n"
                "ext-a,2024999102,b@example.edu.cn,重复外部编号,org_member,active\n"
                "ext-b,2024999103,c@example.edu.cn,=公式,org_member,active\n"
                "ext-c,2024999104,d@example.edu.cn,坏角色,teacher,active\n")
            code, preview = server.request("POST", "/api/orgs/%s/imports/preview" % org_id, raw=body, token=admin)
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview["counts"]["accepted"], 1)
            self.assertEqual(preview["counts"]["rejected"], 3)
            reasons = {row["reasonCode"] for row in preview["rows"] if row["rowNo"] > 1}
            self.assertIn("duplicate_in_file", reasons)
            self.assertIn("formula_prefix", reasons)
            self.assertIn("invalid_role", reasons)
            code, first = server.post("/api/orgs/%s/imports/%s/commit" % (org_id, preview["batchId"]),
                                      {"requestId": "commit-partial"}, admin)
            self.assertEqual(first["counts"]["accepted"], 1)
            self.assertEqual(first["counts"]["rejected"], 3)
            code, report = server.request("GET", "/api/orgs/%s/imports/%s/errors.csv" % (org_id, preview["batchId"]), token=admin)
            self.assertEqual(code, 200)
            text = report.decode("utf-8")
            self.assertIn("formula_prefix", text)
            self.assertNotIn("2024999103", text)
            self.assertNotIn("c@example.edu.cn", text)
            self.assertNotIn("2024999104", text)

    def test_cross_org_import_is_forbidden_before_parsing(self):
        with DemoServer() as server:
            admin = server.admin_token()
            code, org = server.post("/api/orgs", {"name": "别的学院", "slug": "other-college"}, admin)
            other_id = org["id"]
            code, data = register(server, "2024001005")
            token = data["token"]
            members = server.get("/api/orgs/%s/members" % config.PILOT_ORG["id"], admin)[1]["members"]
            user = next(item for item in members if item["studentId"] == "2024001005")
            server.patch("/api/orgs/%s/members/%s" % (config.PILOT_ORG["id"], user["userId"]),
                         {"role": "org_admin"}, admin)
            code, body = server.request("POST", "/api/orgs/%s/imports/preview" % other_id,
                                        raw=b"not even a csv", token=token)
            self.assertEqual(code, 403, body)

    def test_limits_are_enforced(self):
        with self.assertRaises(importer.ImportError_) as too_big:
            importer.parse(b"x" * (importer.MAX_BYTES + 1))
        self.assertEqual(too_big.exception.code, "file_too_large")
        rows = HEADER + "".join("e%d,2024%06d,x%d@example.edu.cn,n,org_member,active\n" % (i, i, i)
                                 for i in range(importer.MAX_ROWS + 1))
        with self.assertRaises(importer.ImportError_) as too_many:
            importer.parse(rows.encode("utf-8"))
        self.assertEqual(too_many.exception.code, "row_limit_exceeded")

    def test_line_field_quote_and_control_limits(self):
        long_line = HEADER + "e-1,2024001001,x@example.edu.cn,n," + "a" * 4100 + ",org_member,active\n"
        with self.assertRaises(importer.ImportError_) as line_error:
            importer.parse(long_line.encode("utf-8"))
        self.assertEqual(line_error.exception.code, "line_too_long")

        long_field = HEADER + "x" * 300 + ",2024001001,x@example.edu.cn,n,org_member,active\n"
        rows = importer.parse(long_field.encode("utf-8"))
        self.assertEqual(rows[0]["reason_code"], "field_too_long")

        control = HEADER + "e-2,2024001001,x@example.edu.cn,坏\x01名字,org_member,active\n"
        rows = importer.parse(control.encode("utf-8"))
        self.assertEqual(rows[0]["reason_code"], "control_character")

        with self.assertRaises(importer.ImportError_) as quote_error:
            importer.parse((HEADER + 'e-3,2024001001,"x@example.edu.cn,n,org_member,active\n').encode("utf-8"))
        self.assertEqual(quote_error.exception.code, "invalid_quote")


if __name__ == "__main__":
    unittest.main()
