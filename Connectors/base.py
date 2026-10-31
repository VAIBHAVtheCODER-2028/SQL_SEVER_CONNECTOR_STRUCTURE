from abc import ABC, abstractmethod
from dataclasses import dataclass, field 

@dataclass
class ColumnInfo:
    name:str
    raw_type:str         # what the DB says: "nvarchar", "datetime2"
    normalized_type:str  # NeuroNova type: text/integer/float/bool/datetime/binary/unsupported
    nullable:bool 
    max_length:int | None=None 
    precision:int | None=None 
    scale: int | None=None
    is_pk:bool=False

@dataclass
class ForeignKey:
    name:str 
    columns:list[str] 
    ref_schema:str 
    ref_table:str 
    ref_columns: list[str] 

@dataclass
class TableInfo:
    schema: str
    name: str
    kind: str                # "table" | "view"
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    approx_rows:int | None = None 

class SQLConnector(ABC):
    @abstractmethod
    def test_connection(self) -> dict: ...
    @abstractmethod
    def list_tables(self) -> list[TableInfo]: ...
    @abstractmethod
    def sample(self, schema:str, table:str, n: int=1000) -> list[dict]: ...
    @abstractmethod
    def close(self) -> None: ...