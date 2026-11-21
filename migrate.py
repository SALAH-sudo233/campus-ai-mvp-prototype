"""显式运行数据库迁移：python3 migrate.py [数据库路径]。可重复执行。"""
import sys

import migrations
import relay
import server


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else server.DB_PATH
    applied = migrations.migrate(path)
    relay.init(server.RELAY_DB)
    migrations.backfill_relay(server.RELAY_DB, path)
    print("applied:", applied)
    print("versions:", migrations.status(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
