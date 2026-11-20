"""兼容入口。实现位于 app_server.py，python3 server.py 的行为不变。"""
from app_server import Handler, Server, do_consume, do_login, do_me, do_register, do_rotate, main, setup


if __name__ == "__main__":
    main()
