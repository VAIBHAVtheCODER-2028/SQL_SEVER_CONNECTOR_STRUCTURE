import pyodbc 
from .base import * 

TYPE_MAP = {
    "int": "integer", "bigint": "integer", "smallint": "integer", "tinyint": "integer",
    "decimal": "float", "numeric": "float", "float": "float", "real": "float",
    "money": "float", "smallmoney": "float",
    "bit": "bool",
    "char": "text", "varchar": "text", "nchar": "text", "nvarchar": "text",
    "text": "text", "ntext": "text", "uniqueidentifier": "text", "xml": "text",
    "date": "datetime", "datetime": "datetime", "datetime2": "datetime",
    "smalldatetime": "datetime", "datetimeoffset": "datetime", "time": "datetime",
    "binary": "binary", "varbinary": "binary", "image": "binary", "timestamp": "binary",
    # geography, geometry, hierarchyid, sql_variant -> fall through to "unsupported"
}

def q(name:str)-> str:
    """Quote an identifier the SQL Server way: [name],with] escaped as ]]"""
    return "[" + name.replace("]","]]")+"]" 

def obc_escape(value: str)->str:
    """Passwords containing ; or } break connection strings unless wrapped."""
    return "{" +value.replace("}","}}")+"}" 

class SQLServerConnector(SQLConnector):
    def __init__(self, host, database, user, password, port=1433,
                driver="ODBC Driver 18 for SQL Server",
                trust_server_cert=False, timeout=10):

        self.conn_str = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            f"UID={odbc_escape(user)};PWD={odbc_escape(password)};"
            f"Encrypt=yes;TrustServerCertificate={'yes' if trust_server_cert else 'no'};"
            f"ApplicationIntent=ReadOnly;"
        )
        self.timeout = timeout
        self._conn = None       

    def _connect(self):
        if self._conn is None:
            self._conn = pyodbc.connect(self.conn_str, timeout=self.timeout, readonly=True)
            self._conn.timeout = 30  # per-query timeout
            # datetimeoffset (-155) is not natively supported by pyodbc
            self._conn.add_output_converter(-155, lambda b: b.decode("utf-16le") if b else None)
        return self._conn        

    def test_connection(self):
        cur = self._connect().cursor()
        cur.execute("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")
        version, db, login = cur.fetchone()
        return {"ok": True, "version": version.split("\n")[0], "database": db, "login": login}

    def list_tables(self):
        cur = self._connect().cursor()

        # 1. tables + views (INFORMATION_SCHEMA is portable across DBs)
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
        """)
        tables = {(s, n): TableInfo(s, n, "view" if t == "VIEW" else "table")
                  for s, n, t in cur.fetchall()}

        # 2. columns
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
            FROM INFORMATION_SCHEMA.COLUMNS
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """)
        for s, t, c, dtype, nullable, maxlen, prec, scale in cur.fetchall():
            if (s, t) in tables:
                tables[(s, t)].columns.append(ColumnInfo(
                    c, dtype, TYPE_MAP.get(dtype.lower(), "unsupported"),
                    nullable == "YES", maxlen, prec, scale))

        # 3. primary keys
        cur.execute("""
            SELECT kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        """)
        for s, t, c in cur.fetchall():
            for col in tables.get((s, t), TableInfo("", "", "")).columns:
                if col.name == c:
                    col.is_pk = True

        # 4. foreign keys: use sys.* catalogs, NOT INFORMATION_SCHEMA (composite FKs get mangled)
        cur.execute("""
            SELECT fk.name, ps.name, pt.name, pc.name, rs.name, rt.name, rc.name,
                   fkc.constraint_column_id
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
            JOIN sys.tables pt  ON pt.object_id = fkc.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
            JOIN sys.columns pc ON pc.object_id = pt.object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.tables rt  ON rt.object_id = fkc.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            JOIN sys.columns rc ON rc.object_id = rt.object_id AND rc.column_id = fkc.referenced_column_id
            ORDER BY fk.name, fkc.constraint_column_id
        """)
        fks = {}
        for name, ps_, pt_, pc_, rs_, rt_, rc_, _ in cur.fetchall():
            key = (ps_, pt_, name)
            fk = fks.setdefault(key, ForeignKey(name, [], rs_, rt_, []))
            fk.columns.append(pc_)
            fk.ref_columns.append(rc_)
        for (s, t, _), fk in fks.items():
            if (s, t) in tables:
                tables[(s, t)].foreign_keys.append(fk)

        # 5. approximate row counts (metadata, instant: never COUNT(*) on big tables)
        cur.execute("""
            SELECT s.name, t.name, SUM(p.row_count)
            FROM sys.dm_db_partition_stats p
            JOIN sys.tables t  ON t.object_id = p.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE p.index_id IN (0, 1)
            GROUP BY s.name, t.name
        """)
        for s, t, n in cur.fetchall():
            if (s, t) in tables:
                tables[(s, t)].approx_rows = n

        return list(tables.values())

    def sample(self, schema, table, n=1000):
        n = max(1, min(int(n), 100_000))                   # clamp: never trust the caller
        sql = f"SELECT TOP ({n}) * FROM {q(schema)}.{q(table)}"   # SQL Server: TOP, not LIMIT
        cur = self._connect().cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None