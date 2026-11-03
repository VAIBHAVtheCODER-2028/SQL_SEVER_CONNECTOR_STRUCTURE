import struct
from datetime import datetime, timezone, timedelta

import pyodbc

from .base import ColumnInfo, ForeignKey, SQLConnector, TableInfo
from .errors import (
    ConnectorAuthError, ConnectorConfigError, ConnectorError,
    ConnectorNetworkError, ConnectorPermissionError, ConnectorTimeoutError,
)

TYPE_MAP = {
    "tinyint": "integer", "smallint": "integer", "int": "integer", "bigint": "integer",
    "decimal": "decimal", "numeric": "decimal", "money": "decimal", "smallmoney": "decimal",
    "float": "float", "real": "float",
    "bit": "bool",
    "char": "text", "varchar": "text", "nchar": "text", "nvarchar": "text",
    "text": "text", "ntext": "text", "uniqueidentifier": "text", "xml": "text",
    "date": "date",
    "time": "time",
    "smalldatetime": "datetime", "datetime": "datetime", "datetime2": "datetime",
    "datetimeoffset": "datetime_tz",
    "binary": "binary", "varbinary": "binary", "image": "binary",
    "timestamp": "binary",       # NOT a date: it's a binary row-version counter
    # geography, geometry, hierarchyid, sql_variant -> "unsupported" (via .get default)
}

# Types pyodbc can't fetch directly: convert to text inside the SELECT.
CAST_EXPR = {
    "geography":   "{c}.ToString()",
    "geometry":    "{c}.ToString()",
    "hierarchyid": "{c}.ToString()",
    "sql_variant": "CAST({c} AS NVARCHAR(4000))",
}

def q(name: str) -> str:
    """Quote an identifier SQL Server style: [name], with ] escaped as ]]."""
    return "[" + name.replace("]", "]]") + "]"

def odbc_escape(value: str) -> str:
    """Wrap a connection-string value in {} so ; and = inside it are safe."""
    return "{" + value.replace("}", "}}") + "}"

def _dto_converter(raw: bytes | None):
    """datetimeoffset (ODBC type -155) arrives as a binary struct:
    year, month, day, hour, minute, second (6 x int16), fraction in ns (uint32),
    tz_hour, tz_minute (2 x int16)."""
    if raw is None:
        return None
    y, mo, d, h, mi, s, frac_ns, tzh, tzm = struct.unpack("<6hI2h", raw)
    return datetime(y, mo, d, h, mi, s, frac_ns // 1000,
                    timezone(timedelta(hours=tzh, minutes=tzm)))

def _translate(e: pyodbc.Error) -> ConnectorError:
    """Map a raw pyodbc error to a typed connector error using SQLSTATE + message."""
    state = e.args[0] if e.args and isinstance(e.args[0], str) else ""
    msg = " ".join(str(a) for a in e.args)
    low = msg.lower()
    if state == "IM002" or "can't open lib" in low:
        return ConnectorConfigError("ODBC driver not found. Install 'ODBC Driver 18 for SQL Server'.", e)
    if "certificate" in low or "ssl provider" in low:
        return ConnectorConfigError("Server TLS certificate is not trusted. Install a valid certificate "
                                    "or enable trust_server_cert for dev/test only.", e)
    if state == "28000" or "login failed" in low:
        return ConnectorAuthError("Login failed: wrong username/password, expired password, or disabled login.", e)
    if state in ("HYT00", "HYT01") or "timeout expired" in low:
        return ConnectorTimeoutError("Timed out (login or query).", e)
    if "permission was denied" in low or "permission denied" in low:
        return ConnectorPermissionError("Logged in, but this login lacks permission for that operation.", e)
    if state in ("08001", "08S01", "08004", "08003"):
        return ConnectorNetworkError("Cannot reach the server (host/port/firewall) or the connection dropped.", e)
    return ConnectorError(msg, e)


class SQLServerConnector(SQLConnector):
    def __init__(self, host, database, user, password,
                 port: int | None = 1433,          # None for named instances: host=r"HOST\SQLEXPRESS"
                 driver="ODBC Driver 18 for SQL Server",
                 trust_server_cert=False,
                 timeout=10,                        # login timeout (seconds)
                 query_timeout=30):                 # per-query timeout (seconds); avoid 0
        server = f"{host},{port}" if port else host
        self._conn_str = (
            f"DRIVER={{{driver}}};SERVER={server};DATABASE={odbc_escape(database)};"
            f"UID={odbc_escape(user)};PWD={odbc_escape(password)};"
            f"Encrypt=yes;TrustServerCertificate={'yes' if trust_server_cert else 'no'};"
            f"ApplicationIntent=ReadOnly;"          # only routes to replicas in availability groups
        )
        self._login_timeout = timeout
        self._query_timeout = query_timeout
        self._conn = None
        # NOTE: read-only safety comes from giving this login SELECT-only permissions.

    # ---------- plumbing ----------
    def _connect(self):
        if self._conn is None:
            try:
                conn = pyodbc.connect(self._conn_str, timeout=self._login_timeout, autocommit=True)
            except pyodbc.Error as e:
                raise _translate(e) from e          # never include the conn string in errors
            conn.timeout = self._query_timeout
            conn.add_output_converter(-155, _dto_converter)
            self._conn = conn
        return self._conn

    def _execute(self, sql, *params):
        """Run a query; if the connection was dropped, reconnect and retry ONCE (reads only)."""
        for attempt in (1, 2):
            try:
                cur = self._connect().cursor()
                cur.execute(sql, *params)
                return cur
            except pyodbc.Error as e:
                err = _translate(e)
                if attempt == 1 and isinstance(err, ConnectorNetworkError):
                    self.close()
                    continue
                raise err from e

    def _fetchall(self, sql, *params):
        cur = self._execute(sql, *params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    # ---------- contract ----------
    def test_connection(self):
        row = self._fetchall("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")[0]
        return {"ok": True, "version": row[0].split("\n")[0], "database": row[1], "login": row[2]}

    def list_tables(self):
        # 1. tables + views (tables only appear if this login can see them)
        tables = {}
        for s, n, t in self._fetchall("""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')"""):
            tables[(s, n)] = TableInfo(s, n, "view" if t == "VIEW" else "table")

        # 2. columns
        for s, t, c, dtype, nullable, maxlen, prec, scale in self._fetchall("""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
            FROM INFORMATION_SCHEMA.COLUMNS
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"""):
            tbl = tables.get((s, t))
            if tbl:
                tbl.columns.append(ColumnInfo(
                    c, dtype, TYPE_MAP.get(dtype.lower(), "unsupported"),
                    nullable == "YES", maxlen, prec, scale))

        # 3. primary keys
        for s, t, c in self._fetchall("""
            SELECT kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA   = kcu.TABLE_SCHEMA
             AND tc.TABLE_NAME     = kcu.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'"""):
            tbl = tables.get((s, t))
            if tbl:
                for col in tbl.columns:
                    if col.name == c:
                        col.is_pk = True

        # 4. foreign keys: sys.* catalogs (INFORMATION_SCHEMA mangles composite FKs)
        fks = {}
        for fk_name, ps, pt, pc, rs, rt, rc in self._fetchall("""
            SELECT fk.name, ps.name, pt.name, pc.name, rs.name, rt.name, rc.name
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
            JOIN sys.tables  pt ON pt.object_id = fkc.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
            JOIN sys.columns pc ON pc.object_id = pt.object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.tables  rt ON rt.object_id = fkc.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            JOIN sys.columns rc ON rc.object_id = rt.object_id AND rc.column_id = fkc.referenced_column_id
            ORDER BY fk.name, fkc.constraint_column_id"""):
            fk = fks.setdefault((ps, pt, fk_name), ForeignKey(fk_name, [], rs, rt, []))
            fk.columns.append(pc)
            fk.ref_columns.append(rc)
        for (s, t, _), fk in fks.items():
            tbl = tables.get((s, t))
            if tbl:
                tbl.foreign_keys.append(fk)

        # 5. approximate row counts. OPTIONAL: must never block the required metadata above.
        #    sys.partitions needs only normal metadata visibility (the DMV needs VIEW DATABASE STATE).
        try:
            for s, t, n in self._fetchall("""
                SELECT s.name, t.name, SUM(p.rows)
                FROM sys.partitions p
                JOIN sys.tables  t ON t.object_id = p.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE p.index_id IN (0, 1)
                GROUP BY s.name, t.name"""):
                tbl = tables.get((s, t))
                if tbl:
                    tbl.approx_rows = int(n)
        except ConnectorError:
            pass   # leave approx_rows as None

        return list(tables.values())

    def sample(self, schema, table, n=1000):
        n = max(1, min(int(n), 100_000))                # clamp: never trust the caller

        # Look the table up by (schema, table) with parameters first. This validates the names
        # against what the DB actually exposes, and gives us column types for safe casting.
        cols = self._fetchall("""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION""", schema, table)
        if not cols:
            raise ConnectorError(f"{schema}.{table} not found or not visible to this login")

        select_list = []
        for name, dtype, maxlen in cols:
            c = q(name)
            expr = CAST_EXPR.get(dtype.lower(), "{c}").format(c=c)
            if dtype.lower() in ("varchar", "nvarchar") and maxlen == -1:   # (max) text: truncate
                expr = f"LEFT({c}, 4000)"
            select_list.append(f"{expr} AS {c}")
        # TODO (known gap): varbinary(max)/image blobs are still returned whole.

        sql = f"SELECT TOP ({n}) {', '.join(select_list)} FROM {q(schema)}.{q(table)}"
        cur = self._execute(sql)
        try:
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except pyodbc.Error:
                pass
            self._conn = None