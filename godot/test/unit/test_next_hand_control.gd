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


func test_table_closed_shows_message_with_no_button():
	var control := _control()
	control.apply_turn_state("table_closed")
	assert_true(control.visible)
	assert_false(control.get_node("%NextHandButton").visible)
	assert_eq(control.get_node("%MessageLabel").text, "Table closed")


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


# ------------------------------------------------------------- the latch
# Every human presses Next Hand. One who presses first waits for the rest
# while snapshots of the same settled hand keep arriving.

func test_a_press_latches_the_button_and_says_it_is_waiting():
	var control := _control()
	control.apply_turn_state("hand_complete", 3)
	control.get_node("%NextHandButton").pressed.emit()
	assert_true(control.get_node("%NextHandButton").disabled)
	assert_eq(control.get_node("%MessageLabel").text, "Waiting for other players")


func test_snapshots_of_the_same_hand_keep_the_latch():
	var control := _control()
	control.apply_turn_state("hand_complete", 3)
	control.get_node("%NextHandButton").pressed.emit()
	control.apply_turn_state("hand_complete", 3)
	control.get_node("%NextHandButton").pressed.emit()
	assert_true(control.get_node("%NextHandButton").disabled,
		"a snapshot re-enabled a latched Next Hand")
	assert_eq(control.get_node("%MessageLabel").text, "Waiting for other players")
	assert_signal_emit_count(control, "next_hand_pressed", 1)


func test_leaving_the_settled_state_clears_the_latch():
	var control := _control()
	control.apply_turn_state("hand_complete", 3)
	control.get_node("%NextHandButton").pressed.emit()
	control.apply_turn_state("dealing", 4)
	control.apply_turn_state("hand_complete", 4)
	assert_false(control.get_node("%NextHandButton").disabled)
	assert_eq(control.get_node("%MessageLabel").text, "Hand complete")


func test_a_redeal_that_voids_again_offers_the_button_again():
	## No other state is seen between the two voids, so only the new hand
	## number says the table moved on.
	var control := _control()
	control.apply_turn_state("voided", 3)
	control.get_node("%NextHandButton").pressed.emit()
	control.apply_turn_state("voided", 4)
	assert_false(control.get_node("%NextHandButton").disabled)
	assert_eq(control.get_node("%MessageLabel").text, "Hand voided")


func test_release_after_a_refusal_lets_the_player_press_again():
	var control := _control()
	control.apply_turn_state("hand_complete", 3)
	control.get_node("%NextHandButton").pressed.emit()
	control.release()
	assert_false(control.get_node("%NextHandButton").disabled)
	assert_eq(control.get_node("%MessageLabel").text, "Hand complete")
	control.get_node("%NextHandButton").pressed.emit()
	assert_signal_emit_count(control, "next_hand_pressed", 2)
