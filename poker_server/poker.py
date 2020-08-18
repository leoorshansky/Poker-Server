import random
import asyncio
import os
import ssl
import json
import jwt
import requests
import itertools
import datetime
from collections import namedtuple
from http.cookies import SimpleCookie
import logging
import socketio
from secrets import token_urlsafe, token_hex
from sanic import Sanic
from sanic.response import html, redirect, text, file
from sanic_jwt import Initialize, Claim
from sanic_session import Session
from jinja2 import Environment, FileSystemLoader
import google.oauth2.credentials
import google_auth_oauthlib.flow
from apache import ReverseProxied
import database

OPEN_LOGINS = {}
JWT_SECRET = os.getenv("JWT_SECRET", token_hex(16))


app = Sanic(__name__)

def authenticate(request):
	nonce = request.args.get('nonce')
	if not nonce or nonce not in OPEN_LOGINS:
		return False
	username = OPEN_LOGINS[nonce]
	del OPEN_LOGINS[nonce]
	return {'user_id':username}

sanicjwt = Initialize(app, cookie_set=True, cookie_secure=True, expiration_delta = 3600 * 24, url_prefix='/poker/auth',
	login_redirect_url="/poker/?login=fail", authenticate=authenticate, secret=JWT_SECRET)
Session(app)
sio = socketio.AsyncServer(async_mode='sanic')
sio.attach(app)
env = Environment(loader=FileSystemLoader(os.getenv("TEMPLATES_PATH")))

def all_equal(lst):
	return len(set(lst)) == 1

def is_consecutive(lst):
	return len(set(lst)) == len(lst) and max(lst) - min(lst) == len(lst) - 1

class Card(namedtuple('Card', 'numeric_rank rank suit')):
	def __str__(self):
		return self.rank + self.suit

def parse_card(card):
	FACE_VALUES = {'A': 14, 'J': 11, 'Q': 12, 'K': 13, "T":10}
	rank, suit = card[:-1], card[-1:]
	return Card(
		numeric_rank=int(FACE_VALUES.get(rank, rank)),
		rank=rank,
		suit=suit
	)

def parse_cards(cards):
	return [parse_card(card) for card in cards]

def encode_hand(hand):
	type_score = [
			'High card',
			'One pair',
			'Two pair',
			'Three of a kind',
			'Straight',
			'Flush',
			'Full house',
			'Four of a kind',
			'Straight flush',
			'Royal flush',
		]
	typ = type_score.index(hand[0])
	return f"{typ:#X}" + "".join([f"{x:X}" for x in hand[1]])

def evaluate_hand(cards):
	ranks = [card.numeric_rank for card in cards]
	suits = [card.suit for card in cards]
	if is_consecutive(ranks):
		return (
			('Straight', [max(ranks), *([0] * 4)]) if not all_equal(suits) else
			('Straight flush', [max(ranks), *([0] * 4)]) if max(ranks) < 14 else
			('Royal flush', [14, *([0] * 4)])
		)
	wheel = [x if x != 14 else 1 for x in ranks]
	if is_consecutive(wheel):
		return (
			('Straight', [5, *([0] * 4)]) if not all_equal(suits) else
			('Straight flush', [5, *([0] * 4)])
		)
	if all_equal(suits):
		return ('Flush', sorted(ranks, reverse = True))
	return {
		4 + 4 + 4 + 4 + 1: ('Four of a kind', [max(set(ranks), key=ranks.count), *([0] * 4)]),
		3 + 3 + 3 + 2 + 2: ('Full house',[max(set(ranks), key=ranks.count), min(set(ranks), key=ranks.count), *([0] * 3)]),
		3 + 3 + 3 + 1 + 1: ('Three of a kind',[max(set(ranks), key=ranks.count), *sorted(set(ranks), key=ranks.count)[:-1], *([0] * 2)]),
		2 + 2 + 2 + 2 + 1: ('Two pair',[*sorted(sorted(set(ranks), key=ranks.count)[1:], reverse = True), min(set(ranks), key=ranks.count), *([0] * 2)]),
		2 + 2 + 1 + 1 + 1: ('One pair',[max(set(ranks), key=ranks.count), *sorted(sorted(set(ranks), key=ranks.count)[:-1], reverse = True), *([0] * 1)]),
		1 + 1 + 1 + 1 + 1: ('High card', sorted(ranks, reverse = True)),
	}[sum(ranks.count(r) for r in ranks)]

class Poker(socketio.AsyncNamespace):

	def __init__(self, path, loop):
		super().__init__(path)
		self.queue = asyncio.Queue(1)
		self.users = []
		self.cards = []
		self.loop = loop
		self.turn_time = 20
		self.clear_state(True)

	async def notify_state(self, msg = "", reveal = False):
		for username in self.users:
			state = self.state
			state["message"] = msg
			if not reveal:
				state["hand"]["hole_cards"] = {username: self.state["hand"]["hole_cards"].get(username, "")}
				state["hand"]["hands"] = {username: self.state["hand"]["hands"].get(username, "")}
			await sio.emit('state', state, to=username)

	async def timer (self, future, time, interval = 5):
		time_left = time
		for _ in range(time // interval):
			await asyncio.sleep(interval)
			if future.done():
				break
			time_left -= interval
			self.state["turn"]["timer"] = time_left
			await self.notify_state()

		if not future.done():
			future.set_result("timeout")

	def clear_state(self, total = False):
		state = {}
		if total:
			state["table"] = {}
			state["table"]["players_chips"] = {}
			state["table"]["seats"] = [""] * 9
		state["hand"] = {}
		state["hand"]["positions"] = []
		state["hand"]["hole_cards"] = {}
		state["hand"]["pot"] = 0
		state["hand"]["community_cards"] = []
		state["hand"]["hands"] = {}
		state["round"] = {}
		state["round"]["chips_out"] = {}
		state["round"]["street"] = ""
		state["round"]["last_action"] = {}
		state["round"]["last_bet_player"] = 0
		state["round"]["first_to_act"] = 0
		state["round"]["over"] = 0
		state["turn"] = {}
		state["turn"]["timer"] = self.turn_time
		state["turn"]["action_player"] = 0
		state["turn"]["bet_size"] = 0
		self.state = state

	async def wait_for_turn (self, future, username):
		while not future.done():
			await self.queue.get()
			if self.state["hand"]["positions"][self.state["turn"]["action_player"]] != username:
				break
		if not future.done():
			future.set_result("taken")

	async def turn_timer(self, time, username):
		loop = self.loop
		done = loop.create_future()
		loop.create_task(self.wait_for_turn(done, username))
		loop.create_task(self.timer(done, time))
		return await done

	async def new_hand (self, state):
		seats = state["table"]["seats"]
		positions = state["hand"]["starting_positions"]
		yeets = []
		for seat in seats:
			if not seat == "":
				yeets.append(seat)
		lmao = 0
		x = 0
		for pos in range(len(positions)):
			if positions[pos] in yeets:
				x = yeets.index(positions[pos])
				lmao = 1
				break
		positions = [yeets[x]]
		if not lmao:
			positions = yeets
		else:
			if pos >= len(yeets):
				pos = len(yeets) - 1
			pos = (pos - 1) % len(yeets)
			i = 1
			while len(positions) < len(yeets):
				if i <= pos:
					positions.insert(0, yeets[x - i])
				else:
					positions.insert(pos + 1, yeets[x - i])
				i += 1

		state["hand"]["positions"] = positions
		state["hand"]["starting_positions"] = positions

		deck = [card + suit for card in ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"] for suit in ["S", "H", "C", "D"]]
		random.shuffle(deck)
		self.cards = deck

		state["round"]["street"] = "preflop"

		small_blind = state["table"]["big_blind"] / 2
		for i in range(2):
			blind = small_blind * (i + 1)
			action = "bigblind" if i == 1 else "smallblind"
			state["table"]["players_chips"][positions[i]] -= blind
			state["round"]["chips_out"][positions[i]] = blind
			state["round"]["last_action"][positions[i]] = action

		state["round"]["first_to_act"] = 2
		state["round"]["last_bet_player"] = 2
		state["turn"]["bet_size"] = 2 * small_blind

		for player in positions:
			state["hand"]["hole_cards"][player] = [self.cards.pop() for x in range(2)]

		return state

	async def find_hands(self):
		hole_cards = [(player, self.state["hand"]["hole_cards"][player]) for player in self.state["hand"]["starting_positions"]]
		community_cards = self.state["hand"]["community_cards"]
		for player, cards in hole_cards:
			total = community_cards + cards
			hand = max([evaluate_hand(x) for x in itertools.combinations(total, 5)], key = encode_hand)
			self.state["hand"]["hands"][player] = hand
 
	async def find_winner(self):
		final_hands = [(player, encode_hand(self.state["hand"]["hands"][player])) for player in self.state["hand"]["positions"]]
		winning_hand = max(final_hands, key = lambda x: x[1])
		if [hand[1] for hand in final_hands].count(winning_hand[1]) > 0: #IF THERE IS A TIE
			tied = []
			for hand in final_hands:
				if hand[1] == winning_hand[1]:
					tied.append(hand[0])
			chips_each = self.state["hand"]["pot"] // len(tied)
			for user in tied:
				self.state["table"]["players_chips"][user] += chips_each
			self.state["hand"]["pot"] = 0
			return
		self.state["table"]["players_chips"][winning_hand[0]] += self.state["hand"]["pot"]
		self.state["hand"]["pot"] = 0

	async def main(self):
		print('bruh')
		hand_running = False
		while True:
			action, user = await self.queue.get()
			print(user, action)
			positions = self.state["hand"]["positions"]
			if hand_running and action in ["check", "call", "raise", "fold", "timeout", "loop_event"]:
				action_player = self.state["turn"]["action_player"]
				if self.state["round"]["over"]:
					street = self.state["round"]["street"]
					for player in self.state["hand"]["starting_positions"]:
						chips_out = self.state["round"]["chips_out"][player]
						self.state["hand"]["pot"] += chips_out
						self.state["round"]["chips_out"][player] = 0
						self.state["round"]["last_action"][player] = ""
					self.state["round"]["first_to_act"] = 0
					self.state["round"]["last_bet_player"] = 0
					self.state["round"]["over"] = 0
					self.state["turn"]["bet_size"] = 0
					self.state["turn"]["action_player"] = 0
					action_player = 0
					self.state["turn"]["timer"] = self.turn_time
					if len(positions) == 1:
						self.state["table"]["players_chips"][positions[0]] += self.state["hand"]["pot"]
						self.state["hand"]["pot"] = 0
						hand_running = False
					elif street == "preflop":
						cards = [self.cards.pop() for x in range(3)]
						self.state["hand"]["community_cards"].extend(cards)
						self.state["round"]["street"] = "flop"
						await self.find_hands()
					elif street == "flop":
						cards = [self.cards.pop()]
						self.state["hand"]["community_cards"].extend(cards)
						self.state["round"]["street"] = "turn"
						await self.find_hands()
					elif street == "turn":
						cards = [self.cards.pop()]
						self.state["hand"]["community_cards"].extend(cards)
						self.state["round"]["street"] = "river"
						await self.find_hands()
					elif street == "river":
						hand_running = False
						await self.find_winner()
						await self.notify_state(True)
						self.queue.put(("loop_event", None))
						continue
					await self.notify_state()
				action_player_name = positions[action_player]
				result = await self.turn_timer(self.turn_time, action_player_name)
				if result == "timeout":
					if self.state["round"]["last_bet_player"] == (action_player + 1) % len(positions) or len(positions) == 2:
						self.state["round"]["over"] = 1
					else:
						self.state["turn"]["action_player"] = (action_player + 1) % len(positions) - 1
					self.state["round"]["last_action"][action_player_name] = "fold"
					del self.state["hand"]["positions"][action_player]
				self.state["turn"]["timer"] = self.turn_time
				await self.notify_state()
			elif action == "join":
				if len(self.state["table"]["players_chips"]) > 1:
					print("detected >1 player at table")
					self.state = await self.new_hand(self.state)
					print("new hand made")
					hand_running = True
					await self.notify_state()
					self.queue.put(("loop_event", None))

	async def on_connect(self, sid, environ):
		cookies = SimpleCookie()
		cookies.load(environ['HTTP_COOKIE'])
		if 'access_token' not in cookies:
			await sio.send({"error": "re-authenticate"}, sid)
			await sio.disconnect(sid)
			return
		token = cookies['access_token'].value
		try:
			username = jwt.decode(token, JWT_SECRET)['user_id']
		except Exception:
			await sio.send({"error": "re-authenticate"}, sid)
			await sio.disconnect(sid)
			return
		async with sio.session(sid) as session:
			session['username'] = username

	async def on_json(self, sid, data):
		action = data["action"]
		username = sio.get_session(sid).get("username")
		if not username:
			await sio.send({"error": "You are not authenticated"}, sid)
			return
		if action == "join":
			amount = int(data["amount"])
			if self.state["table"]["players_chips"].get(username):
				await sio.send({"error": "already joined"}, sid)
				return
			if database.join(username, amount) != "success":
				await sio.send({"error": "something went wrong"}, sid)
				return
			seat = int(data["seat"])
			if self.state["table"]["seats"][seat] != "":
				await sio.send({"error": "seat taken"}, sid)
				return
			self.state["table"]["players_chips"][username] = amount
			self.state["table"]["seats"][seat] = username
			self.queue.put(("join", username))
			await sio.send({"success": True}, sid)
			await self.notify_state("test")
		if action == "leave":
			if not self.state["table"]["players_chips"].get(username):
				await sio.send({"error": "not at table"}, sid)
				return
			chips = self.state["table"]["players_chips"][username]
			seat = self.state["table"]["seats"].index(username)
			if database.leave(username, chips) != "success":
				await sio.send({"error": "something went wrong"}, sid)
				return
			del self.state["table"]["players_chips"][username]
			del self.state["table"]["seats"][seat]
			await sio.send({"success": True}, sid)
			await self.notify_state("test")
		if action == "state":
			await self.notify_state()
	
	async def on_disconnect(self, sid):
		username = await sio.get_session(sid).get("username")
		self.users.remove(username) if username in self.users else 0

		# try:
			# data = json.loads(message)
			# action = data["action"]
			# state = await self.get_state()
			# positions = state["hand"]["positions"]
			# action_player = state["turn"]["action_player"]
			# action_player_name = positions[action_player]
			# bet_size = state["turn"]["bet_size"]
			# chips_out = state["round"]["chips_out"][action_player_name]
			# if action_player_name == username:
			# 	if action["name"] == "check":
			# 		if bet_size == chips_out:
			# 			reveal = False
			# 			if state["round"]["first_to_act"] == (action_player + 1) % len(positions):
			# 				state["round"]["over"] = 1
			# 				if state["round"]["street"] == "river":
			# 					reveal = True
			# 			else:
			# 				state["turn"]["action_player"] = (action_player + 1) % len(positions)
			# 			state["round"]["last_action"][username] = "check"
			# 			await self.set_state(state)
			# 			await self.notify_state(reveal)
			# 		else:
			# 			await websocket.send("cannotcheck")
			# 	elif action["name"] == "call":
			# 		if bet_size != 0:
			# 			reveal = False
			# 			if state["round"]["last_bet_player"] == (action_player + 1) % len(positions):
			# 				state["round"]["over"] = 1
			# 				if state["round"]["street"] == "river":
			# 					reveal = True
			# 			else:
			# 				state["turn"]["action_player"] = (action_player + 1) % len(positions)
			# 			state["round"]["last_action"][username] = "call"
			# 			chip_difference = bet_size - state["round"]["chips_out"][username]
			# 			state["round"]["chips_out"][username] = bet_size
			# 			state["table"]["players_chips"][username] -= chip_difference
			# 			await self.set_state(state)
			# 			await self.notify_state(reveal)
			# 	elif action["name"] == "bet":
			# 		bet = action["chips"] + bet_size
			# 		if bet_size < bet:
			# 			state["turn"]["bet_size"] = bet
			# 			state["round"]["chips_out"][username] = bet
			# 			state["table"]["players_chips"][username] -= action["chips"]
			# 			state["round"]["last_action"][username] = f"bet {action['chips']}"
			# 			state["round"]["last_bet_player"] = action_player
			# 			state["turn"]["action_player"] = (action_player + 1) % len(positions)
			# 			await self.set_state(state)
			# 			await self.notify_state()
			# 		else:
			# 			await websocket.send("cannotbet")
			# 	elif action["name"] == "fold":
			# 		reveal = False
			# 		if state["turn"]["last_bet_player"] == (action_player + 1) % len(positions):
			# 			state["round"]["over"] = 1
			# 		else:
			# 			state["turn"]["action_player"] = (action_player + 1) % len(positions)
			# 		state["turn"]["action_player"] -= 1
			# 		state["round"]["last_action"][username] = "fold"
			# 		del state["hand"]["positions"][action_player]
			# 		await self.set_state(state)
			# 		await self.notify_state(reveal)
			# 	else:
			# 		continue
		# except Exception as e:
		#     print(e)
		# finally:
		#     requests.get(f"http://localhost:5000/leave/{username}")
		#     await self.notify_state()
		#     await self.unregister(websocket)

@app.route("/poker/")
async def homepage(request):
	if request.args.get('login') == 'fail':
		request.ctx.session["logged_in"] = 0
	if int(request.ctx.session.get("logged_in", 0)):
		return redirect(app.url_for("lobby", _external=True, _scheme="https", _server="le0.tech"), status=303)
	res = env.get_template('homepage.html').render()
	return html(res)

@app.route("/poker/lobby/")
@sanicjwt.protected(redirect_on_fail=True)
@sanicjwt.inject_user()
async def lobby(request, user):
	res = env.get_template('homepage.html').render(avatar=request.ctx.session.get("avatar"))
	return html(res)

@app.route("/poker/login")
async def login(request):
	flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
		'client_secret.json',
		['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile'])
	flow.redirect_uri = app.url_for('token', _external=True, _scheme="https", _server='le0.tech')
	authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
	request.ctx.session['state'] = state
	return redirect(authorization_url, status=303)

@app.route("/poker/logout")
@sanicjwt.protected()
@sanicjwt.inject_user()
async def logout(request, user):
	if int(request.ctx.session.get("logged_in", 0)):
		request.ctx.session["logged_in"] = 0
	return text("done")

@app.route("/poker/token")
async def token(request):
	state = request.ctx.session.get('state')
	if not state:
		return text("failed")
	flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
    	'client_secret.json',
    	scopes=['openid','https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile'], state=state)
	flow.redirect_uri = app.url_for('token', _external=True, _scheme="https", _server='le0.tech')
	authorization_response = request.url.replace("http", "https")
	flow.fetch_token(authorization_response=authorization_response)
	token = flow.credentials.id_token
	decoded = jwt.decode(token, verify=False)
	request.ctx.session['logged_in'] = 1
	email = decoded["email"]
	nonce = token_urlsafe(8)
	OPEN_LOGINS[nonce] = email
	request.ctx.session['avatar'] = decoded["picture"]
	res = env.get_template('auth.html').render(nonce=nonce)
	return html(res)

server = app.create_server(host="0.0.0.0", port=5000, debug=True, return_asyncio_server=True)

loop = asyncio.get_event_loop()
game = Poker(None, loop)
sio.register_namespace(game)
task1 = asyncio.ensure_future(server, loop=loop)
#task2 = asyncio.ensure_future(game.main(), loop=loop)
loop.run_forever()
loop.close()