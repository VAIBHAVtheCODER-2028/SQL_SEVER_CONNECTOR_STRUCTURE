from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# NeuroNova's own vocabulary. Every connector must map into exactly these.
NORMALIZED_TYPES = (
    "text", "integer", "decimal", "float", "bool",
    "date", "time", "datetime", "datetime_tz",
    "binary", "unsupported",
)

@dataclass
class ColumnInfo:
    name: str
    raw_type: str                 # what the DB says: "datetime2"
    normalized_type: str          # one of NORMALIZED_TYPES
    nullable: bool
    max_length: int | None = None # SQL Server reports -1 for (max) types
    precision: int | None = None
    scale: int | None = None
    is_pk: bool = False

@dataclass
class ForeignKey:
    name: str
    columns: list[str]            # lists: composite keys have 2+ columns
    ref_schema: str
    ref_table: str
    ref_columns: list[str]

@dataclass
class TableInfo:
    schema: str
    name: str
    kind: str                     # "table" | "view"
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    approx_rows: int | None = None   # None = unknown (views, or count unavailable)

class SQLConnector(ABC):
    @abstractmethod
    def test_connection(self) -> dict: ...
    @abstractmethod
    def list_tables(self) -> list[TableInfo]: ...
    @abstractmethod
    def sample(self, schema: str, table: str, n: int = 1000) -> list[dict]: ...
    @abstractmethod
    def close(self) -> None: ...

    # context manager: `with SQLServerConnector(...) as c:` always closes
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False