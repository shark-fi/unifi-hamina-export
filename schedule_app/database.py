"""SQLite persistence for the employee scheduler.

A thin data-access layer over a single SQLite file. Two tables:

    employees   one row per person (name + contact details)
    shifts      one row per scheduled shift, tied to an employee

Everything a route needs is exposed as a small function here so the Flask
layer never touches SQL directly.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from typing import Any, Iterable

# The database lives next to this file unless SCHEDULE_DB overrides it.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.db")


def _db_path() -> str:
    return os.environ.get("SCHEDULE_DB", DEFAULT_DB_PATH)


def get_connection() -> sqlite3.Connection:
    """Open a connection with row access by column name and FKs enabled."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    email   TEXT,
    phone   TEXT,
    role    TEXT,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date   TEXT NOT NULL,          -- ISO date, e.g. 2026-08-10
    start_time  TEXT NOT NULL,          -- HH:MM
    end_time    TEXT NOT NULL,          -- HH:MM
    location    TEXT,
    notes       TEXT,
    created     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shifts_employee ON shifts(employee_id);
CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(work_date);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #

def add_employee(name: str, email: str = "", phone: str = "", role: str = "") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO employees (name, email, phone, role) VALUES (?, ?, ?, ?)",
            (name.strip(), email.strip(), phone.strip(), role.strip()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_employee(emp_id: int, name: str, email: str, phone: str, role: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE employees SET name=?, email=?, phone=?, role=? WHERE id=?",
            (name.strip(), email.strip(), phone.strip(), role.strip(), emp_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_employee(emp_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM employees WHERE id=?", (emp_id,))
        conn.commit()
    finally:
        conn.close()


def get_employee(emp_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    finally:
        conn.close()


def list_employees() -> list[sqlite3.Row]:
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM employees ORDER BY name").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Shifts
# --------------------------------------------------------------------------- #

def add_shift(
    employee_id: int,
    work_date: str,
    start_time: str,
    end_time: str,
    location: str = "",
    notes: str = "",
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO shifts
               (employee_id, work_date, start_time, end_time, location, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (employee_id, work_date, start_time, end_time, location.strip(), notes.strip()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_shift(shift_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
        conn.commit()
    finally:
        conn.close()


def list_shifts_for_employee(
    employee_id: int, from_date: str | None = None
) -> list[sqlite3.Row]:
    """Shifts for one employee, soonest first. Optionally only on/after a date."""
    conn = get_connection()
    try:
        if from_date:
            rows = conn.execute(
                """SELECT * FROM shifts
                   WHERE employee_id=? AND work_date >= ?
                   ORDER BY work_date, start_time""",
                (employee_id, from_date),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM shifts WHERE employee_id=?
                   ORDER BY work_date, start_time""",
                (employee_id,),
            ).fetchall()
        return rows
    finally:
        conn.close()


def list_all_shifts(from_date: str | None = None) -> list[sqlite3.Row]:
    """All shifts joined with employee name/contact, soonest first."""
    conn = get_connection()
    try:
        base = """
            SELECT shifts.*, employees.name AS employee_name,
                   employees.email AS employee_email,
                   employees.phone AS employee_phone
            FROM shifts
            JOIN employees ON employees.id = shifts.employee_id
        """
        if from_date:
            base += " WHERE shifts.work_date >= ?"
            base += " ORDER BY shifts.work_date, shifts.start_time"
            return conn.execute(base, (from_date,)).fetchall()
        base += " ORDER BY shifts.work_date, shifts.start_time"
        return conn.execute(base).fetchall()
    finally:
        conn.close()


def today_iso() -> str:
    return date.today().isoformat()
