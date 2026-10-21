class_name Main
extends Control

signal hand_presentation_finished(hand_num: int)

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
@onready var _command_status: Label = %CommandStatusLabel
@onready var _connection_banner: Label = %ConnectionBanner
@onready var _connection_lobby: ConnectionLobby = %ConnectionLobby
@onready var _deal_presentation: DealPresentation = %DealPresentation

const CONNECTION_LOST_TEXT := "Connection lost — close and relaunch"

## The player's commands whose refusals are shown, with the name each is
## shown under. start_game has its own feedback in the lobby control.
const _PLAYER_COMMANDS := {
	"fold": "Fold",
	"check_call": "Check/Call",
	"raise_to": "Raise",
	"next_hand": "Next hand",
}

## Untyped on purpose: production wiring points this at the real
## %SidecarClient child, but tests substitute a lightweight fake --
## anything duck-typing fold()/check_call()/raise_to()/next_hand() --
## to verify outgoing commands without a live socket.
var _sidecar

## The snapshot a shown refusal refers to. The sidecar answers every command
## with its result and then a fresh snapshot, so the first snapshot after a
## refusal is the unchanged table it was refused on; the message stays until
## a later snapshot differs from it.
var _refused_on: Dictionary = {}
var _refusal_awaits_snapshot := false

## Set once the sidecar is gone. Nothing in this client reconnects, so the
## table on screen is frozen at its last state and no control may act on it.
var _connection_lost := false
var _has_connection_lobby := false
var _latest_snapshot: Dictionary = {}
var _presented_hand := 0
var _presentation_active := false
var _presentation_generation := 0
var _shown_presentation_stage := ""


func _ready() -> void:
	_sidecar = %SidecarClient
	_sidecar.snapshot_received.connect(_on_snapshot_received)
	_sidecar.command_result_received.connect(_on_command_result_received)
	_sidecar.disconnected_from_sidecar.connect(_on_sidecar_disconnected)
	_betting_controls.fold_pressed.connect(_on_fold_pressed)
	_betting_controls.check_call_pressed.connect(_on_check_call_pressed)
	_betting_controls.raise_pressed.connect(_on_raise_pressed)
	_next_hand_control.next_hand_pressed.connect(_on_next_hand_pressed)
	_lobby_control.start_game_pressed.connect(_on_start_game_pressed)
	_connection_lobby.command_requested.connect(_on_lobby_command_requested)
	_deal_presentation.finished.connect(_on_hand_presentation_finished)
	_connect_to_sidecar_from_cmdline()


func _on_snapshot_received(snapshot: Dictionary) -> void:
	if _connection_lost:
		return
	_expire_refusal(snapshot)
	_latest_snapshot = snapshot
	var lobby: Dictionary = snapshot.get("lobby", {})
	var phase := str(snapshot.get("phase", ""))
	var state := str(snapshot.get("turn", {}).get("state", ""))
	if str(lobby.get("mode", "")) == "menu" or phase == "lobby":
		_cancel_hand_presentation()
		_presented_hand = 0
	elif state in ["table_closed", "connection_lost", "voided", "eliminated"]:
		_cancel_hand_presentation()
	var hand_num := int(snapshot.get("hand_num", 0))
	# Desktop tables show a hand opening once, including hands started by LAN
	# readiness. A clock update must never restart the animation.
	if not lobby.is_empty() and phase in ["dealing", "betting"] and hand_num > 0 and hand_num != _presented_hand and state not in ["table_closed", "voided", "eliminated"]:
		_cancel_hand_presentation()
		_presented_hand = hand_num
		_presentation_active = true
		_play_hand_presentation.call_deferred(_presentation_generation)
	_render_snapshot(snapshot)


func _render_snapshot(snapshot: Dictionary) -> void:
	_table_view.apply_snapshot(snapshot)
	_table_view.set_deal_in_progress(_presentation_active)
	_player_info_panel.apply_snapshot(snapshot)
	var you: Dictionary = snapshot.get("you", {})
	_betting_controls.apply_legal(you.get("legal", {}))
	var turn: Dictionary = snapshot.get("turn", {})
	var turn_state := str(turn.get("state", "lobby"))
	_next_hand_control.apply_turn_state(
		turn_state, int(snapshot.get("hand_num", 0)), turn.get("clock", {}))
	_lobby_control.apply_turn_state(turn_state)
	if snapshot.get("lobby") is Dictionary:
		_has_connection_lobby = true
		_connection_lobby.apply_lobby(snapshot["lobby"])
		_lobby_control.visible = false
		# A host or joiner may leave a finished table and create another in
		# this process. Refusals from the old table must not survive that.
		if str(snapshot["lobby"].get("mode", "")) == "menu":
			_command_status.text = ""
			_betting_controls.apply_legal({})
			_next_hand_control.visible = false
	if _presentation_active:
		_apply_presentation_mask()


func is_hand_presentation_active() -> bool:
	return _presentation_active


func presentation_stage() -> String:
	if not _presentation_active:
		return "idle"
	return _deal_presentation.stage if _deal_presentation.is_playing() else "shuffling"


func _process(_delta: float) -> void:
	if _presentation_active and presentation_stage() != _shown_presentation_stage:
		_apply_presentation_mask()


func _apply_presentation_mask() -> void:
	_shown_presentation_stage = presentation_stage()
	_player_info_panel.apply_hand_presentation(_presented_hand, _shown_presentation_stage)
	_betting_controls.apply_legal({})
	_next_hand_control.visible = false
	_lobby_control.visible = false


func _play_hand_presentation(generation: int) -> void:
	# Container layout must settle before card destination centers are read.
	await get_tree().process_frame
	if not is_inside_tree() or not _presentation_active or generation != _presentation_generation:
		return
	var targets: Array[Vector2] = []
	var to_local := _deal_presentation.get_global_transform_with_canvas().affine_inverse()
	for target: Vector2 in _table_view.deal_targets(_latest_snapshot):
		targets.append(to_local * target)
	if targets.is_empty():
		_on_hand_presentation_finished()
		return
	_deal_presentation.start(_presented_hand, targets)
	_apply_presentation_mask()


func _on_hand_presentation_finished() -> void:
	if not _presentation_active or _connection_lost:
		return
	_presentation_active = false
	_render_snapshot(_latest_snapshot)
	hand_presentation_finished.emit(_presented_hand)


func _cancel_hand_presentation() -> void:
	_presentation_generation += 1
	_presentation_active = false
	_shown_presentation_stage = ""
	_deal_presentation.cancel()
	_table_view.set_deal_in_progress(false)


func _on_lobby_command_requested(command: String, payload: Dictionary) -> void:
	if _connection_lost or not _sidecar.send_command(command, payload):
		_connection_lobby.command_failed("Not connected")


func _on_fold_pressed() -> void:
	if not _connection_lost and not _presentation_active:
		_sidecar.fold()


func _on_check_call_pressed() -> void:
	if not _connection_lost and not _presentation_active:
		_sidecar.check_call()


func _on_raise_pressed(amount: int) -> void:
	if not _connection_lost and not _presentation_active:
		_sidecar.raise_to(amount)


func _on_next_hand_pressed() -> void:
	if not _connection_lost and not _presentation_active:
		_sidecar.next_hand()


## The lobby control latches on press so a second press cannot fire
## during the deal. Anything that means "no reply is coming" has to
## release it, or the panel sits on "Starting..." forever.
func _on_start_game_pressed() -> void:
	if _connection_lost or not _sidecar.start_game():
		_lobby_control.start_failed("Not connected")


func _on_command_result_received(result: Dictionary) -> void:
	var command := str(result.get("command", ""))
	var ok := bool(result.get("ok", false))
	if _has_connection_lobby:
		_connection_lobby.handle_command_result(result)
	if command == "start_game":
		if not ok:
			_lobby_control.start_failed(_start_failure_text(result))
		return
	# The Next Hand button latched on press; a refusal (not_ready) means the
	# table is not moving on, so nothing else would ever release it.
	if command == "next_hand" and not ok:
		_next_hand_control.release()
	# Without this a rejected, stale or failed action vanished: the buttons
	# simply stayed as they were and nothing said the click had not counted.
	if not ok and _PLAYER_COMMANDS.has(command):
		_command_status.text = _refusal_text(command, result)
		_refusal_awaits_snapshot = true


func _expire_refusal(snapshot: Dictionary) -> void:
	if _command_status.text.is_empty():
		return
	if _refusal_awaits_snapshot:
		_refusal_awaits_snapshot = false
		_refused_on = _without_clock(snapshot)
	elif _without_clock(snapshot) != _refused_on:
		_command_status.text = ""


func _without_clock(snapshot: Dictionary) -> Dictionary:
	var position := snapshot.duplicate(true)
	if position.get("turn") is Dictionary:
		position["turn"].erase("clock")
	return position


## Names the command and gives the sidecar's reason in its own terms
## (GODOT_PROTOCOL.md section 4 verdicts, or the error string).
func _refusal_text(command: String, result: Dictionary) -> String:
	var action: String = _PLAYER_COMMANDS[command]
	if result.has("error"):
		return "%s failed: %s" % [action, str(result["error"])]
	var verdict := str(result.get("verdict", ""))
	match verdict:
		"rejected":
			return "%s refused: not your turn, or not a legal action" % action
		"not_ready":
			return "%s refused: the hand is not finished" % action
		"stale":
			return "%s ignored: already applied" % action
		"buffered":
			return "%s queued behind an earlier action" % action
		_:
			return "%s not applied (%s)" % [action, verdict]


## The sidecar going away mid-start is the one failure that produces no
## command_result at all: the deal holds the round-trip open for about a
## second at three seats, and a crash in that window means no reply is
## ever coming. Without this the panel waits on "Starting..." forever.
##
## Mid-hand the loss was invisible: the badge kept saying "your turn", the
## betting buttons stayed live, and a click only logged a warning. There is
## no reconnect, so the client says so and leaves nothing to press.
func _on_sidecar_disconnected() -> void:
	_cancel_hand_presentation()
	_lobby_control.start_failed("Sidecar disconnected")
	_connection_lost = true
	_connection_banner.text = CONNECTION_LOST_TEXT
	_connection_banner.visible = true
	_betting_controls.apply_legal({})
	_player_info_panel.apply_connection_lost()
	_next_hand_control.visible = false
	_lobby_control.visible = false
	_connection_lobby.visible = false
	_command_status.text = ""


func _start_failure_text(result: Dictionary) -> String:
	match str(result.get("verdict", "")):
		"refused":
			return "Table refused"
		"hand_failed":
			return "First hand failed"
		"already_started":
			return "Already started"
		_:
			return "Could not start"


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
