#!/usr/bin/env python
"""CrewKeep CLI — user management + dev server.

Usage:
  crewkeep.py serve            # uvicorn on CREWKEEP_HOST/PORT (default 0.0.0.0:8091)
  crewkeep.py users list
  crewkeep.py users add USERNAME PASSWORD [--name "Display"] [--admin]
  crewkeep.py users password USERNAME NEW_PASSWORD
  crewkeep.py users delete USERNAME
  crewkeep.py llm-test         # live engine check
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="crewkeep")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the dev server")

    users = sub.add_parser("users", help="manage users")
    u_sub = users.add_subparsers(dest="u_cmd", required=True)
    u_sub.add_parser("list")
    add = u_sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("password")
    add.add_argument("--name", default="")
    add.add_argument("--admin", action="store_true")
    pw = u_sub.add_parser("password")
    pw.add_argument("username")
    pw.add_argument("new_password")
    rm = u_sub.add_parser("delete")
    rm.add_argument("username")
    adm = u_sub.add_parser("admin")
    adm.add_argument("username")
    adm.add_argument("--yes", action="store_true", help="grant admin")
    adm.add_argument("--no", action="store_true", help="revoke admin")

    sub.add_parser("llm-test")

    args = parser.parse_args()

    if args.cmd == "serve":
        import app
        app.main()
        return
    if args.cmd == "llm-test":
        import llm
        import json
        print(json.dumps(llm.test_connection(), indent=2))
        sys.exit(0 if llm.test_connection()["ok"] else 1)
    if args.cmd == "users":
        import auth
        if args.u_cmd == "list":
            for u in auth.list_users():
                role = "admin" if u["is_admin"] else "user"
                print(f"{u['username']:20} {role:6} {u['display_name']}")
            return
        if args.u_cmd == "add":
            try:
                u = auth.create_user(args.username, args.password,
                                     args.name, is_admin=args.admin)
                print(f"created {u['username']} (admin={u['is_admin']})")
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                sys.exit(1)
            return
        if args.u_cmd == "password":
            auth.set_password(args.username, args.new_password)
            print("password updated")
            return
        if args.u_cmd == "delete":
            auth.delete_user(args.username)
            print("deleted")
            return
        if args.u_cmd == "admin":
            if args.yes == args.no:
                print("error: pass exactly one of --yes / --no", file=sys.stderr)
                sys.exit(1)
            u = auth.set_admin(args.username, is_admin=args.yes)
            if not u:
                print(f"error: no such user '{args.username}'", file=sys.stderr)
                sys.exit(1)
            print(f"{u['username']} is now {'admin' if u['is_admin'] else 'user'}")


if __name__ == "__main__":
    main()
