from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_members (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    joined_at TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS trip_invites (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    invite_code TEXT NOT NULL UNIQUE,
    invited_name_key TEXT NOT NULL,
    claimed_by_user_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (trip_id, invited_name_key)
);

CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    paid_by_user_id TEXT NOT NULL REFERENCES users(id),
    category TEXT NOT NULL,
    expense_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trip_members_user_id
ON trip_members(user_id);

CREATE INDEX IF NOT EXISTS idx_expenses_trip_id
ON expenses(trip_id, created_at);

CREATE INDEX IF NOT EXISTS idx_trip_invites_trip_id
ON trip_invites(trip_id, created_at);

CREATE TRIGGER IF NOT EXISTS validate_trip_before_insert
BEFORE INSERT ON trips
WHEN
    typeof(json_extract(NEW.data, '$.name')) != 'text'
    OR length(trim(json_extract(NEW.data, '$.name'))) NOT BETWEEN 1 AND 100
    OR ((json_extract(NEW.data, '$.start_date') IS NULL) != (json_extract(NEW.data, '$.end_date') IS NULL))
    OR json_extract(NEW.data, '$.start_date') > json_extract(NEW.data, '$.end_date')
BEGIN
    SELECT RAISE(ABORT, 'invalid trip data');
END;

CREATE TRIGGER IF NOT EXISTS validate_trip_before_update
BEFORE UPDATE OF data ON trips
WHEN
    typeof(json_extract(NEW.data, '$.name')) != 'text'
    OR length(trim(json_extract(NEW.data, '$.name'))) NOT BETWEEN 1 AND 100
    OR ((json_extract(NEW.data, '$.start_date') IS NULL) != (json_extract(NEW.data, '$.end_date') IS NULL))
    OR json_extract(NEW.data, '$.start_date') > json_extract(NEW.data, '$.end_date')
BEGIN
    SELECT RAISE(ABORT, 'invalid trip data');
END;
"""


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._replace_legacy_invite_table(connection)
            connection.executescript(SCHEMA)
            connection.execute(
                """
                DELETE FROM trips
                WHERE json_extract(data, '$.start_date') IS NOT NULL
                  AND json_extract(data, '$.end_date') IS NOT NULL
                  AND json_extract(data, '$.start_date') > json_extract(data, '$.end_date')
                """
            )

    @staticmethod
    def _replace_legacy_invite_table(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(trip_invites)").fetchall()
        }
        required_columns = {"invited_name_key", "claimed_by_user_id", "created_at"}
        if columns and not required_columns.issubset(columns):
            # Legacy links have no intended recipient, so they cannot be retained
            # without silently assigning the wrong in-trip name.
            connection.execute("DROP TABLE trip_invites")

    def _fetch_data(self, query: str, parameters: tuple[str, ...]) -> str | None:
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return str(row["data"]) if row else None

    def _fetch_data_list(self, query: str, parameters: tuple[str, ...]) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [str(row["data"]) for row in rows]

    def insert_user(self, user: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO users (id, email, data) VALUES (?, ?, ?)",
                (str(user.id), str(user.email).lower(), user.model_dump_json()),
            )

    def get_user_by_id(self, user_id: str) -> str | None:
        return self._fetch_data("SELECT data FROM users WHERE id = ?", (user_id,))

    def get_user_by_email(self, email: str) -> str | None:
        return self._fetch_data("SELECT data FROM users WHERE email = ?", (email.lower(),))

    def insert_trip_with_owner(self, trip: BaseModel, owner: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO trips (id, owner_user_id, created_at, data) VALUES (?, ?, ?, ?)",
                (str(trip.id), str(trip.owner_user_id), trip.created_at.isoformat(), trip.model_dump_json()),
            )
            connection.execute(
                """
                INSERT INTO trip_members (id, trip_id, user_id, joined_at, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(owner.id),
                    str(owner.trip_id),
                    str(owner.user_id),
                    owner.joined_at.isoformat(),
                    owner.model_dump_json(),
                ),
            )

    def get_trip(self, trip_id: str) -> str | None:
        return self._fetch_data("SELECT data FROM trips WHERE id = ?", (trip_id,))

    def update_trip_with_expenses(self, trip: BaseModel, expenses: list[BaseModel]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE trips SET data = ? WHERE id = ?",
                (trip.model_dump_json(), str(trip.id)),
            )
            for expense in expenses:
                connection.execute(
                    """
                    UPDATE expenses
                    SET paid_by_user_id = ?, category = ?, expense_time = ?, data = ?
                    WHERE trip_id = ? AND id = ?
                    """,
                    (
                        str(expense.paid_by_user_id),
                        str(expense.category),
                        expense.expense_time.isoformat(),
                        expense.model_dump_json(),
                        str(expense.trip_id),
                        str(expense.id),
                    ),
                )

    def list_trips_for_user(self, user_id: str) -> list[str]:
        return self._fetch_data_list(
            """
            SELECT trips.data
            FROM trips
            JOIN trip_members ON trip_members.trip_id = trips.id
            WHERE trip_members.user_id = ?
            ORDER BY trips.created_at
            """,
            (user_id,),
        )

    def insert_member(self, member: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_members (id, trip_id, user_id, joined_at, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(member.id),
                    str(member.trip_id),
                    str(member.user_id),
                    member.joined_at.isoformat(),
                    member.model_dump_json(),
                ),
            )

    def get_member(self, trip_id: str, user_id: str) -> str | None:
        return self._fetch_data(
            "SELECT data FROM trip_members WHERE trip_id = ? AND user_id = ?",
            (trip_id, user_id),
        )

    def list_members(self, trip_id: str) -> list[str]:
        return self._fetch_data_list(
            "SELECT data FROM trip_members WHERE trip_id = ? ORDER BY joined_at",
            (trip_id,),
        )

    def delete_member(self, trip_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM trip_members WHERE trip_id = ? AND user_id = ?",
                (trip_id, user_id),
            )

    def insert_invite(self, invite: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_invites (
                    id, trip_id, invite_code, invited_name_key,
                    claimed_by_user_id, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(invite.id),
                    str(invite.trip_id),
                    str(invite.invite_code),
                    str(invite.invited_name_key),
                    str(invite.claimed_by_user_id) if invite.claimed_by_user_id else None,
                    invite.created_at.isoformat(),
                    invite.model_dump_json(),
                ),
            )

    def get_invite_for_name(self, trip_id: str, invited_name_key: str) -> str | None:
        return self._fetch_data(
            "SELECT data FROM trip_invites WHERE trip_id = ? AND invited_name_key = ?",
            (trip_id, invited_name_key),
        )

    def get_invite_by_code(self, invite_code: str) -> str | None:
        return self._fetch_data(
            "SELECT data FROM trip_invites WHERE invite_code = ?",
            (invite_code,),
        )

    def list_invites(self, trip_id: str) -> list[str]:
        return self._fetch_data_list(
            "SELECT data FROM trip_invites WHERE trip_id = ? ORDER BY created_at",
            (trip_id,),
        )

    def claim_invite(self, invite: BaseModel, member: BaseModel) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trip_invites
                SET claimed_by_user_id = ?, data = ?
                WHERE id = ? AND claimed_by_user_id IS NULL
                """,
                (
                    str(invite.claimed_by_user_id),
                    invite.model_dump_json(),
                    str(invite.id),
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO trip_members (id, trip_id, user_id, joined_at, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(member.id),
                    str(member.trip_id),
                    str(member.user_id),
                    member.joined_at.isoformat(),
                    member.model_dump_json(),
                ),
            )
        return True

    def insert_expense(self, expense: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO expenses (
                    id, trip_id, created_by_user_id, paid_by_user_id,
                    category, expense_time, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(expense.id),
                    str(expense.trip_id),
                    str(expense.created_by_user_id),
                    str(expense.paid_by_user_id),
                    str(expense.category),
                    expense.expense_time.isoformat(),
                    expense.created_at.isoformat(),
                    expense.model_dump_json(),
                ),
            )

    def get_expense(self, trip_id: str, expense_id: str) -> str | None:
        return self._fetch_data(
            "SELECT data FROM expenses WHERE trip_id = ? AND id = ?",
            (trip_id, expense_id),
        )

    def list_expenses(self, trip_id: str) -> list[str]:
        return self._fetch_data_list(
            "SELECT data FROM expenses WHERE trip_id = ? ORDER BY created_at",
            (trip_id,),
        )

    def update_expense(self, expense: BaseModel) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE expenses
                SET paid_by_user_id = ?, category = ?, expense_time = ?, data = ?
                WHERE trip_id = ? AND id = ?
                """,
                (
                    str(expense.paid_by_user_id),
                    str(expense.category),
                    expense.expense_time.isoformat(),
                    expense.model_dump_json(),
                    str(expense.trip_id),
                    str(expense.id),
                ),
            )

    def delete_expense(self, trip_id: str, expense_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM expenses WHERE trip_id = ? AND id = ?",
                (trip_id, expense_id),
            )
