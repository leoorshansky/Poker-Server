from flask import Flask, g, request, make_response
import sqlite3, datetime, json

app = Flask(__name__)

DATABASE = '/var/www/html/bruh/poker/poker.db'

def response(o, status = 200, ctype = "text/plain"):
    resp = make_response(str(o), status)
    resp.headers["Content-Type"] = ctype
    return resp

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

@app.route("/chips/<name>/get/")
def get_chips(name):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return response("notfound", 404)
    return response(row["qty"])

@app.route("/chips/<name>/last_replenished/")
def get_last_replenished(name):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return response("notfound", 404)
    return response(row["last_replenished"])

@app.route("/chips/<name>/replenish/")
def replenish(name):
    row = query_db("SELECT * from chips WHERE username = ?", (name,), True)
    if not row:
        return response("notfound", 404)
    qty = request.args.get("qty")
    date = datetime.datetime.utcnow().strftime("%d %B %Y %X")
    with get_db() as con:
        con.execute("UPDATE chips SET qty = ?, last_replenished = ? WHERE username = ?", (qty, date, name))
    return response("success")

@app.route("/join/<name>/")
def join(name):
    try:
        chips = int(request.args.get("chips"))
        seat = int(request.args.get("seat"))
    except Exception:
        return response("invalidrequest")
    with open("/var/www/html/bruh/poker/gamestatus.json", "r") as openfile:
        game_status = json.load(openfile)
    if name not in game_status["table"]["players_chips"].keys():
        if game_status["table"]["seats"][seat] == "":
            game_status["table"]["seats"][seat] = name
        else:
            return response("seattaken")
        game_status["table"]["players_chips"][name] = chips
        with open("/var/www/html/bruh/poker/gamestatus.json", "w") as openfile:
            json.dump(game_status, openfile, indent = 4)
        prev_chips = int(query_db("SELECT * from chips WHERE username = ?", (name,), True)["qty"])
        with get_db() as con:
            con.execute("UPDATE chips SET qty = ? WHERE username = ?", (prev_chips - chips, name))
        return response("success")
    return response("alreadyjoined")

@app.route("/leave/<name>/")
def leave(name):
    with open("/var/www/html/bruh/poker/gamestatus.json", "r") as openfile:
        game_status = json.load(openfile)
    if name in game_status["table"]["players_chips"].keys():
        chips = game_status["table"]["players_chips"][name]
        del game_status["table"]["players_chips"][name]
        game_status["table"]["seats"].remove(name)
        with open("/var/www/html/bruh/poker/gamestatus.json", "w") as openfile:
            json.dump(game_status, openfile, indent = 4)
        prev_chips = int(query_db("SELECT * from chips WHERE username = ?", (name,), True)["qty"])
        with get_db() as con:
            con.execute("UPDATE chips SET qty = ? WHERE username = ?", (chips + prev_chips, name))
        return response("success")
    return response("notingame")

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

application = app