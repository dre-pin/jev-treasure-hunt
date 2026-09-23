"""Treasure hunt: steer @ to the $.

With --player me you drive with the arrow keys. With --player jev, every move is a
Choice over up/down/left/right asked of the Jev model.

Usage:
    .venv/bin/python game.py --player me
    .venv/bin/python game.py --player me --walls        # random walls, 25% density
    .venv/bin/python game.py --player me --walls 0.35   # denser walls
    .venv/bin/python game.py --walls --seed 4821        # replay a map by its seed
    .venv/bin/python game.py --player jev               # needs TYPESAFE_API_KEY in .env

Keys: r restarts, q quits (both modes).
"""

import argparse
import os
import random
import select
import sys
import termios
import time
import tty
from collections import deque
from datetime import datetime
from pathlib import Path

from typesafe_sdk import Choice

from jev_client import MODEL, make_client

SIZE = 30  # including the outer wall
WALL, FLOOR, PLAYER, MONEY = "#", ".", "@", "$"
PLAYER_COLOR, MONEY_COLOR, RESET = "\x1b[1;36m", "\x1b[1;33m", "\x1b[0m"  # bold cyan, bold yellow
MAX_JEV_MOVES = 300  # safety cap for jev when --max-moves isn't given
DEFAULT_WALL_DENSITY = 0.25
DEBUG_DIR = Path(__file__).resolve().parent / "debug_logs"

DIRECTIONS = {
    "up": (-1, 0),
    "down": (1, 0),
    "right": (0, 1),
    "left": (0, -1),
}
ARROW_KEYS = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
}
CRITERIA = {
    "up": "Move @ one cell up, to the line above.",
    "down": "Move @ one cell down, to the line below.",
    "left": "Move @ one cell left on its line.",
    "right": "Move @ one cell right on its line.",
}


def random_floor_cell(rng):
    return rng.randint(1, SIZE - 2), rng.randint(1, SIZE - 2)


def is_open(cell, walls):
    r, c = cell
    return 0 < r < SIZE - 1 and 0 < c < SIZE - 1 and cell not in walls


def reachable(start, goal, walls):
    """Breadth-first search over open cells."""
    seen, queue = {start}, deque([start])
    while queue:
        cell = queue.popleft()
        if cell == goal:
            return True
        for dr, dc in DIRECTIONS.values():
            nxt = (cell[0] + dr, cell[1] + dc)
            if nxt not in seen and is_open(nxt, walls):
                seen.add(nxt)
                queue.append(nxt)
    return False


def new_seed():
    return random.randrange(1_000_000)


def new_game(seed, wall_density=0.0):
    """Place @ and $, plus random interior walls; reroll until $ is reachable from @.

    The same seed and density always produce the same map.
    """
    rng = random.Random(seed)
    while True:
        player = random_floor_cell(rng)
        money = random_floor_cell(rng)
        if money == player:
            continue
        walls = set()
        if wall_density > 0:
            walls = {
                (r, c)
                for r in range(1, SIZE - 1)
                for c in range(1, SIZE - 1)
                if rng.random() < wall_density
            } - {player, money}
        if reachable(player, money, walls):
            return player, money, walls


def move(player, direction, walls):
    """Return the new position, staying put if the move would hit a wall."""
    dr, dc = DIRECTIONS[direction]
    nxt = (player[0] + dr, player[1] + dc)
    return nxt if is_open(nxt, walls) else player


def grid_text(player, money, walls, newline="\n"):
    rows = []
    for r in range(SIZE):
        cells = []
        for c in range(SIZE):
            if (r, c) == player:
                cells.append(PLAYER)
            elif (r, c) == money:
                cells.append(MONEY)
            elif r in (0, SIZE - 1) or c in (0, SIZE - 1) or (r, c) in walls:
                cells.append(WALL)
            else:
                cells.append(FLOOR)
        rows.append("".join(cells))
    return newline.join(rows)


def render(player, money, walls, status):
    # Home the cursor and clear to end of screen instead of a full clear, to avoid flicker.
    # Color only the terminal view; the model still gets the plain grid from grid_text.
    grid = grid_text(player, money, walls, "\r\n")
    grid = grid.replace(PLAYER, PLAYER_COLOR + PLAYER + RESET).replace(MONEY, MONEY_COLOR + MONEY + RESET)
    frame = "\x1b[H" + grid + "\r\n\r\n" + status + "\x1b[K\x1b[J"
    sys.stdout.write(frame)
    sys.stdout.flush()


def read_key(fd, timeout=None):
    """Read one keypress (arrow keys are 3-byte escapes). Returns "" on timeout."""
    if not select.select([fd], [], [], timeout)[0]:
        return ""
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":
        while select.select([fd], [], [], 0.01)[0]:
            ch += os.read(fd, 1).decode(errors="ignore")
            if len(ch) >= 3:
                break
    return ch


class HumanPlayer:
    label = "me"

    def start_game(self, seed):
        pass

    def describe(self):
        return self.label

    def next_move(self, fd, player, money, walls, history):
        """Block on the keyboard; return a direction, or a control key ("q"/"r")."""
        while True:
            key = read_key(fd)
            if key in ARROW_KEYS:
                return ARROW_KEYS[key], ""
            if key.lower() in ("q", "r") or key == "\x03":
                return key.lower() if key != "\x03" else "q", ""


class JevPlayer:
    label = "jev"

    def __init__(self, debug_path=None, previous_map=False):
        self.previous_map = previous_map
        self.last_player = None  # where @ was on the previous call, for --previous-map
        self.client = make_client()
        self.model = MODEL  # replaced by the version the API reports answering with
        self.instructions = "Which direction should @ move next?"
        self.criteria = CRITERIA
        self.question = {"move": Choice(instructions=self.instructions, criteria=self.criteria)}
        # One file per run; every call appends its full input and output.
        self.debug_file = open(debug_path, "a") if debug_path else None

    def debug(self, text):
        if self.debug_file:
            self.debug_file.write(text + "\n")
            self.debug_file.flush()

    def start_game(self, seed):
        self.last_player = None
        self.debug(f"\n{'#' * 70}\nNEW GAME  seed {seed}\n{'#' * 70}")

    def build_state(self, player, money, walls, history):
        recent = ", ".join(history[-10:]) or "none yet"
        return (
            "You are playing a 2D grid treasure hunt. Move the player (@) onto the treasure $.\n"
            f"Legend: {WALL} wall, {FLOOR} empty floor, {PLAYER} you, {MONEY} treasure. "
            "Walls block movement. You must go around. You cannot move diagonally.\n"
            "The map is drawn top to bottom: the first line is the top edge and each "
            "character is one cell.\n\n"
            + self.previous_map_text(money, walls)
            + f"{grid_text(player, money, walls)}\n\n"
            f"Moves so far: {len(history)}. Last moves: {recent}."
        )

    def previous_map_text(self, money, walls):
        """With --previous-map, the map as it was before the last move, then a heading for the current one."""
        if not self.previous_map:
            return ""
        if self.last_player is None:
            return "Previous map: none yet, this is the first move.\n\nCurrent map:\n"
        return (
            "Previous map (before your last move):\n"
            f"{grid_text(self.last_player, money, walls)}\n\n"
            "Current map:\n"
        )

    def next_move(self, fd, player, money, walls, history):
        # Let q/r interrupt between model calls.
        key = read_key(fd, timeout=0)
        if key.lower() in ("q", "r") or key == "\x03":
            return key.lower() if key != "\x03" else "q", ""
        state = self.build_state(player, money, walls, history)
        self.last_player = player
        # Logged before the call, so the debug window shows the request while Jev is thinking.
        self.debug(
            f"\n{'=' * 25} move {len(history) + 1} {'=' * 25}\n"
            f"--- STATE ---\n{state}\n"
            f"--- INSTRUCTIONS ---\n{self.instructions}\n"
            "--- CRITERIA ---\n" + "\n".join(f"{k}: {v}" for k, v in self.criteria.items())
        )
        started = time.perf_counter()
        response = self.client.system_one(state=state, questions=self.question)
        latency_ms = (time.perf_counter() - started) * 1000
        self.model = response.model
        answer = response.choices["move"]
        probs = answer.probabilities
        if hasattr(probs, "items"):
            probs = ", ".join(f"{k} {v:.2f}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))
        if self.debug_file:
            try:
                raw = response.model_dump_json(indent=2)
            except AttributeError:
                raw = repr(response)
            self.debug(
                f"--- RESPONSE ({response.model}, {latency_ms:.0f} ms) ---\n"
                f"pick: {answer.choice}  confidence: {answer.confidence:.2f}\n"
                f"probabilities: {probs}\n"
                f"--- RAW RESPONSE ---\n{raw}"
            )
        return answer.choice, f"   jev: {answer.choice} ({answer.confidence:.2f}, {latency_ms:.0f} ms)"

    def describe(self):
        return f"{self.label} ({self.model})"

    def close(self):
        self.client.close()
        if self.debug_file:
            self.debug_file.close()


def play(fd, controller, wall_density, seed, result, max_moves=None):
    """Run the game loop, keeping `result` up to date with the current game's outcome."""
    player, money, walls = new_game(seed, wall_density)
    controller.start_game(seed)
    history, won, detail = [], False, ""
    while True:
        result.update(seed=seed, moves=len(history), won=won)
        status = f"[{controller.label}] Seed: {seed}  Moves: {len(history)}{detail}"
        out_of_moves = max_moves is not None and len(history) >= max_moves
        finished = won or out_of_moves
        if won:
            status += "   Found the treasure!  r = play again, q = quit"
        elif finished:
            status += f"   Out of moves ({max_moves}).  r = play again, q = quit"
        elif controller.label == "me":
            status += "   Arrow keys to move, r = restart, q = quit"
        else:
            status += "   r = restart, q = quit"
        render(player, money, walls, status)

        if finished:
            action = read_key(fd).lower()
        else:
            action, detail = controller.next_move(fd, player, money, walls, history)
        if action in ("q", "\x03"):
            return
        if action == "r":
            seed = new_seed()
            player, money, walls = new_game(seed, wall_density)
            controller.start_game(seed)
            history, won, detail = [], False, ""
        elif action in DIRECTIONS and not finished:
            player = move(player, action, walls)
            history.append(action)
            won = player == money


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--player", choices=["me", "jev"], default="me", help="who controls @")
    parser.add_argument(
        "--walls",
        nargs="?",
        type=float,
        const=DEFAULT_WALL_DENSITY,
        default=0.0,
        metavar="DENSITY",
        help=f"add random interior walls (fraction of cells, default {DEFAULT_WALL_DENSITY}); "
        "the treasure is always reachable",
    )
    parser.add_argument("--seed", type=int, help="replay a specific map (shown in the status line)")
    parser.add_argument(
        "--max-moves",
        type=int,
        metavar="N",
        help=f"end the game after N moves (default: no limit for me, {MAX_JEV_MOVES} for jev)",
    )
    parser.add_argument(
        "--previous-map",
        action="store_true",
        help="also send jev the map as it was before its last move",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="write every Jev call's full input and output to "
        "debug_logs/<seed>_<settings>_<time>.log; "
        "watch it with `tail -f` in another window",
    )
    args = parser.parse_args()
    if not 0 <= args.walls < 0.6:
        parser.error("--walls density must be between 0 and 0.6")

    if args.max_moves is not None and args.max_moves < 1:
        parser.error("--max-moves must be at least 1")
    max_moves = args.max_moves
    if max_moves is None and args.player == "jev":
        max_moves = MAX_JEV_MOVES
    if (args.debug or args.previous_map) and args.player != "jev":
        parser.error("--debug and --previous-map only apply to --player jev")
    seed = new_seed() if args.seed is None else args.seed
    debug_path = None
    if args.debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        settings = f"walls{args.walls}_max{max_moves}" + ("_prevmap" if args.previous_map else "")
        debug_path = DEBUG_DIR / f"{seed}_{settings}_{datetime.now():%Y%m%d-%H%M%S}.log"
        debug_path.touch()
        # latest.log always points at the newest run, so `tail -F debug_logs/latest.log` follows it.
        latest = DEBUG_DIR / "latest.log"
        latest.unlink(missing_ok=True)
        latest.symlink_to(debug_path.name)
    if args.player == "jev":
        controller = JevPlayer(debug_path=debug_path, previous_map=args.previous_map)
        controller.debug(
            f"RUN {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"command: {' '.join(sys.argv)}\n"
            f"player: jev  walls: {args.walls}  seed: {seed}  max-moves: {max_moves}  "
            f"previous-map: {args.previous_map}"
        )
    else:
        controller = HumanPlayer()
    result = {"seed": seed, "moves": 0, "won": False}
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        tty.setraw(fd)
        play(fd, controller, args.walls, seed, result, max_moves)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")  # show cursor, leave alternate screen
        sys.stdout.flush()
        summary = (
            f"Player: {controller.describe()}\n"
            f"Seed: {result['seed']}\n"
            f"Moves: {result['moves']}\n"
            f"Treasure: {'REACHED' if result['won'] else 'NOT_REACHED'}"
        )
        if hasattr(controller, "close"):
            controller.debug(f"\n{'#' * 70}\nEND OF RUN\n{summary}")
            controller.close()
        print(summary)
        if debug_path:
            print(f"Debug log: {debug_path}")


if __name__ == "__main__":
    main()
