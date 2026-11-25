"""显式运行数据库迁移：python3 migrate.py [数据库路径]。可重复执行。

破坏性切换 New-API campus: token 使用独立入口：

    python3 migrate.py --relay-cutover            # 只列出，不删除
    python3 migrate.py --relay-cutover --apply    # 备份后要求输入 DELETE 确认
"""
import argparse

import app_server
import migrations
import relay


def _print_cutover(result):
    mode = "apply" if result["apply"] else "dry-run"
    print("relay-cutover:", mode, "matched:", result["matched"], "deleted:", result["deleted"])
    for token in result["tokens"]:
        print("  id=%s name=%s" % (token.get("id"), token.get("name")))
    if result.get("backup"):
        print("backup:", result["backup"])
    if result.get("manifest"):
        print("manifest:", result["manifest"])
    for item in result["failed"]:
        print("failed: id=%s error=%s" % (item.get("id"), item.get("error")))
    return 0 if not result["failed"] else 1


def _relay_cutover(args):
    client = relay.client_from_env()
    if not client.config.enabled:
        print("relay-cutover: New-API 未启用或凭据缺失（NEW_API_ENABLED/NEW_API_BASE_URL/NEW_API_TOKEN）")
        return 2
    confirm = None
    if args.apply:
        preview = relay.relay_cutover(client, relay_db=app_server.RELAY_DB, apply=False)
        _print_cutover(preview)
        if not preview["matched"]:
            return 0
        try:
            confirm = input("将删除上述 campus: 前缀 token。输入 DELETE 确认：").strip()
        except EOFError:
            confirm = None
    try:
        result = relay.relay_cutover(client, relay_db=app_server.RELAY_DB, apply=args.apply,
                                     backup_dir=args.backup, confirm=confirm)
    except ValueError as exc:
        print("relay-cutover:", exc)
        return 2
    return _print_cutover(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description="校园平台数据库迁移与切换工具")
    parser.add_argument("db", nargs="?", default=app_server.DB_PATH, help="主数据库路径")
    parser.add_argument("--relay-cutover", action="store_true", help="清理 New-API 中 campus: 前缀 token")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认只 dry-run）")
    parser.add_argument("--backup", help="切换备份目录")
    args = parser.parse_args(argv)
    if args.relay_cutover:
        return _relay_cutover(args)
    applied = migrations.migrate(args.db)
    relay.init(app_server.RELAY_DB)
    migrations.backfill_relay(app_server.RELAY_DB, args.db)
    print("applied:", applied)
    print("versions:", migrations.status(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
