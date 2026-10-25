"""Cause one real deadlock (ORA-00060) on the lab database, so the next brief has something to report."""
import threading, time
import oracledb

DSN, USER, PW = "127.0.0.1:15299/FREEPDB1", "lab", "lab_pw_2026"
a, b = (oracledb.connect(user=USER, password=PW, dsn=DSN) for _ in range(2))
with a.cursor() as c:
    try: c.execute("drop table ns_dl purge")
    except oracledb.DatabaseError: pass
    c.execute("create table ns_dl (id number primary key, v number)")
    c.execute("insert into ns_dl values (1, 0)"); c.execute("insert into ns_dl values (2, 0)"); a.commit()
a.cursor().execute("update ns_dl set v = 1 where id = 1")
b.cursor().execute("update ns_dl set v = 1 where id = 2")
def second():
    try: b.cursor().execute("update ns_dl set v = 2 where id = 1")
    except oracledb.DatabaseError as e: print("b:", e)
t = threading.Thread(target=second); t.start(); time.sleep(1)
try: a.cursor().execute("update ns_dl set v = 2 where id = 2")
except oracledb.DatabaseError as e: print("a:", e)
a.rollback(); t.join(10); b.rollback()
