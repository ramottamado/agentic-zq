"""Data-access layer behind the MCP server.

`Warehouse` is the seam between the MCP tools and the actual engine. Today only
`MockWarehouse` exists: an in-memory SQLite database seeded with sample sales data
and attached as schema `sales`, so queries look like Impala's `db.table` form.
`ImpalaWarehouse` implements the same protocol against a real Impala virtual warehouse (impyla,
Kerberos/GSSAPI over HTTP). It uses the Kerberos ticket cache of the process (`kinit` / keytab).

Note: SQLite's SQL dialect is not Impala's. Generated SQL that works here may need
adjusting against a real Impala virtual warehouse (date functions especially).
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_READ_ONLY_PREFIX = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


class QueryRejected(ValueError):
    """The SQL was refused by a guardrail before reaching the engine."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
        }


class Warehouse(Protocol):
    def list_tables(self) -> list[dict[str, str]]: ...
    def describe_table(self, table: str) -> list[dict[str, str]]: ...
    def execute(self, sql: str, max_rows: int) -> QueryResult: ...


def check_read_only(sql: str) -> str:
    """Return the cleaned single statement, or raise QueryRejected."""
    cleaned = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql)).strip().rstrip(";").strip()
    if not cleaned:
        raise QueryRejected("Empty query.")
    if ";" in cleaned:
        raise QueryRejected("Only a single statement is allowed.")
    if not _READ_ONLY_PREFIX.match(cleaned):
        raise QueryRejected("Only read-only SELECT/WITH queries are allowed.")
    return cleaned


SCHEMA_DDL = """
CREATE TABLE sales.customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT,
    region TEXT,
    segment TEXT,
    signup_date TEXT
);
CREATE TABLE sales.products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT,
    category TEXT,
    unit_price REAL
);
CREATE TABLE sales.orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    product_id INTEGER,
    order_date TEXT,
    quantity INTEGER,
    revenue REAL
);
"""

TABLE_COMMENTS = {
    "sales.customers": "One row per customer, with region and business segment.",
    "sales.products": "Product catalogue with category and list price.",
    "sales.orders": "One row per order line; revenue = quantity * unit_price. order_date is YYYY-MM-DD.",
}

_REGIONS = ["APAC", "EMEA", "North America", "LATAM"]
_SEGMENTS = ["Enterprise", "SMB", "Consumer"]
_PRODUCTS = [
    ("Cloud Storage 1TB", "Storage", 120.0),
    ("Cloud Storage 10TB", "Storage", 950.0),
    ("Compute Small", "Compute", 80.0),
    ("Compute Large", "Compute", 640.0),
    ("Analytics Seat", "Analytics", 45.0),
    ("ML Workbench Seat", "Analytics", 210.0),
    ("Support Basic", "Services", 300.0),
    ("Support Premium", "Services", 1500.0),
]


class MockWarehouse:
    """In-memory SQLite stand-in for an Impala virtual warehouse. Deterministic (seeded)."""

    def __init__(self, seed: int = 42, n_customers: int = 60, n_orders: int = 1500) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("ATTACH DATABASE ':memory:' AS sales")
        self._conn.executescript(SCHEMA_DDL)
        self._seed(random.Random(seed), n_customers, n_orders)
        # Engine-level guard in addition to check_read_only().
        self._conn.execute("PRAGMA query_only = ON")

    def _seed(self, rng: random.Random, n_customers: int, n_orders: int) -> None:
        start = date(2025, 1, 1)
        customers = [
            (
                i,
                f"Customer {i:03d}",
                rng.choice(_REGIONS),
                rng.choice(_SEGMENTS),
                (start - timedelta(days=rng.randint(0, 700))).isoformat(),
            )
            for i in range(1, n_customers + 1)
        ]
        products = [(i, name, cat, price) for i, (name, cat, price) in enumerate(_PRODUCTS, start=1)]
        orders = []
        for i in range(1, n_orders + 1):
            pid, _, _, price = rng.choice(products)
            qty = rng.randint(1, 10)
            orders.append(
                (
                    i,
                    rng.randint(1, n_customers),
                    pid,
                    (start + timedelta(days=rng.randint(0, 364))).isoformat(),
                    qty,
                    round(qty * price, 2),
                )
            )
        self._conn.executemany("INSERT INTO sales.customers VALUES (?,?,?,?,?)", customers)
        self._conn.executemany("INSERT INTO sales.products VALUES (?,?,?,?)", products)
        self._conn.executemany("INSERT INTO sales.orders VALUES (?,?,?,?,?,?)", orders)
        self._conn.commit()

    def list_tables(self) -> list[dict[str, str]]:
        with self._lock:
            names = self._conn.execute(
                "SELECT name FROM sales.sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return [{"table": f"sales.{n}", "comment": TABLE_COMMENTS.get(f"sales.{n}", "")} for (n,) in names]

    def describe_table(self, table: str) -> list[dict[str, str]]:
        db, _, name = table.rpartition(".")
        if (db or "sales") != "sales" or not re.fullmatch(r"\w+", name):
            raise QueryRejected(f"Unknown table: {table}")
        with self._lock:
            cols = self._conn.execute(f"PRAGMA sales.table_info({name})").fetchall()
        if not cols:
            raise QueryRejected(f"Unknown table: {table}")
        return [{"column": c[1], "type": c[2]} for c in cols]

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        statement = check_read_only(sql)
        with self._lock:
            try:
                cur = self._conn.execute(statement)
            except sqlite3.Error as exc:
                raise QueryRejected(f"Query failed: {exc}") from exc
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(max_rows + 1)
        rows = [list(r) for r in fetched[:max_rows]]
        return QueryResult(columns=columns, rows=rows, truncated=len(fetched) > max_rows)


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_REF = re.compile(r"(?<![\w.`])`?([A-Za-z_]\w*)`?\s*\.\s*`?[A-Za-z_]\w*`?", re.IGNORECASE)


class ImpalaWarehouse:
    """Real Impala virtual warehouse over impyla (HTTP transport, GSSAPI/Kerberos).

    Auth comes from the Kerberos credential cache (KRB5CCNAME); renew it outside this class.
    Access is scoped to `databases` here in addition to whatever Ranger enforces. A fresh
    connection is opened per call because impyla connections are not thread-safe.
    """

    def __init__(
        self,
        *,
        host: str,
        databases: list[str],
        port: int = 443,
        http_path: str = "cliservice",
        krb_service_name: str = "hive",
        krb_host: str | None = None,
        ssl_verify: bool = False,
        ca_cert: str | None = None,
        connect: Any = None,
    ) -> None:
        if not databases:
            raise ValueError("At least one database must be allowed.")
        self._databases = [d.lower() for d in databases]
        self._default_db = self._databases[0]
        if connect is None:
            from impala.dbapi import connect  # imported lazily: only needed for the real engine
        self._connect = connect
        self._kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "auth_mechanism": "GSSAPI",
            "use_ssl": True,
            "use_http_transport": True,
            "http_path": http_path,
            "kerberos_service_name": krb_service_name,
            "verify_cert": ssl_verify,
        }
        if krb_host:
            self._kwargs["krb_host"] = krb_host
        if ca_cert:
            self._kwargs["ca_cert"] = ca_cert
        if not ssl_verify:
            logger.warning("Impala TLS certificate verification is DISABLED (set IMPALA_CA_CERT to enable).")

    def _run(self, sql: str, max_rows: int | None = None) -> tuple[list[str], list[list[Any]], bool]:
        conn = self._connect(**self._kwargs)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                columns = [d[0] for d in cur.description or []]
                if max_rows is None:
                    fetched = cur.fetchall()
                else:
                    fetched = cur.fetchmany(max_rows + 1)
            finally:
                cur.close()
        except QueryRejected:
            raise
        except Exception as exc:
            raise QueryRejected(f"Query failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        rows = [list(r) for r in fetched]
        if max_rows is None:
            return columns, rows, False
        return columns, rows[:max_rows], len(rows) > max_rows

    def _split(self, table: str) -> tuple[str, str]:
        db, _, name = table.strip().replace("`", "").rpartition(".")
        db = (db or self._default_db).lower()
        if not _IDENT.match(name) or not _IDENT.match(db):
            raise QueryRejected(f"Invalid table name: {table}")
        if db not in self._databases:
            raise QueryRejected(f"Database not allowed: {db}. Allowed: {', '.join(self._databases)}")
        return db, name

    def list_tables(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for db in self._databases:
            _, rows, _ = self._run(f"SHOW TABLES IN `{db}`")
            out += [{"table": f"{db}.{r[0]}", "comment": ""} for r in rows]
        return out

    def describe_table(self, table: str) -> list[dict[str, str]]:
        db, name = self._split(table)
        _, rows, _ = self._run(f"DESCRIBE `{db}`.`{name}`")
        return [{"column": r[0], "type": r[1]} for r in rows]

    def execute(self, sql: str, max_rows: int) -> QueryResult:
        statement = check_read_only(sql)
        self._check_databases(statement)
        columns, rows, truncated = self._run(statement, max_rows)
        return QueryResult(columns=columns, rows=rows, truncated=truncated)

    def _check_databases(self, statement: str) -> None:
        """Reject `otherdb.table` references. Heuristic (Ranger is the real boundary): it catches
        qualified names, while unqualified tables resolve in the connection's default database."""
        stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "''", statement)
        for db in _QUALIFIED_REF.findall(stripped):
            if db.lower() not in self._databases:
                raise QueryRejected(f"Database not allowed: {db}. Allowed: {', '.join(self._databases)}")
