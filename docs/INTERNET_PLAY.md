# Direct Internet play

This preview adds direct Internet hosting for casual, play-money games. Players
connect to each other; there is no hosted game server or message relay. A VPN is
not required. The existing authenticated invites, peer verification and game
protocol still apply.

## Play with friends

1. Open Poker Preview and enter your name.
2. The host selects **Host Internet game**. Hosting asks a compatible router for
   a temporary TCP port mapping and puts the router's reported public address in the
   invite.
3. The host copies the invite and sends it privately to the other players.
4. Each friend pastes that invite and selects **Join game**.
5. Everyone selects **Ready**. The host selects **Start game**.

Use **Host LAN game** for players on the same local network. The same **Join
game** button accepts either kind of invite. Practice with bots remains local.
An invite grants admission to that table; share it only with intended players.

## Connection requirements and limits

The Internet host needs a reachable public IPv4 address and a compatible router
with UPnP automatic port mapping enabled. Double NAT, carrier-grade NAT, disabled
UPnP, a restrictive firewall or an incompatible router can prevent hosting. If
hosting fails, the game shows the error and permits retrying or joining another
host. It does not silently fall back to a relay or change router-wide settings.

The mapping belongs to the hosting session. Closing attempts to remove it;
the finite lease provides an expiry fallback if cleanup cannot reach the router.
Renewal and cleanup check its random ownership marker and target before acting.
Observed entries belonging to other applications are refused or left alone.
UPnP has no atomic conditional update, so those checks cannot eliminate a
simultaneous change by another program between the check and router request.
Unexpected process or machine failure can delay cleanup until the mapping lease
expires. Keep the host's computer and game running for the match.

The requested lease lasts 300 seconds and renews during play. Routers offering
only permanent mappings are refused. Router calls use a maintained native
library with no cancellable request API: startup and Leave have bounded waits,
and at most two mapping workers can remain while native calls finish. A late
completed operation performs owned cleanup and does not renew after cancellation.

Read-only checks must establish that a port is free before creating a mapping.
Some routers return an empty reply for a missing specific entry. That reply is
never treated as proof of vacancy: the adapter instead validates a complete
mapping-table enumeration ending in the router's explicit end-of-list response.
Malformed, duplicate, incomplete or timed-out listings are refused. Enumeration
permits at most 64 entries and uses a six-second checked deadline; a read already
in progress can finish after that deadline within its remaining socket timeout.

This path does not implement ICE, TURN, TCP hole punching or a general solution
for every NAT type. UDP STUN discovery alone does not establish that a TCP game
listener is reachable from the Internet. This is a connectivity feature for the
casual game, not the future server-backed real-money product.

## Validation status

Implementation tests cover the hosting command, network-specific lobby labels,
pending-command protection, failure recovery and the existing LAN flow. Router
mapping behavior and full game behavior require their separate backend tests.

An Internet release gate remains pending until two computers on genuinely
separate networks complete ten hands with matching balances and clear disconnect
behavior. A local-network VM, a public-address lookup or a successful router
mapping alone does not satisfy that gate. Record the tested build, two-network
setup and result evidence before claiming Internet play is verified.

Live router changes during development follow the workspace owner's explicit
router-change approval rule. This is an operator constraint, not an additional
dialog in the game.

For a bounded operator trial, `tools/check_direct_host.py` requires an explicit
`--simulate-router` or `--allow-router-mapping` mode and a new `--run-dir`. The
optional `--mapping-details` flag records operation counts and an exact-port
read-only cleanup check locally. It never adds or deletes a rule independently
of the normal host adapter. A pending or failed cleanup lookup remains unknown;
closed local sockets alone do not establish that the router rule was removed.
