"""
app/cli.py
=============================================================================
KiranaIQ Administrative Provisioning & Account Management CLI
=============================================================================
Provides secure, terminal-only commands for:
1. First-time administrator creation (python -m app.cli create-admin)
2. Safe local password recovery (python -m app.cli reset-password)
3. Accidental account lockout recovery (python -m app.cli unlock-account)
4. Audit listing of registered administrators (python -m app.cli list-admins)

Security Guarantees:
- Never prints plaintext passwords or hashes to terminal stdout/stderr.
- Employs getpass.getpass() for interactive hidden password entry.
- Uses OWASP-recommended PBKDF2-HMAC-SHA256 password hashing.
- Enforces password strength rules (8+ chars, upper, lower, numeric, special).
- Prohibits public web admin creation; requires explicit local terminal access.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from typing import Optional

from app.config import settings
from app.models import User
from app.services.auth import (
    hash_password,
    validate_email_format,
    validate_password_strength,
    validate_username,
)
from app.services.security import login_tracker
from app.storage.sqlite_store import SQLiteStore


def get_store(db_path: Optional[str] = None) -> SQLiteStore:
    """Initialize and return the SQLite storage backend."""
    resolved_path = db_path or settings.sqlite_db_path or "data/bills.db"
    store = SQLiteStore(db_path=resolved_path)
    store.create_tables()
    return store


def cmd_create_admin(args: argparse.Namespace) -> int:
    """
    Provision the initial administrator account.
    Fails if an administrator already exists unless --force is provided.
    """
    store = get_store(args.db_path)
    current_count = store.count_users()

    if current_count > 0 and not args.force:
        sys.stderr.write(
            "\n[!] Error: An administrator account already exists.\n"
            "    Initial setup can only provision one primary administrator.\n"
            "    To reset a forgotten password, run:\n"
            "      python -m app.cli reset-password\n"
            "    To force additional admin creation, use --force.\n\n"
        )
        return 1

    # Username acquisition
    username = (args.username or "").strip()
    if not username:
        try:
            username = input("Enter administrator username: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stdout.write("\nAborted.\n")
            return 130

    valid_u, u_err = validate_username(username)
    if not valid_u:
        sys.stderr.write(f"\n[!] Invalid username: {u_err}\n")
        return 1

    # Email acquisition
    email = (args.email or "").strip()
    if not email:
        try:
            email = input("Enter administrator email: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stdout.write("\nAborted.\n")
            return 130

    valid_e, e_err = validate_email_format(email)
    if not valid_e:
        sys.stderr.write(f"\n[!] Invalid email: {e_err}\n")
        return 1

    # Check existing uniqueness
    if store.get_user_by_username(username):
        sys.stderr.write(f"\n[!] Error: Username '{username}' is already in use.\n")
        return 1
    if store.get_user_by_email(email):
        sys.stderr.write(f"\n[!] Error: Email '{email}' is already in use.\n")
        return 1

    # Password acquisition
    password = args.password or ""
    if not password:
        try:
            password = getpass.getpass("Enter administrator password: ")
            confirm = getpass.getpass("Confirm administrator password: ")
        except (KeyboardInterrupt, EOFError):
            sys.stdout.write("\nAborted.\n")
            return 130

        if password != confirm:
            sys.stderr.write("\n[!] Error: Passwords do not match.\n")
            return 1
    elif hasattr(args, "confirm_password") and args.confirm_password and args.password != args.confirm_password:
        sys.stderr.write("\n[!] Error: Passwords do not match.\n")
        return 1

    valid_p, p_err = validate_password_strength(password)
    if not valid_p:
        sys.stderr.write(f"\n[!] Insecure password: {p_err}\n")
        return 1

    # Hash and persist
    p_hash = hash_password(password)
    new_user = User(
        user_id=f"usr_{secrets.token_hex(8)}",
        username=username,
        email=email.lower(),
        password_hash=p_hash,
        role="admin",
    )
    store.insert_user(new_user)

    sys.stdout.write(
        f"\n============================================================\n"
        f"✓ Administrator Successfully Provisioned\n"
        f"============================================================\n"
        f"  Username  : {new_user.username}\n"
        f"  Email     : {new_user.email}\n"
        f"  Role      : {new_user.role}\n"
        f"  Storage   : Persistent SQLite ({store._db_path})\n\n"
        f"You may now sign in at the KiranaIQ Dashboard:\n"
        f"  http://localhost:8000/ui/login\n\n"
    )
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    """
    Safely reset the administrator password using explicit local CLI access.
    Also clears any temporary login lockouts.
    """
    store = get_store(args.db_path)

    identifier = (args.identifier or "").strip()
    if not identifier:
        try:
            identifier = input("Enter administrator username or email: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stdout.write("\nAborted.\n")
            return 130

    user = store.get_user_by_identifier(identifier)
    if not user:
        sys.stderr.write(f"\n[!] Error: No user account found matching '{identifier}'.\n")
        return 1

    password = args.password or ""
    if not password:
        try:
            password = getpass.getpass(f"Enter new password for '{user.username}': ")
            confirm = getpass.getpass("Confirm new password: ")
        except (KeyboardInterrupt, EOFError):
            sys.stdout.write("\nAborted.\n")
            return 130

        if password != confirm:
            sys.stderr.write("\n[!] Error: Passwords do not match.\n")
            return 1

    valid_p, p_err = validate_password_strength(password)
    if not valid_p:
        sys.stderr.write(f"\n[!] Insecure password: {p_err}\n")
        return 1

    # Update password in database
    p_hash = hash_password(password)
    store.update_user_password(user.user_id, p_hash)

    # Clear account lockout
    login_tracker.reset()

    sys.stdout.write(
        f"\n============================================================\n"
        f"✓ Password Successfully Reset\n"
        f"============================================================\n"
        f"  Account   : {user.username} ({user.email})\n"
        f"  Status    : Password updated & login lockouts cleared.\n\n"
        f"Sign in with your new password at:\n"
        f"  http://localhost:8000/ui/login\n\n"
    )
    return 0


def cmd_unlock_account(args: argparse.Namespace) -> int:
    """
    Recover from accidental account lockout by resetting rate limiters and lockout counters.
    """
    username = (args.username or "").strip()
    login_tracker.reset()

    if username:
        sys.stdout.write(f"\n✓ Account lockout cleared for '{username}'.\n\n")
    else:
        sys.stdout.write("\n✓ All account lockouts and rate limiter counters successfully cleared.\n\n")
    return 0


def cmd_list_admins(args: argparse.Namespace) -> int:
    """
    List all provisioned administrator accounts without displaying credentials.
    """
    store = get_store(args.db_path)
    users = store.list_users()

    if not users:
        sys.stdout.write("\nNo administrator accounts currently provisioned.\n")
        sys.stdout.write("Run 'python -m app.cli create-admin' to create one.\n\n")
        return 0

    sys.stdout.write(f"\n{'USER ID':<22} {'USERNAME':<18} {'ROLE':<8} {'EMAIL':<30} {'CREATED AT'}\n")
    sys.stdout.write("-" * 95 + "\n")
    for u in users:
        created_str = u.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if u.created_at else "N/A"
        sys.stdout.write(f"{u.user_id:<22} {u.username:<18} {u.role:<8} {u.email:<30} {created_str}\n")
    sys.stdout.write("-" * 95 + f"\nTotal: {len(users)} administrator(s)\n\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="KiranaIQ Production Administrative Provisioning & Recovery CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Administrative commands")

    # 1. create-admin
    p_create = subparsers.add_parser("create-admin", help="Provision initial administrator account")
    p_create.add_argument("--username", "-u", help="Administrator username (3-30 chars)")
    p_create.add_argument("--email", "-e", help="Administrator email address")
    p_create.add_argument("--password", "-p", help="Administrator password (optional, prompted if omitted)")
    p_create.add_argument("--confirm-password", help="Password confirmation for non-interactive scripts")
    p_create.add_argument("--force", "-f", action="store_true", help="Force creation even if an admin already exists")
    p_create.add_argument("--db-path", help="Path to SQLite database file")
    p_create.set_defaults(func=cmd_create_admin)

    # 2. reset-password
    p_reset = subparsers.add_parser("reset-password", help="Reset administrator password locally")
    p_reset.add_argument("--identifier", "-i", help="Administrator username or email")
    p_reset.add_argument("--password", "-p", help="New administrator password (prompted if omitted)")
    p_reset.add_argument("--db-path", help="Path to SQLite database file")
    p_reset.set_defaults(func=cmd_reset_password)

    # 3. unlock-account
    p_unlock = subparsers.add_parser("unlock-account", help="Clear temporary login lockouts")
    p_unlock.add_argument("--username", "-u", help="Specific username to unlock (or clears all if omitted)")
    p_unlock.set_defaults(func=cmd_unlock_account)

    # 4. list-admins
    p_list = subparsers.add_parser("list-admins", help="List provisioned administrators")
    p_list.add_argument("--db-path", help="Path to SQLite database file")
    p_list.set_defaults(func=cmd_list_admins)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
