# test_skeleton.py
from connectors.sqlserver import SQLServerConnector

HOST, DB = "localhost", "nn_test"     # Express/named instance: HOST = r"localhost\SQLEXPRESS" and add port=None below

def run(label, fn):
    print(f"\n=== {label}")
    try:
        result = fn()
        if result is not None:
            print(result)
    except Exception as e:
        print(f"FAILED -> {type(e).__name__}: {e}")
        if getattr(e, "original", None) is not None:
            print(f"   original: {e.original!r}")

def make(user, pw, **kw):
    return SQLServerConnector(HOST, DB, user, pw, **kw)

def summarize(c):
    for t in sorted(c.list_tables(), key=lambda t: (t.schema, t.name)):
        print(f"{t.kind:5} {t.schema}.{t.name}  rows~{t.approx_rows}  cols={len(t.columns)}  fks={len(t.foreign_keys)}")
        for col in t.columns:
            print(f"        {col.name:<14} {col.raw_type:<16} -> {col.normalized_type}{'  [PK]' if col.is_pk else ''}")
        for fk in t.foreign_keys:
            print(f"        FK {fk.name}: {fk.columns} -> {fk.ref_schema}.{fk.ref_table}{fk.ref_columns}")

# ---------- connection layer ----------
run("A. strict TLS vs self-signed cert",
    lambda: make("nn_reader", "Reader#2026x").test_connection())
run("B. trust server cert",
    lambda: make("nn_reader", "Reader#2026x", trust_server_cert=True).test_connection())
run("C. wrong password",
    lambda: make("nn_reader", "nope", trust_server_cert=True).test_connection())
run("D. closed port",
    lambda: make("nn_reader", "Reader#2026x", port=1444, trust_server_cert=True).test_connection())
run("E. password containing ; and }",
    lambda: make("nn_tricky", "Tr;cky}Pass#2026", trust_server_cert=True).test_connection())
run("F. silent firewall drop (timeout)",
    lambda: SQLServerConnector("10.255.255.1", DB, "x", "y", trust_server_cert=True, timeout=5).test_connection())

# ---------- metadata + data ----------
c = make("nn_reader", "Reader#2026x", trust_server_cert=True)
run("G. list_tables as nn_reader",   lambda: summarize(c))
run("H. sample Order Details (dbo)", lambda: c.sample("dbo", "Order Details", 5))
run("I. sample misleading",          lambda: c.sample("dbo", "misleading", 5))
run("J. sample missing table",       lambda: c.sample("dbo", "nope", 5))
c.close()

# ---------- permissions ----------
with make("nn_limited", "Limited#2026x", trust_server_cert=True) as lim:
    run("K. list_tables as nn_limited", lambda: summarize(lim))