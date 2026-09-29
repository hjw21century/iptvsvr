"""命令行入口：python3 -m iptvhub <命令>"""

import argparse
import json
import sys

from .config import load_config, load_sources
from .util import human_time, setup_logging


def cmd_run(args) -> int:
    from .pipeline import Pipeline
    pipeline = Pipeline(load_config(args.config))
    summary = pipeline.run(limit=args.limit, skip_probe=args.skip_probe,
                           recheck_all=args.recheck_all)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_export(args) -> int:
    from .pipeline import Pipeline
    pipeline = Pipeline(load_config(args.config))
    channels = pipeline.rescore_and_export()
    print("已导出 %d 个频道到 %s" % (len(channels), pipeline.cfg["paths"]["data"]))
    return 0


def cmd_static(args) -> int:
    """导出一份纯静态站点（可直接丢给 GitHub Pages / nginx 静态目录）。"""
    import os
    import shutil
    cfg = load_config(args.config)
    out = os.path.abspath(args.out)
    os.makedirs(os.path.join(out, "static"), exist_ok=True)
    shutil.copy(os.path.join(cfg["paths"]["web"], "index.html"), os.path.join(out, "index.html"))
    for name in ("style.css", "app.js"):
        shutil.copy(os.path.join(cfg["paths"]["web"], name), os.path.join(out, "static", name))
    exported = []
    for name in ("channels.json", "playlist.m3u", "playlist.m3u8", "playlist.txt",
                 "playlist_full.m3u"):
        source = os.path.join(cfg["paths"]["data"], name)
        if os.path.exists(source):
            shutil.copy(source, os.path.join(out, name))
            exported.append(name)
    print("静态站点已导出到 %s（%s）" % (out, ", ".join(exported)))
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    cfg = load_config(args.config)
    if args.host:
        cfg["server"]["host"] = args.host
    if args.port:
        cfg["server"]["port"] = args.port
    if args.no_auto_update:
        cfg["server"]["auto_update"] = False
    serve(cfg)
    return 0


def cmd_stats(args) -> int:
    from .store import Store
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    stats = store.stats()
    print("频道数        : %d" % stats["channels_total"])
    print("源总数/可用   : %d / %d" % (stats["streams_total"], stats["streams_alive"]))
    print("平均首包延迟  : %.0f ms" % stats["avg_ttfb_ms"])
    print("平均实测吞吐  : %.0f kbps" % stats["avg_kbps"])
    last = stats.get("last_run")
    if last:
        print("最近一次更新  : %s (%s)" % (human_time(last["started_at"]), last.get("note", "")))
    print("\n分组分布：")
    for group in sorted(stats["groups"], key=lambda g: -g["channels"]):
        print("  %-12s 频道 %4d  源 %5d" % (group["group_title"], group["channels"], group["streams"]))
    return 0


def cmd_token(args) -> int:
    """查看或重置管理后台令牌。"""
    from .admin import AdminApi
    from .store import Store
    cfg = load_config(args.config)
    admin = AdminApi(cfg, Store(cfg["paths"]["db"]), None, None)
    if args.reset:
        import os
        import secrets
        token = secrets.token_hex(16)
        with open(admin.token_path, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(admin.token_path, 0o600)
        print("已重置令牌（重启服务后生效）")
    configured = (cfg.get("server", {}).get("admin_token") or "").strip()
    print("管理后台 : %s/admin" % (cfg.get("site_url") or "http://127.0.0.1:%d" % cfg["server"]["port"]))
    print("令牌     : %s" % admin.token())
    print("来源     : %s" % ("config.json server.admin_token" if configured else admin.token_path))
    return 0


def cmd_user(args) -> int:
    """账号管理：add / list / passwd / role / delete / key / disable。"""
    import getpass
    from .auth import Auth
    from .store import Store
    cfg = load_config(args.config)
    auth = Auth(Store(cfg["paths"]["db"]), cfg)

    def ask_password() -> str:
        if args.password:
            return args.password
        first = getpass.getpass("密码: ")
        if first != getpass.getpass("再输一次: "):
            raise ValueError("两次输入不一致")
        return first

    try:
        if args.action == "list":
            users = auth.list_users()
            if not users:
                print("还没有任何账号，用 `user add <用户名> --role admin` 创建")
                return 0
            print("%-16s %-6s %-26s %s" % ("用户名", "角色", "订阅密钥", "最后登录"))
            for user in users:
                print("%-16s %-6s %-26s %s%s" % (
                    user["username"], user["role"], user["sub_key"],
                    human_time(user["last_login"]) if user["last_login"] else "从未",
                    "  [已停用]" if user["disabled"] else ""))
            return 0

        if args.action == "add":
            user = auth.create_user(args.username, ask_password(), args.role)
            print("已创建 %s（%s）" % (user["username"], user["role"]))
            print("订阅密钥: %s" % user["sub_key"])
            return 0

        if args.action == "passwd":
            auth.set_password(args.username, ask_password())
            auth.destroy_user_sessions(args.username)
            print("已修改密码，该账号的登录状态已全部失效")
            return 0

        if args.action == "role":
            auth.set_role(args.username, args.role)
            print("已把 %s 的角色设为 %s" % (args.username, args.role))
            return 0

        if args.action == "disable":
            auth.set_disabled(args.username, True)
            print("已停用 %s" % args.username)
            return 0

        if args.action == "enable":
            auth.set_disabled(args.username, False)
            print("已启用 %s" % args.username)
            return 0

        if args.action == "key":
            print("新的订阅密钥: %s" % auth.regenerate_key(args.username))
            return 0

        if args.action == "delete":
            print("已删除 %s" % args.username if auth.delete_user(args.username)
                  else "没有这个账号")
            return 0
    except (ValueError, PermissionError) as exc:
        print("失败：%s" % exc)
        return 1

    print("未知操作")
    return 1


def cmd_probe(args) -> int:
    from .netclient import HttpClient
    from .probe import Prober
    cfg = load_config(args.config)
    prober = Prober(HttpClient(cfg), cfg)
    for url in args.urls:
        result = prober.probe(url)
        print("%s\n  %s" % (url, json.dumps(result.as_dict(), ensure_ascii=False)))
    return 0


def cmd_sources(args) -> int:
    from .netclient import HttpClient
    from .parser import NoiseFilter, parse_playlist
    from .config import load_groups
    cfg = load_config(args.config)
    client = HttpClient(cfg)
    noise = NoiseFilter(load_groups().get("blocked_keywords", []))
    for source in load_sources():
        status, text = client.fetch_text(source["url"])
        entries = parse_playlist(text, source["type"], source["url"], source["name"],
                                 source["weight"], noise) if status == 200 else []
        print("%-18s status=%-3s 条目=%-6d %s" % (source["name"], status, len(entries), source["url"]))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="iptvhub", description="IPTV-Hub 自建直播源服务")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="执行一次完整更新（采集/探测/评分/导出）")
    run.add_argument("--limit", type=int, default=0, help="只探测前 N 条（调试用）")
    run.add_argument("--skip-probe", action="store_true", help="只采集不探测")
    run.add_argument("--recheck-all", action="store_true", help="忽略失效冷却，全部重测")
    run.set_defaults(func=cmd_run)

    export = sub.add_parser("export", help="用库中已有数据重新评分并导出播放列表")
    export.set_defaults(func=cmd_export)

    static = sub.add_parser("static", help="导出纯静态站点（含网页与播放列表）")
    static.add_argument("--out", default="public", help="输出目录，默认 public/")
    static.set_defaults(func=cmd_static)

    serve = sub.add_parser("serve", help="启动 HTTP 服务（API + 网页 + 播放列表）")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-auto-update", action="store_true", help="不启用内置定时更新")
    serve.set_defaults(func=cmd_serve)

    stats = sub.add_parser("stats", help="查看当前库内统计")
    stats.set_defaults(func=cmd_stats)

    token = sub.add_parser("token", help="查看/重置管理后台令牌")
    token.add_argument("--reset", action="store_true", help="生成新令牌")
    token.set_defaults(func=cmd_token)

    user = sub.add_parser("user", help="账号管理（登录认证）")
    user.add_argument("action",
                      choices=["add", "list", "passwd", "role", "delete", "key",
                               "disable", "enable"])
    user.add_argument("username", nargs="?", default="")
    user.add_argument("--password", help="不加则交互式输入，避免留在命令历史里")
    user.add_argument("--role", default="user", choices=["admin", "user"])
    user.set_defaults(func=cmd_user)

    probe = sub.add_parser("probe", help="调试：深度探测指定链接")
    probe.add_argument("urls", nargs="+")
    probe.set_defaults(func=cmd_probe)

    sources = sub.add_parser("sources", help="检查上游源清单可用性")
    sources.set_defaults(func=cmd_sources)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
