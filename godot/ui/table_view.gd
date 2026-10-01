class_name TableView
extends Control

## Root of the table scene: renders a full snapshot (GODOT_PROTOCOL.md
## section 5) by handing each seats[i] entry to its SeatView, the board
## array to BoardView, and pot/hand_num to labels. Up to 9 seats per the
## table-size cap in docs/L5_SCOPE.md; slots beyond the current seat
## count are cleared rather than left showing stale data.

const MAX_SEATS := 9

@onready var _seat_views: Array[SeatView] = [
	%Seat0, %Seat1, %Seat2, %Seat3, %Seat4, %Seat5, %Seat6, %Seat7, %Seat8,
]
@onready var _board_view: BoardView = %BoardView
@onready var _pot_label: Label = %PotLabel
@onready var _hand_label: Label = %HandLabel


func apply_snapshot(snapshot: Dictionary) -> void:
	var seats: Array = snapshot.get("seats", [])
	var action_on := int(snapshot.get("action_on", -1))
	var you: Dictionary = snapshot.get("you", {})
	for i in range(MAX_SEATS):
		if i < seats.size() and seats[i] is Dictionary:
			var seat: Dictionary = seats[i]
			if bool(seat.get("is_you", false)):
				seat = _with_own_hole(seat, you.get("hole"))
			var is_action_on := int(seat.get("seat", i)) == action_on
			_seat_views[i].apply_seat(seat, is_action_on)
		else:
			_seat_views[i].apply_seat({}, false)

	_board_view.apply_board(snapshot.get("board", []))
	_pot_label.text = "Pot: %d" % int(snapshot.get("pot", 0))

	var hand_num := int(snapshot.get("hand_num", 0))
	_hand_label.text = ("Hand #%d" % hand_num) if hand_num > 0 else ""


## The sidecar sends the local seat's cards only in you.hole, never in that
## seat's public entry (GODOT_PROTOCOL.md section 5), so rendering seats[i]
## alone showed the player their own hand face down. Copied into the is_you
## seat only, on a duplicate so the snapshot itself is not altered.
func _with_own_hole(seat: Dictionary, hole: Variant) -> Dictionary:
	if not (hole is Array) or seat.has("hole"):
		return seat
	var own := seat.duplicate()
	own["hole"] = hole
	return own
