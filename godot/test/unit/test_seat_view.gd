extends GutTest

const SeatViewScene := preload("res://ui/seat_view.tscn")


func _seat() -> SeatView:
	var seat: SeatView = SeatViewScene.instantiate()
	add_child_autofree(seat)
	return seat


func test_empty_seat_shows_placeholder_and_no_cards():
	var view := _seat()
	view.apply_seat({}, false)
	assert_eq(view.get_node("%NameLabel").text, "Empty seat")
	assert_false(view.get_node("%CardA").visible)
	assert_false(view.get_node("%CardB").visible)


func test_occupied_seat_shows_name_stack_and_position():
	var view := _seat()
	view.apply_seat({
		"seat": 0, "name": "Ada", "stack": 455, "bet": 0,
		"folded": false, "all_in": false, "in_seat": true,
		"sitting_out": false, "last_action": "", "pos": "BTN", "is_you": false,
	}, false)
	assert_eq(view.get_node("%NameLabel").text, "Ada")
	assert_eq(view.get_node("%StackLabel").text, "455")
	assert_eq(view.get_node("%PosBadge").text, "BTN")
	assert_eq(view.get_node("%BetLabel").text, "")


func test_is_you_appends_suffix_to_name():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ada", "is_you": true}, false)
	assert_eq(view.get_node("%NameLabel").text, "Ada (You)")


func test_positive_bet_is_shown():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ben", "bet": 20}, false)
	assert_eq(view.get_node("%BetLabel").text, "Bet 20")


func test_action_on_shows_turn_indicator():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ada"}, true)
	assert_eq(view.get_node("%TurnIndicator").text, "ACTING")


func test_not_action_on_hides_turn_indicator():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ada"}, false)
	assert_eq(view.get_node("%TurnIndicator").text, "")


func test_folded_status_takes_priority_and_hides_cards():
	var view := _seat()
	view.apply_seat({
		"in_seat": true, "name": "Cy", "folded": true, "all_in": true,
	}, false)
	assert_eq(view.get_node("%StatusLabel").text, "Folded")
	assert_false(view.get_node("%CardA").visible)
	assert_false(view.get_node("%CardB").visible)


func test_all_in_status_when_not_folded():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Cy", "all_in": true}, false)
	assert_eq(view.get_node("%StatusLabel").text, "All in")


func test_sitting_out_status():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Cy", "sitting_out": true}, false)
	assert_eq(view.get_node("%StatusLabel").text, "Sitting out")


func test_last_action_shown_when_no_other_status_applies():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Cy", "last_action": "CALL 20"}, false)
	assert_eq(view.get_node("%StatusLabel").text, "CALL 20")


func test_in_seat_without_hole_shows_face_down_cards():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ada"}, false)
	assert_true(view.get_node("%CardA").visible)
	assert_false(view.get_node("%CardA/CardLabel").visible)


func test_showdown_hole_reveals_actual_cards():
	var view := _seat()
	view.apply_seat({"in_seat": true, "name": "Ada", "hole": ["Ah", "Kd"]}, false)
	assert_eq(view.get_node("%CardA/CardLabel").text, "A♥")
	assert_eq(view.get_node("%CardB/CardLabel").text, "K♦")
