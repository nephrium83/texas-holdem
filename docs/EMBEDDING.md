# Embed poker in another game

The public Python interface is `holdem.embedding`. Its `API_VERSION = 1`
marks the local embedding contract. It reuses the same rules engine as the
desktop game; the tavern example does not copy betting, dealing, or payout
rules. See [`PACKAGING.md`](PACKAGING.md) for installation and optional host
dependencies.

This increment proves a second Python host: an RPG tavern with two independent
tables, player-filtered views, host-owned buy-ins, and one payout per table
visit. It does not establish a Unity, Unreal, C++, C#, or native Godot SDK.
Those hosts need a Python runtime or a separately implemented process/message
bridge. No such bridge is provided or validated here.

## Responsibilities

| Poker module | Surrounding game |
| --- | --- |
| Cards, legal actions, turns, pots, winners and chip results | Characters, UI, animation, sound and world interaction |
| A view for the requested poker player | Authentication and binding a connection to that player |
| Initial/final table chip balances and stable result IDs | Reserving buy-ins and crediting the game's currency ledger |
| Rejection of invalid poker commands | Command delivery, scheduling, persistence and disconnect policy |

`PokerTable.authority_mode` is `"trusted_host"`. The host can inspect its own
process and controls the random source; player-filtered snapshots do not hide
cards from that host. This API is suitable for a local example or a trusted
authority design. It does not add the desktop application's mental-poker
proofs to a local table, make an MMO authoritative, or verify a real-money
wallet. The existing authenticated P2P `Session` has a separate integration
path described below.

## Small public interface

```python
import random
from uuid import uuid4
from holdem.embedding import PlayerSeat, PokerTable, TableConfig

# A new table instance gets a new host-owned ID for every visit/reservation.
table = PokerTable(
    str(uuid4()),
    [PlayerSeat("hero", "Ranger", 500), PlayerSeat("npc", "Innkeeper", 500)],
    rng=random.SystemRandom(),
    config=TableConfig(small_blind=25, big_blind=50),
)
table.start_hand()
hero_snapshot = table.view("hero")
# Send this object only to the authenticated hero. Each opponent gets their
# own table.view(player_id), never hero_snapshot or the table object itself.
if hero_snapshot["you"].get("legal"):
    reply = table.command("hero", "check_call")
```

The host chooses two to nine distinct `PlayerSeat(player_id, name, chips)`
values. IDs are stable within a table. A `TableConfig` supplies blinds,
betting structure (default `"No-Limit"`), and the big-blind ante flag.
The host supplies the random source; seeded `random.Random` in the example
is for repeatable tests, not a fairness claim.

| Call or property | Contract |
| --- | --- |
| `table_id`, `phase`, `authority_mode` | Identity, lifecycle and explicit trust model |
| `start_hand()` | Start the first or next hand when the table permits it |
| `command(player_id, command, payload=None)` | `fold`, `check_call`, or `raise_to` with `{"amount": absolute_target}`; validates the acting player and action |
| `view(player_id)` | Owned JSON-compatible snapshot for that player only |
| `hand_result()` | Owned completed-hand chip result, or `None` |
| `close()` | Owned final table result; stable on retry; rejects a live hand |
| `result()` | Owned final close result after closure, otherwise `None` |

Lifecycle phases are `lobby`, `betting`, `settled`, and `closed`. A hand
result identifies its table and hand and contains `initial_chips`,
`final_chips`, `net_chips`, and `winner_ids`. A close result identifies the
reservation with `table_id`, `result_id = table_id + "/close"`, and the
table's initial/final chip maps. Results and views carry `api_version`.
Returned objects are copies: changing one cannot change the table state.

Snapshots include `player_id`, public `seats`, `action_on`, `events`, and
the viewer's `you.hole`. `you.legal` is offered only to the player who may
act. During play, opponents' private cards are absent. At a contested
showdown only the hands selected for display by poker rules are revealed;
a folded opponent's cards remain absent from the view.

Give each authenticated player only their own view. Never accept a client's
claimed `player_id`, result, chip balance, or winners as host authority.
The host must serialize actions for each table; this synchronous API does
not provide a concurrent scheduler. Independent table instances have
independent state and injected random sources.

## RPG tavern example

From a checkout with the package installed:

```powershell
python examples/rpg_tavern.py --demo
```

The example can also be copied into another directory and run against an
installed wheel. It imports only `holdem.embedding` and the Python standard
library; it has no checkout-relative imports or `sys.path` modification.

The demo maps world interactions onto `Tavern.sit`, `play`, `command`,
`view`, and `leave`. It starts two tables simultaneously, completes legal
call/check hands, checks private views and table isolation, retries a
payout, and makes a fresh visit. The printed report must show two completed
tables and `total_funds: 8000`.

The process-local `FakeWallet` reserves every player's buy-in together
before exposing a new table. An insufficient balance or invalid player
rejects the whole reservation. The host obtains the final result directly
from `close()` and credits final chips against that same reservation.
Validation rejects mismatched IDs, players, original buy-ins, negative or
non-integer chips, and non-conserved totals before any balance changes.
Repeated identical reservations/payouts do nothing; conflicting retries
raise an error. A new visit gets a fresh UUID even at the same tavern chair.

The demo wallet has no database, transaction locks or crash recovery. It
cannot authenticate an otherwise plausible forged result; only the trusted
host may supply the core's result. A production host must persist the
reservation and result ID with the currency transfer in one transaction,
enforce unique payout IDs, recover reservations after a crash, and implement
its own authenticated world/currency integration. Integer poker chips do
not automatically become an external game's currency.

`close()` refuses an unfinished hand. The example deliberately has no
mid-hand leave/cancellation, reconnect or abandonment policy. Finish the
hand before leaving. A real game's host must define those behaviors before
offering them to players; a process exit is not a safe payout mechanism.

## Existing desktop and P2P sessions

`SessionPlayer(session, *, start_table=None)` in the same public facade
adapts an existing P2P `Session` using `view()` and
`command(command, payload=None)`. It retains the desktop snapshot/command
format and the existing cryptographic session lifecycle. It does not
convert `PokerTable` into that protocol. A host still composes transport,
clock, secret source, authenticated admission and cryptographic resources;
see [`SESSION_DEPENDENCIES.md`](SESSION_DEPENDENCIES.md),
[`CORE_IDENTITY.md`](CORE_IDENTITY.md), and
[`MULTIPLAYER.md`](MULTIPLAYER.md).

The base wheel needs no third-party runtime dependency for `PokerTable`.
The optional `[p2p]` extra installs Python cryptography dependencies, while
the mental-poker path also needs a compatible libsodium native runtime.
The tavern demo opens no sockets or router mappings, starts no services,
and makes no network fairness or different-network play claim.
