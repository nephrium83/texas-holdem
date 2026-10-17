class_name SeatView
extends PanelContainer

## Renders one entry of snapshot.seats[i] (GODOT_PROTOCOL.md section 5).
## Public only -- this component is never given a seat's hole cards
## except its own (is_you) or at a contested showdown, matching what the
## snapshot itself withholds. It does not decide that; it only ever
## displays what apply_seat() is handed.

@onready var _name_label: Label = %NameLabel
@onready var _pos_badge: Label = %PosBadge
@onready var _stack_label: Label = %StackLabel
@onready var _bet_label: Label = %BetLabel
@onready var _status_label: Label = %StatusLabel
@onready var _turn_indicator: Label = %TurnIndicator
@onready var _card_a: CardView = %CardA
@onready var _card_b: CardView = %CardB


func apply_seat(seat: Dictionary, is_action_on: bool) -> void:
	if not bool(seat.get("in_seat", false)):
		_apply_empty()
		return

	var is_you := bool(seat.get("is_you", false))
	var name := str(seat.get("name", "Seat"))
	_name_label.text = "%s (You)" % name if is_you else name

	_stack_label.text = str(int(seat.get("stack", 0)))

	var bet := int(seat.get("bet", 0))
	_bet_label.text = ("Bet %d" % bet) if bet > 0 else ""

	var pos: Variant = seat.get("pos")
	_pos_badge.text = str(pos) if pos != null else ""

	_status_label.text = _status_text(seat)
	_turn_indicator.text = "ACTING" if is_action_on else ""
	_apply_cards(seat)


## A lobby seat is table membership, not a dealt hand: the lobby snapshot
## carries only seat, name and is_you (GODOT_PROTOCOL.md section 5), and no
## in_seat -- which apply_seat() reads as "nobody here". So a named seat is
## shown as taken, with no stack, position or cards, because none exist yet.
func apply_lobby_seat(seat: Dictionary) -> void:
	_apply_empty()
	var name := str(seat.get("name", ""))
	if name.is_empty():
		return
	_name_label.text = "%s (You)" % name if bool(seat.get("is_you", false)) else name


func _apply_empty() -> void:
	_name_label.text = "Empty seat"
	_stack_label.text = ""
	_bet_label.text = ""
	_pos_badge.text = ""
	_status_label.text = ""
	_turn_indicator.text = ""
	_card_a.clear()
	_card_b.clear()


func _status_text(seat: Dictionary) -> String:
	if bool(seat.get("folded", false)):
		return "Folded"
	if bool(seat.get("all_in", false)):
		return "All in"
	if bool(seat.get("sitting_out", false)):
		return "Sitting out"
	return str(seat.get("last_action", ""))


func _apply_cards(seat: Dictionary) -> void:
	# Folded hands are mucked face down at a real table; this MVP simply
	# hides them rather than showing a face-down muck, a display choice
	# that can be revisited without changing what data the seat carries.
	if bool(seat.get("folded", false)):
		_card_a.clear()
		_card_b.clear()
		return
	var hole: Variant = seat.get("hole")
	if hole is Array and hole.size() >= 2:
		_card_a.set_card(str(hole[0]))
		_card_b.set_card(str(hole[1]))
	else:
		_card_a.set_face_down()
		_card_b.set_face_down()
