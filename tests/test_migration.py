import os
import sqlite3
import tempfile
import unittest

import config
import migrations
import relay


class MigrationTests(unittest.TestCase):
    def test_v11_upgrade_preserves_real_accounts_and_seeds_four_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "v11.sqlite")
            original = migrations.MIGRATIONS
            try:
                migrations.MIGRATIONS = original[:-1]
                migrations.migrate(db_path)
            finally:
                migrations.MIGRATIONS = original
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE org_memberships SET role = 'org_admin' WHERE student_id = '2024001001'")
                before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                membership = conn.execute("SELECT * FROM org_memberships WHERE student_id = '2024001001'").fetchone()
            self.assertEqual(migrations.migrate(db_path), [12])
            self.assertEqual(migrations.migrate(db_path), [])
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], before + 4)
                self.assertEqual(conn.execute("SELECT * FROM org_memberships WHERE student_id = '2024001001'").fetchone(),
                                 membership)
                roles = {row[0] for row in conn.execute("SELECT role FROM org_memberships WHERE external_ref LIKE 'four-end-demo:%'")}
                self.assertEqual(roles, {"org_member", "org_operator", "college_viewer"})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM platform_admins").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM org_memberships WHERE org_id = ?",
                                              (config.PILOT_ORG["id"],)).fetchone()[0], 200)
                demo_org = conn.execute("SELECT id FROM organizations WHERE slug = 'four-end-demo'").fetchone()[0]
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM works WHERE org_id = ?", (demo_org,)).fetchone()[0],
                                 len(config.SEED_WORKS))

    def test_rerun_is_idempotent_and_backfills_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy.sqlite")
            conn = sqlite3.connect(db_path)
            for statement in migrations.LEGACY_SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at) "
                         "VALUES ('stu_roster', 'student', '2024001001', '00', '00', 777, 23, 1)")
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at) "
                         "VALUES ('stu_outside', 'student', '2024999999', '00', '00', 4321, 99, 1)")
            conn.execute("INSERT INTO ledger (owner_ref, title, amount, kind, request_id, created_at) "
                         "VALUES ('stu_outside', '旧流水', -99, 'use', 'old-1', 1)")
            conn.execute("INSERT INTO sessions (token_hash, owner_ref, expires_at) VALUES ('hash', 'stu_outside', 9999999999)")
            conn.commit()
            conn.close()

            applied = migrations.migrate(db_path)
            self.assertEqual(applied, list(range(1, 13)))
            self.assertEqual(migrations.migrate(db_path), [])

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            roster = conn.execute("SELECT * FROM students WHERE owner_ref = 'stu_roster'").fetchone()
            outside = conn.execute("SELECT * FROM students WHERE owner_ref = 'stu_outside'").fetchone()
            self.assertEqual(roster["balance"], 777)
            self.assertEqual(outside["balance"], 4321)
            self.assertIsNotNone(roster["user_id"])
            self.assertEqual(roster["org_id"], config.PILOT_ORG["id"])
            roster_membership = conn.execute("SELECT * FROM org_memberships WHERE user_id = ?", (roster["user_id"],)).fetchone()
            outside_membership = conn.execute("SELECT * FROM org_memberships WHERE user_id = ?", (outside["user_id"],)).fetchone()
            self.assertEqual(roster_membership["admission_status"], "active")
            self.assertEqual(outside_membership["admission_status"], "pending_review")
            ledger = conn.execute("SELECT * FROM ledger WHERE owner_ref = 'stu_outside'").fetchone()
            self.assertEqual(ledger["user_id"], outside["user_id"])
            self.assertEqual(ledger["org_id"], config.PILOT_ORG["id"])
            session = conn.execute("SELECT * FROM sessions WHERE owner_ref = 'stu_outside'").fetchone()
            self.assertEqual(session["user_id"], outside["user_id"])
            count = conn.execute("SELECT COUNT(*) FROM org_memberships WHERE org_id = ? AND admission_status = 'active'",
                                 (config.PILOT_ORG["id"],)).fetchone()[0]
            self.assertGreaterEqual(count, 200)
            quota = conn.execute("SELECT quota_limit FROM organizations WHERE id = ?",
                                 (config.PILOT_ORG["id"],)).fetchone()[0]
            self.assertEqual(quota, config.PILOT_TOTAL_QUOTA)
            demo_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            self.assertIn("demo", demo_columns)
            versions = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(versions, 12)
            conn.close()

    def test_failed_version_rolls_back_without_half_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "broken.sqlite")
            original = migrations.MIGRATIONS

            def broken(conn):
                conn.execute("CREATE TABLE half_write (id INTEGER PRIMARY KEY)")
                raise RuntimeError("migration failed")

            migrations.MIGRATIONS = original[:2] + [(99, "broken", broken)]
            try:
                with self.assertRaises(RuntimeError):
                    migrations.migrate(db_path)
            finally:
                migrations.MIGRATIONS = original

            conn = sqlite3.connect(db_path)
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            conn.close()
            self.assertEqual(versions, [1, 2])
            self.assertNotIn("half_write", tables)
            self.assertEqual(migrations.migrate(db_path), list(range(3, 13)))

    def test_relay_backfill_fills_user_and_org(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "main.sqlite")
            relay_path = os.path.join(tmp, "relay.sqlite")
            conn = sqlite3.connect(db_path)
            for statement in migrations.LEGACY_SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO students (owner_ref, mode, identity, pw_salt, pw_hash, balance, spent, created_at) "
                         "VALUES ('stu_legacy2', 'student', '2024001002', '00', '00', 10, 0, 1)")
            conn.commit()
            conn.close()

            migrations.migrate(db_path)
            relay.init(relay_path)
            relay.upsert_account(relay_path, "stu_legacy2", "student", "2024001002", "测试学生")
            relay.issue_key(relay_path, "stu_legacy2", "issue:legacy")
            migrations.backfill_relay(relay_path, db_path)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            user_id = conn.execute("SELECT user_id FROM students WHERE owner_ref = 'stu_legacy2'").fetchone()["user_id"]
            conn.close()
            relay_conn = sqlite3.connect(relay_path)
            relay_conn.row_factory = sqlite3.Row
            account = relay_conn.execute("SELECT * FROM relay_accounts WHERE owner_ref = 'stu_legacy2'").fetchone()
            key = relay_conn.execute("SELECT * FROM relay_keys WHERE owner_ref = 'stu_legacy2'").fetchone()
            relay_conn.close()
            self.assertEqual(account["user_id"], user_id)
            self.assertEqual(account["org_id"], config.PILOT_ORG["id"])
            self.assertEqual(key["user_id"], user_id)


if __name__ == "__main__":
    unittest.main()
