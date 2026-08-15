class_name FakeSidecar
extends RefCounted

## Duck-typed stand-in for SidecarClient's outgoing-command surface.
## Lets Main's command wiring be tested without a live socket: swap
## this in for main._sidecar after the scene's real _ready() has run.

var calls: Array = []


## Mirrors SidecarClient.start_game's bool return. Set send_succeeds to
## false to stand in for a client whose socket is down.
var send_succeeds := true


func start_game() -> bool:
	calls.append(["start_game"])
	return send_succeeds


func fold() -> void:
	calls.append(["fold"])


func check_call() -> void:
	calls.append(["check_call"])


func raise_to(amount: int) -> void:
	calls.append(["raise_to", amount])


func next_hand() -> void:
	calls.append(["next_hand"])
