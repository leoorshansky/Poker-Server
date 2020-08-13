import sqlite3, datetime, json, os
from flask import g

DATABASE = os.path.join(os.getcwd(), "poker.db")

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def modify_db(cmd):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(cmd)
    conn.commit()
    conn.close()

def get_chips(name):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return "notfound"
    return row["qty"]

def get_last_replenished(name):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return "notfound"
    return row["last_replenished"]

def replenish(name, qty):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return "notfound"
    date = datetime.datetime.utcnow().strftime("%d %B %Y %X")
    with get_db() as con:
        con.execute("UPDATE chips SET qty = ?, last_replenished = ? WHERE username = ?", (qty, date, name))
    return "success"

def join(name, chips):
    prev_chips = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if prev_chips is None:
        modify_db(f"INSERT INTO chips VALUES ('{name}', 10000, '{datetime.datetime.utcnow().strftime('''%d %B %Y %X''')}');")
        prev_chips = 10000
    else:
        prev_chips = int(prev_chips["qty"])
    if prev_chips < chips:
        print(f"NOT ENOUGH CHIPS for user {name}")
        return "broke"
    with get_db() as con:
        con.execute("UPDATE chips SET qty = ? WHERE username = ?", (prev_chips - chips, name))
    return "success"

def leave(name, chips):
    prev_chips = int(query_db("SELECT * from chips WHERE username = ?", (name,), True)["qty"])
    with get_db() as con:
        con.execute("UPDATE chips SET qty = ? WHERE username = ?", (chips + prev_chips, name))
    return "success"

def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()