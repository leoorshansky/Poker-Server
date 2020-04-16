import random
import asyncio
import ssl
import json
import websockets
import jwt
import requests
import itertools
from collections import namedtuple
import logging
logger = logging.getLogger('websockets')
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

TOKEN_SECRET = b"80O1dGcfN63w6kDrllmP8bgH3m87HABdFYFAybV5t1diubzyuUF2pb92gEVphVD"

#ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
#ssl_context.load_cert_chain("/etc/letsencrypt/live/studyingforwhap.ml/fullchain.pem", "/etc/letsencrypt/live/studyingforwhap.ml/privkey.pem")

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
    # wheel = [int(s) for s in " ".join([str(i) for i in ranks]).replace("14", "1").split(" ")]
    # lmao
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

class Poker:

    def __init__(self, loop):
            self.queue = asyncio.Queue(1)
            self.USERS = {}
            self.cards = []
            self.loop = loop
            self.turn_time = 20

    async def get_state(self):
        with open("/var/www/html/bruh/poker/gamestatus.json", "r") as openfile:
           return json.load(openfile)

    async def set_state(self, state):
        with open("/var/www/html/bruh/poker/gamestatus.json", "w") as openfile:
            return json.dump(state, openfile)

    async def register(self, websocket, username):
        self.USERS[websocket] = username

    async def unregister(self, websocket):
        del self.USERS[websocket]

    async def state_event(self, username, reveal = False):
        state = await self.get_state()
        if not reveal:
            state["hand"]["hole_cards"] = {username: state["hand"]["hole_cards"].get(username, "")}
            state["hand"]["hands"] = {username: state["hand"]["hands"].get(username, "")}
        return json.dumps(state)

    async def notify_state(self, reveal = False):
        if self.USERS: # asyncio.wait doesn't accept an empty list
            await asyncio.wait([user.send(await self.state_event(self.USERS[user], reveal)) for user in self.USERS])
        await asyncio.sleep(0.5)
        await self.queue.put(1)

    async def timer (self, future, time, interval = 5):
        time_left = time
        for i in range(time // interval):
            await asyncio.sleep(interval)
            if future.done():
                break
            time_left -= interval
            state = await self.get_state()
            state["turn"]["timer"] = time_left
            await self.set_state(state)
            await self.notify_state()

        if not future.done():
            future.set_result("timeout")

    async def clear_state(self, total = False):
        state = await self.get_state()
        if total:
            state["table"]["players_chips"] = {}
            state["table"]["seats"] = [""] * 9
        state["hand"]["positions"] = []
        state["hand"]["hole_cards"] = {}
        state["hand"]["pot"] = 0
        state["hand"]["community_cards"] = []
        state["round"]["chips_out"] = {}
        state["round"]["street"] = ""
        state["round"]["last_action"] = {}
        state["round"]["last_bet_player"] = 0
        state["round"]["first_to_act"] = 0
        state["round"]["over"] = 0
        state["turn"]["timer"] = self.turn_time
        state["turn"]["action_player"] = 0
        state["turn"]["bet_size"] = 0
        await self.set_state(state)
        await self.notify_state()

    async def wait_for_turn (self, future, username):
        while not future.done():
            await self.queue.get()
            state = await self.get_state()
            if state["hand"]["positions"][state["turn"]["action_player"]] != username:
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
        state = await self.get_state()
        hole_cards = [(player, state["hand"]["hole_cards"][player]) for player in state["hand"]["starting_positions"]]
        community_cards = state["hand"]["community_cards"]
        for player, cards in hole_cards:
            total = community_cards + cards
            hand = max([evaluate_hand(x) for x in itertools.combinations(total, 5)], key = encode_hand)
            state["hand"]["hands"][player] = hand
 
    async def find_winner(self):
        state = await self.get_state()
        winner = max([(player, state["hand"]["hands"][player]) for player in state["hand"]["positions"]], key = lambda x: encode_hand(x[1]))[0]
        state["table"]["players_chips"][winner] += state["hand"]["pot"]
        state["hand"]["pot"] = 0
        await self.set_state(state)

    async def main(self):
        hand_running = False
        turn = ""
        while True:
            await self.queue.get()
            state = await self.get_state()
            positions = state["hand"]["positions"]
            if hand_running:
                action_player = state["turn"]["action_player"]
                if state["round"]["over"]:
                    street = state["round"]["street"]
                    for player in state["hand"]["starting_positions"]:
                        chips_out = state["round"]["chips_out"][player]
                        state["hand"]["pot"] += chips_out
                        state["round"]["chips_out"][player] = 0
                        state["round"]["last_action"][player] = ""
                    state["round"]["first_to_act"] = 0
                    state["round"]["last_bet_player"] = 0
                    state["round"]["over"] = 0
                    state["turn"]["bet_size"] = 0
                    state["turn"]["action_player"] = 0
                    state["turn"]["timer"] = self.turn_time
                    turn = ""
                    positions = state["hand"]["positions"]
                    if len(positions) == 1:
                        state["table"]["players_chips"][positions[0]] += state["hand"]["pot"]
                        state["hand"]["pot"] = 0
                        hand_running = False
                    elif street == "preflop":
                        cards = [self.cards.pop() for x in range(3)]
                        state["hand"]["community_cards"].extend(cards)
                        state["round"]["street"] = "flop"
                        await self.find_hands()
                    elif street == "flop":
                        cards = [self.cards.pop()]
                        state["hand"]["community_cards"].extend(cards)
                        state["round"]["street"] = "turn"
                        await self.find_hands()
                    elif street == "turn":
                        cards = [self.cards.pop()]
                        state["hand"]["community_cards"].extend(cards)
                        state["round"]["street"] = "river"
                        await self.find_hands()
                    elif street == "river":
                        hand_running = False
                        await self.find_winner()

                    await self.set_state(state)
                    await self.notify_state()
                else:
                    action_player_name = state["hand"]["positions"][action_player]
                    if action_player_name != turn:
                        turn = action_player_name
                        result = await self.turn_timer(self.turn_time, turn)
                        state = await self.get_state()
                        action_player = state["turn"]["action_player"]
                        positions = state["hand"]["positions"]
                        if result == "timeout":
                            reveal = False
                            if state["round"]["last_bet_player"] == (action_player + 1) % len(positions) or len(positions) == 2:
                                state["round"]["over"] = 1
                            else:
                                state["turn"]["action_player"] = (action_player + 1) % len(positions) - 1
                            state["round"]["last_action"][turn] = "fold"
                            del state["hand"]["positions"][action_player]
                        state["turn"]["timer"] = self.turn_time
                        await self.set_state(state)
                        await self.notify_state()
                    else:
                        continue
            else:
                if len(state["table"]["players_chips"]) > 1:
                    #await asyncio.sleep(20)
                    state = await self.new_hand(state)
                    hand_running = True
                    await self.set_state(state)
                    await self.notify_state()

    async def handler(self, websocket, path):
        try:
            handshake = await websocket.recv()
            data = json.loads(handshake)
            token = data["token"]
            action = data["action"]
            if action["name"] != "join":
                raise Exception()
            chips = action["chips"]
            seat = action["seat"]
            decoded = jwt.decode(token, TOKEN_SECRET, algorithms = "HS256")
            username = decoded["username"]
            for socket, name in self.USERS.items():
                if name == username:
                    socket.close()
            await self.register(websocket, username)
            response = requests.get(f"http://localhost:5000/join/{username}?chips={chips}&seat={seat}").text
            if response == "seattaken":
                await websocket.send("seattaken")
                await self.unregister(websocket)
                return
            await websocket.send("joined")
            await self.notify_state()
        except Exception:
            await websocket.send("denied")
            await self.unregister(websocket)
            requests.get(f"http://localhost:5000/leave/{username}")
            return
        try:
            async for message in websocket:
                data = json.loads(message)
                action = data["action"]
                state = await self.get_state()
                positions = state["hand"]["positions"]
                action_player = state["turn"]["action_player"]
                action_player_name = positions[action_player]
                bet_size = state["turn"]["bet_size"]
                chips_out = state["round"]["chips_out"][action_player_name]
                if action_player_name == username:
                    if action["name"] == "check":
                        if bet_size == chips_out:
                            reveal = False
                            if state["round"]["first_to_act"] == (action_player + 1) % len(positions):
                                state["round"]["over"] = 1
                                if state["round"]["street"] == "river":
                                    reveal = True
                            else:
                                state["turn"]["action_player"] = (action_player + 1) % len(positions)
                            state["round"]["last_action"][username] = "check"
                            await self.set_state(state)
                            await self.notify_state(reveal)
                        else:
                            await websocket.send("cannotcheck")
                    elif action["name"] == "call":
                        if bet_size != 0:
                            reveal = False
                            if state["round"]["last_bet_player"] == (action_player + 1) % len(positions):
                                state["round"]["over"] = 1
                                if state["round"]["street"] == "river":
                                    reveal = True
                            else:
                                state["turn"]["action_player"] = (action_player + 1) % len(positions)
                            state["round"]["last_action"][username] = "call"
                            chip_difference = bet_size - state["round"]["chips_out"][username]
                            state["round"]["chips_out"][username] = bet_size
                            state["table"]["players_chips"][username] -= chip_difference
                            await self.set_state(state)
                            await self.notify_state(reveal)
                    elif action["name"] == "bet":
                        bet = action["chips"] + bet_size
                        if bet_size < bet:
                            state["turn"]["bet_size"] = bet
                            state["round"]["chips_out"][username] = bet
                            state["table"]["players_chips"][username] -= action["chips"]
                            state["round"]["last_action"][username] = f"bet {action['chips']}"
                            state["round"]["last_bet_player"] = action_player
                            state["turn"]["action_player"] = (action_player + 1) % len(positions)
                            await self.set_state(state)
                            await self.notify_state()
                        else:
                            await websocket.send("cannotbet")
                    elif action["name"] == "fold":
                        reveal = False
                        if state["turn"]["last_bet_player"] == (action_player + 1) % len(positions):
                            state["round"]["over"] = 1
                        else:
                            state["turn"]["action_player"] = (action_player + 1) % len(positions)
                        state["turn"]["action_player"] -= 1
                        state["round"]["last_action"][username] = "fold"
                        del state["hand"]["positions"][action_player]
                        await self.set_state(state)
                        await self.notify_state(reveal)
                    else:
                        continue
        except Exception as e:
            print(e)
        finally:
            requests.get(f"http://localhost:5000/leave/{username}")
            await self.notify_state()
            await self.unregister(websocket)

loop = asyncio.get_event_loop()
game = Poker(loop)
tasks = [
    game.clear_state(True),
    websockets.serve(game.handler, '0.0.0.0', 6789),
    game.main()
]
asyncio.ensure_future(asyncio.wait(tasks))
loop.run_forever()
