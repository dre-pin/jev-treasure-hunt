# jev-treasure-hunt

A terminal treasure hunt on a 30x30 grid. Steer `@` onto the `$` yourself with
the arrow keys, or hand the controls to the Jev model and watch it play one
typed decision at a time.

```
##############################
#............................#
#...@........................#
#............................#
#.................$..........#
...
##############################
```

## Setup

Requires Python 3.10+ (the TypeSafe SDK does) and a terminal on macOS or Linux.

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env   # then put your TypeSafe API key in .env
```

Without `uv`, use any Python 3.10+ (`python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`).
macOS's built-in `python3` is 3.9 and pip will report no matching `typesafe-sdk`.

The API key is only used by `--player jev`, but both modes need the SDK installed.

## Play

```bash
.venv/bin/python game.py                         # you play, open map
.venv/bin/python game.py --walls                 # random walls (25% of cells)
.venv/bin/python game.py --walls 0.35            # denser walls
.venv/bin/python game.py --player jev --walls    # Jev plays
.venv/bin/python game.py --walls --seed 482113   # replay a specific map
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--player me\|jev` | `me` | Who controls `@`. |
| `--walls [DENSITY]` | off | Fill a fraction of interior cells with walls (0 to 0.6; 0.25 if no value). |
| `--seed N` | random | Map seed. The same seed and `--walls` value always give the same map. |
| `--max-moves N` | none (`me`), 300 (`jev`) | End the game after `N` moves, e.g. `--max-moves 100` for comparable Jev runs. |
| `--previous-map` | off | Jev only: also send the map as it was before the last move, above the current map. |
| `--debug` | off | Jev only: write every call's full input and output to `debug_logs/<seed>_<settings>_<time>.log`, e.g. `4821_walls0.25_max100_20260922-205128.log`. |

With `--debug`, each run gets its own log in `debug_logs/` (git-ignored). Every Jev call
appends the full state (prompt and map), instructions, criteria, the pick with its
probabilities, and the raw response; the log ends with the run summary. To watch it live,
run this in a second terminal window. `latest.log` always points at the newest run:

```bash
tail -F debug_logs/latest.log
```

Keys: arrow keys move (in `me` mode), `r` starts a new map, `q` or Ctrl-C quits.
The status line shows the seed and move count; in `jev` mode it also shows each
pick, its confidence, and the call latency.

When you quit, a summary is printed:

```
Player: jev (jev-<version>)
Seed: 482113
Moves: 57
Treasure: REACHED
```

`Treasure` is `REACHED` or `NOT_REACHED` for the game in progress when you quit.
For Jev, `Player` shows the model version the API reports answering with.

## How it works

- **Maps.** Walls are placed at random from the seed, then a breadth-first search
  checks there is a path from `@` to `$`. If not, the layout is rerolled, so the
  treasure is always reachable.
- **Jev player.** Each move is one TypeSafe `Choice` over `up`, `down`, `left`,
  `right`. The state sent to the model is the plain-text map and the last ten
  moves. There are no coordinates: Jev has to find `@` and `$` on the map
  itself. Moves into a wall leave `@` in
  place but still count. Jev stops after 300 moves unless `--max-moves` says otherwise.

## Files

- `game.py` – the game, rendering, and both players.
- `jev_client.py` – `.env` loading and the TypeSafe client for the Jev model.
