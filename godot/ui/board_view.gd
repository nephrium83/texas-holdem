class_name BoardView
extends HBoxContainer

## Renders snapshot.board (GODOT_PROTOCOL.md section 5): 0 to 5 community
## cards. Slots beyond the current board length are cleared, not shown
## face down -- an unrevealed street's cards don't exist yet as far as
## any player (including this client) has been told.

@onready var _slots: Array[CardView] = [%Card0, %Card1, %Card2, %Card3, %Card4]


func apply_board(board: Array) -> void:
	for i in range(_slots.size()):
		if i < board.size():
			_slots[i].set_card(str(board[i]))
		else:
			_slots[i].clear()
