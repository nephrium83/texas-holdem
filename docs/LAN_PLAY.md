# Playing together on your local network

Everyone needs this version of Poker Preview on a Windows computer connected
to the same local network. These rooms use play chips.

1. Open Poker Preview, enter your name and choose **Host LAN game**.
2. Copy the room's invite and send it to your friends.
3. Friends open Poker Preview, enter their names, paste the whole invite and
   choose **Join LAN game**.
4. Once everyone appears in the lobby, each guest selects **Ready**.
5. The host selects **Start game** when all players are ready. A room needs
   two to nine players. The host can also switch their own Ready state.
6. After a hand, select **Next hand** or wait for its 30-second countdown.
   If another computer is still confirming the result, the game waits for it.

Use **Leave table** to return to the menu, or **Back to menu** if the table has
already closed. You can immediately create or join
another room in the same application. A non-host leaving during play applies
the casual departure rule and lets survivors continue once their results agree.
Losing the host closes the table. This version does not reconnect players or
transfer a running game to a replacement host. Turns also show a 30-second
clock and automatically check or fold. All players need this compatible
preview and clocks within 20 seconds of each other before starting.
See `LAN_RELIABILITY.md` for timing and payout details.

If Windows asks whether Poker may use the network, allow it on your private
network. A guest network that isolates devices can prevent friends from joining.
An invite carries the host's LAN address, so no separate address field is needed.
If the host's network address changes, create a new room and share its new invite.

Invites grant access to a room. Share them only with the intended players.
An older V1 invite or an invite without a LAN address is refused. This delivery
does not use an internet relay or support joining from outside the local network.

## Implementation boundary

`holdem.lan_session.LanTable` owns one authenticated production Session per
sidecar process. It uses the existing signed transport, full-key V2 host pin,
admission proof and Bayer–Groth deal. It adds no alternate betting engine or
trusted plaintext dealing path. The rendering connection remains on localhost.

`lobby_state()` returns room metadata and public membership. An `on_change`
callback reports that state is dirty and may run on a background thread; callers
read snapshots on their rendering thread. Lobby membership comes from the
Session's public player list, before its playing seat order is frozen.

Each real peer needs its own process and config directory when testing multiple
players on one computer. Reusing a config directory reuses its persisted signing
identity and does not represent independent players.

## Acceptance evidence

Nine process and UI acceptance cases passed in
`tests/test_lan_integration.py`. They cover independent two- and three-player
tables playing three consistent hands, private-card separation, lobby Ready
and leave/rehost behavior, visible connection loss, roster updates, and the
actual Godot Host/Join controls. A late join to a running room is visibly
refused without disturbing the existing game. Local practice also passed through the
protocol and through Godot, including ten complete bot hands in the UI case.

On 2026-10-20, the user-selected **Windows VM on Unraid** hosted the packaged
application while BWING joined it over the LAN. Both real Godot clients
selected the lobby and betting controls, completed three hands, agreed on
every settled stack, and checked own-card visibility and opponent-card
concealment. Neither computer needed an external Python or Godot setup.
All 93 packaged files were hash-verified on the VM. Both applications and
their Godot children exited normally, the localhost listener closed, and
the temporary firewall rule (restricted to BWING and the tested executable)
was removed. The VM used the headless renderer for this automated check;
this does not establish its interactive graphics-driver compatibility.

The runtime source for that package is `261d3a2`; final delivery results and
artifact hashes are recorded in PR #65 and the continuing delivery handoff.
