"""
scripts/create_admin.py
-----------------------
Bootstrap an initial admin user directly into the database.

Usage:
    python -m scripts.create_admin --username admin --password yourpassword

Options:
    --username  Username for the new admin (default: admin)
    --password  Plaintext password (required)
    --db-url    Override DATABASE_URL env var

Reads DATABASE_URL from environment if --db-url is not provided.
Falls back to: postgresql+psycopg2://libraryai:changeme@localhost:5432/libraryai
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import bcrypt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


def get_password_hash(password: str) -> str:
    return str(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())


def main() -> None:
    parser = argparse.ArgumentParser(description="Create initial admin user")
    parser.add_argument(
        "--username",
        default=os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin"),
        help="Admin username (default: $BOOTSTRAP_ADMIN_USERNAME or 'admin')",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"),
        required=not os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"),
        help="Plaintext password (default: $BOOTSTRAP_ADMIN_PASSWORD)",
    )
    parser.add_argument("--db-url", default=None, help="Override DATABASE_URL")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing user with the same username"
            " (updates password and sets role=admin)"
        ),
    )
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help=(
            "Exit 0 silently if the username already exists. "
            "Used for automated bootstrap (compose/k8s) where the script "
            "runs on every container start."
        ),
    )
    args = parser.parse_args()

    # Prefer sync psycopg2 driver for this script
    db_url: str = args.db_url or os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://libraryai:changeme@localhost:5432/libraryai",
    )
    # Replace asyncpg driver with psycopg2 if needed (script is synchronous)
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

    engine = create_engine(db_url)

    with Session(engine) as session:
        # Check if username already exists
        existing = session.execute(
            text("SELECT user_id FROM users WHERE username = :u"),
            {"u": args.username},
        ).fetchone()

        if existing:
            if args.skip_if_exists:
                print(f"[~] Admin user '{args.username}' already exists — skipping bootstrap.")
                return
            if not args.force:
                print(
                    f"[!] User '{args.username}' already exists (user_id={existing[0]}). Aborting."
                )
                print("    Use --force to overwrite the existing account.")
                sys.exit(1)

            # --force: update password and ensure role=admin
            hashed = get_password_hash(args.password)
            session.execute(
                text(
                    "UPDATE users SET hashed_password = :hp, role = 'admin', is_active = true"
                    " WHERE username = :u"
                ),
                {"hp": hashed, "u": args.username},
            )
            session.commit()
            print(f"[+] Admin user '{args.username}' updated (--force).")
            print(f"    username : {args.username}")
            print(f"    user_id  : {existing[0]}")
            print("    role     : admin")
            return

        user_id = str(uuid.uuid4())
        hashed = get_password_hash(args.password)

        session.execute(
            text(
                "INSERT INTO users (user_id, username, hashed_password, role, is_active)"
                " VALUES (:id, :u, :hp, 'admin', true)"
            ),
            {"id": user_id, "u": args.username, "hp": hashed},
        )
        session.commit()

    print("[+] Admin user created successfully.")
    print(f"    username : {args.username}")
    print(f"    user_id  : {user_id}")
    print("    role     : admin")


if __name__ == "__main__":
    main()
