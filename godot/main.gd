class_name Main
extends Control

## Wires the Godot client together: forwards every snapshot
## (GODOT_PROTOCOL.md section 5) to the table, the player-info panel,
## the betting controls, and the next-hand control, and forwards their
## player commands (section 4) to the sidecar. Holds no game logic
## itself -- it is pure plumbing between already-built,
## independently-tested pieces (#4, #5, #6, #7).

@onready var _table_view: TableView = %TableView
@onready var _betting_controls: BettingControls = %BettingControls
@onready var _player_info_panel: PlayerInfoPanel = %PlayerInfoPanel
@onready var _next_hand_control: NextHandControl = %NextHandControl
@onready var _lobby_control: LobbyControl = %LobbyControl

## Untyped on purpose: production wiring points this at the real
## %SidecarClient child, but tests substitute a lightweight fake --
## anything duck-typing fold()/check_call()/raise_to()/next_hand() --
## to verify outgoing commands without a live socket.
var _sidecar


func _ready() -> void:
	_sidecar = %SidecarClient
	_sidecar.snapshot_received.connect(_on_snapshot_received)
	_betting_controls.fold_pressed.connect(_on_fold_pressed)
	_betting_controls.check_call_pressed.connect(_on_check_call_pressed)
	_betting_controls.raise_pressed.connect(_on_raise_pressed)
	_next_hand_control.next_hand_pressed.connect(_on_next_hand_pressed)
	_connect_to_sidecar_from_cmdline()


func _on_snapshot_received(snapshot: Dictionary) -> void:
	_table_view.apply_snapshot(snapshot)
	_player_info_panel.apply_snapshot(snapshot)
	var you: Dictionary = snapshot.get("you", {})
	_betting_controls.apply_legal(you.get("legal", {}))
	var turn: Dictionary = snapshot.get("turn", {})
	var turn_state := str(turn.get("state", "lobby"))
	_next_hand_control.apply_turn_state(turn_state)
	_lobby_control.apply_turn_state(turn_state)


func _on_fold_pressed() -> void:
	_sidecar.fold()


func _on_check_call_pressed() -> void:
	_sidecar.check_call()


func _on_raise_pressed(amount: int) -> void:
	_sidecar.raise_to(amount)


func _on_next_hand_pressed() -> void:
	_sidecar.next_hand()



## The sidecar's listening port is OS-assigned (client_server.py binds
## port 0) and must be handed to this process at launch; there is no
## fixed default to fall back to. GODOT_PROTOCOL.md section 7 doesn't
## yet specify that hand-off mechanism -- no launcher exists yet either
## (noted in #4) -- so this uses a --sidecar-port= command-line argument
## as a placeholder convention until a real one is designed.
##
## Uses get_cmdline_user_args(), NOT get_cmdline_args(): the latter
## returns only engine-recognized arguments and is empty for a run like
## `godot --path godot -- --sidecar-port=1234` -- verified live, since
## this is exactly the invocation a person actually launching the
## client would use, and it silently connected to nothing until this
## was caught by an actual end-to-end run rather than a unit test (no
## live socket existed to test this line against before now).
func _connect_to_sidecar_from_cmdline() -> void:
	var prefix := "--sidecar-port="
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with(prefix):
			var port := int(arg.substr(prefix.length()))
			_sidecar.connect_to_sidecar("127.0.0.1", port)
			return
