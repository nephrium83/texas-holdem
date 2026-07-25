class_name NextHandControl
extends PanelContainer

## Prompts to advance between hands per GODOT_PROTOCOL.md's continuous-
## session lifecycle (section 5): the client sends next_hand after a
## hand settles or voids. Driven by turn.state -- the same
## authoritative field player_info_panel renders -- not by you.legal:
## advancing to the next hand is not a betting decision, so it has its
## own trigger independent of BettingControls.

signal next_hand_pressed

@onready var _button: Button = %NextHandButton
@onready var _message_label: Label = %MessageLabel


func _ready() -> void:
	_button.pressed.connect(_on_button_pressed)


## state is turn.state from a snapshot (section 5's turn-state table).
## Only "hand_complete" and "voided" leave a next_hand command to send;
## "eliminated" and "match_complete" are terminal-for-this-seat
## spectator states shown without a control, and every mid-hand state
## (your_turn, waiting, dealing, lobby, ...) hides this entirely.
func apply_turn_state(state: String) -> void:
	match state:
		"hand_complete":
			visible = true
			_button.visible = true
			_button.disabled = false
			_message_label.text = "Hand complete"
		"voided":
			visible = true
			_button.visible = true
			_button.disabled = false
			_message_label.text = "Hand voided"
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


func _on_button_pressed() -> void:
	next_hand_pressed.emit()
