extends GutTest

const NextHandControlScene := preload("res://ui/next_hand_control.tscn")


func _control() -> NextHandControl:
	var control: NextHandControl = NextHandControlScene.instantiate()
	add_child_autofree(control)
	watch_signals(control)
	return control


func test_hand_complete_shows_enabled_button():
	var control := _control()
	control.apply_turn_state("hand_complete")
	assert_true(control.visible)
	assert_true(control.get_node("%NextHandButton").visible)
	assert_false(control.get_node("%NextHandButton").disabled)
	assert_eq(control.get_node("%MessageLabel").text, "Hand complete")


func test_voided_shows_enabled_button():
	var control := _control()
	control.apply_turn_state("voided")
	assert_true(control.visible)
	assert_true(control.get_node("%NextHandButton").visible)
	assert_eq(control.get_node("%MessageLabel").text, "Hand voided")


func test_eliminated_shows_message_with_no_button():
	var control := _control()
	control.apply_turn_state("eliminated")
	assert_true(control.visible)
	assert_false(control.get_node("%NextHandButton").visible)
	assert_eq(control.get_node("%MessageLabel").text, "Eliminated -- spectating")


func test_match_complete_shows_message_with_no_button():
	var control := _control()
	control.apply_turn_state("match_complete")
	assert_true(control.visible)
	assert_false(control.get_node("%NextHandButton").visible)
	assert_eq(control.get_node("%MessageLabel").text, "Match complete")


func test_mid_hand_states_hide_entirely():
	var control := _control()
	for state in ["your_turn", "waiting", "folded_waiting", "all_in_waiting", "resolving", "dealing", "lobby"]:
		control.apply_turn_state(state)
		assert_false(control.visible, "expected hidden for state: %s" % state)


func test_button_press_emits_next_hand_pressed():
	var control := _control()
	control.apply_turn_state("hand_complete")
	control.get_node("%NextHandButton").pressed.emit()
	assert_signal_emitted(control, "next_hand_pressed")
