class_name NextHandControl
extends PanelContainer

## Prompts to advance between hands per GODOT_PROTOCOL.md's continuous-
## session lifecycle (section 5): the client sends next_hand after a
## hand settles or voids. Driven by turn.state -- the same
## authoritative field player_info_panel renders -- not by you.legal:
## advancing to the next hand is not a betting decision, so it has its
## own trigger independent of BettingControls.

signal next_hand_pressed

const WAITING_TEXT := "Waiting for other players"
const _ADVANCEABLE_STATES := ["hand_complete", "voided"]

@onready var _button: Button = %NextHandButton
@onready var _message_label: Label = %MessageLabel

## Latches the press until the table moves on. Every human at the table
## presses Next Hand, so one who presses first waits for the rest -- and
## each snapshot arriving meanwhile would otherwise re-enable the button.
var _requested := false
## The hand the latch was set on. A redeal that voids again before any other
## state is seen is still a new hand, and must offer the button again.
var _requested_hand := 0
var _state := ""
var _hand_num := 0


func _ready() -> void:
	_button.pressed.connect(_on_button_pressed)


## state is turn.state from a snapshot (section 5's turn-state table), and
## hand_num the snapshot's hand_num. Only "hand_complete" and "voided" leave
## a next_hand command to send; "eliminated" and "match_complete" are
## terminal-for-this-seat spectator states shown without a control, and
## every mid-hand state (your_turn, waiting, dealing, lobby, ...) hides this
## entirely. Leaving the settled state, or a new hand, clears the latch.
func apply_turn_state(state: String, hand_num: int = 0) -> void:
	if _requested and (
		state not in _ADVANCEABLE_STATES or hand_num != _requested_hand
	):
		_requested = false
	_state = state
	_hand_num = hand_num
	match state:
		"hand_complete":
			_show_advance("Hand complete")
		"voided":
			_show_advance("Hand voided")
		"eliminated":
			visible = true
			_button.visible = false
			_message_label.text = "Eliminated -- spectating"
		"match_complete":
			visible = true
			_button.visible = false
			_message_label.text = "Match complete"
		_:
			visible = false


## Release the latch after a next_hand that did not take (the sidecar said
## not_ready), so the player is not left on a disabled button.
func release() -> void:
	_requested = false
	apply_turn_state(_state, _hand_num)


func _show_advance(message: String) -> void:
	visible = true
	_button.visible = true
	_button.disabled = _requested
	_message_label.text = WAITING_TEXT if _requested else message


func _on_button_pressed() -> void:
	if _requested:
		return
	_requested = true
	_requested_hand = _hand_num
	_button.disabled = true
	_message_label.text = WAITING_TEXT
	next_hand_pressed.emit()
