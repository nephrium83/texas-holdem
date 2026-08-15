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

## Set when a start attempt failed, so the panel can say so instead of
## sitting on a stale "Starting..." that never resolves.
var _failure := ""

## Turn states that prove the table is genuinely playing. Anything else
## after a failed start means the table is not actually running, whatever
## its phase says.
const _LIVE_STATES := [
	"your_turn", "waiting", "folded_waiting", "all_in_waiting",
	"resolving", "hand_complete", "voided", "eliminated", "match_complete",
]


func _ready() -> void:
	_button.pressed.connect(_on_button_pressed)


func apply_turn_state(state: String) -> void:
	if state == "lobby":
		visible = true
		_button.visible = true
		_button.disabled = _requested
		if _requested:
			_message_label.text = "Starting..."
		elif _failure != "":
			_message_label.text = _failure
		else:
			_message_label.text = "Ready to start"
		return

	if _failure != "" and state not in _LIVE_STATES:
		# A start reported failure and the table left the lobby anyway:
		# that is `hand_failed` -- the table is live, its hand is not, and
		# nothing will ever void it. Keep saying so. Hiding here wiped the
		# message on the very next snapshot and left the user staring at a
		# table stuck on "Dealing" with no explanation anywhere in the UI.
		visible = true
		_button.visible = false
		_message_label.text = _failure
		return

	# A hand is genuinely running (or the session ended). Clear the latch so
	# a later return to the lobby offers a working button again.
	visible = false
	_requested = false
	_failure = ""


## Release the latch after a start that did not take.
##
## The latch used to clear ONLY on leaving the lobby -- but every failure
## leaves the client IN the lobby, so any refused start wedged the panel on
## a disabled button reading "Starting..." forever. Three ways in: the
## sidecar answering `refused`, answering `hand_failed`, or the command
## never leaving the client because the socket is down (SidecarClient
## silently drops sends while disconnected).
func start_failed(reason: String = "") -> void:
	_requested = false
	_failure = reason if reason != "" else "Could not start"
	if visible:
		_button.disabled = false
		_message_label.text = _failure


func _on_button_pressed() -> void:
	if _requested:
		return
	_requested = true
	_failure = ""
	_button.disabled = true
	_message_label.text = "Starting..."
	start_game_pressed.emit()
