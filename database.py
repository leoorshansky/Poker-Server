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

def join(name, chips, seat):
    with open(os.path.join(os.getcwd(), "gamestatus.json"), "r") as openfile:
        game_status = json.load(openfile)
    if name not in game_status["table"]["players_chips"].keys():
        if game_status["table"]["seats"][seat] == "":
            game_status["table"]["seats"][seat] = name
        else:
            return "seattaken"
        game_status["table"]["players_chips"][name] = chips
        with open(os.path.join(os.getcwd(), "gamestatus.json"), "w") as openfile:
            json.dump(game_status, openfile, indent = 4)
        prev_chips = int(query_db("SELECT * from chips WHERE username = ?", (name,), True)["qty"])
        with get_db() as con:
            con.execute("UPDATE chips SET qty = ? WHERE username = ?", (prev_chips - chips, name))
        return "success"
    return "alreadyjoined"

def leave(name):
    with open(os.path.join(os.getcwd(), "gamestatus.json"), "r") as openfile:
        game_status = json.load(openfile)
    if name in game_status["table"]["players_chips"].keys():
        chips = game_status["table"]["players_chips"][name]
        del game_status["table"]["players_chips"][name]
        game_status["table"]["seats"].remove(name)
        with open(os.path.join(os.getcwd(), "gamestatus.json"), "w") as openfile:
            json.dump(game_status, openfile, indent = 4)
        prev_chips = int(query_db("SELECT * from chips WHERE username = ?", (name,), True)["qty"])
        with get_db() as con:
            con.execute("UPDATE chips SET qty = ? WHERE username = ?", (chips + prev_chips, name))
        return "success"
    return "notingame"

def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()