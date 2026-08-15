class_name LobbyControl
extends PanelContainer

## Starts the table. This is the product edge that was missing: the sidecar
## has exposed the `start_game` command (GODOT_PROTOCOL.md section 4) and
## nothing in this client ever sent it, so a person launching the client sat
## in the lobby forever while the protocol, the Python tests and the
## real-socket integration test all passed. A capability nothing invokes is
## indistinguishable from one that does not exist -- which is the same
## defect, one layer up, that made the whole mental-poker deal unreachable.
##
## Driven by turn.state, like NextHandControl: `lobby` is the only state in
## which starting is meaningful. Once a hand is underway the sidecar answers
## `already_started`, so this hides rather than offering a no-op.

signal start_game_pressed

@onready var _button: Button = %StartGameButton
@onready var _message_label: Label = %LobbyMessageLabel

## Latches the request so a second press cannot fire while the first is in
## flight. Starting a table runs a whole mental-poker deal inside one
## round-trip -- roughly a second at three seats, five at nine -- and every
## snapshot arriving during that window would otherwise re-enable the button.
var _requested := false


func _ready() -> void:
	_button.pressed.connect(_on_button_pressed)


func apply_turn_state(state: String) -> void:
	visible = (state == "lobby")
	if not visible:
		# A hand is running (or the session ended). Clear the latch so a
		# later return to lobby offers a working button again.
		_requested = false
		return
	_button.disabled = _requested
	_message_label.text = "Starting..." if _requested else "Ready to start"


func _on_button_pressed() -> void:
	if _requested:
		return
	_requested = true
	_button.disabled = true
	_message_label.text = "Starting..."
	start_game_pressed.emit()
