extends GutTest

## The control that starts a table.
##
## This exists because the sidecar's start_game command had no caller in
## this client at all: the Python side implemented and tested it end to end
## over a real socket, and a person launching the Godot client still sat in
## the lobby forever. These tests are the product edge, not the protocol.

const LobbyControlScene := preload("res://ui/lobby_control.tscn")


func _control() -> LobbyControl:
	var control: LobbyControl = LobbyControlScene.instantiate()
	add_child_autofree(control)
	watch_signals(control)
	return control


func test_lobby_state_shows_an_enabled_start_button():
	var control := _control()
	control.apply_turn_state("lobby")
	assert_true(control.visible)
	assert_true(control.get_node("%StartGameButton").visible)
	assert_false(control.get_node("%StartGameButton").disabled)
	assert_eq(control.get_node("%LobbyMessageLabel").text, "Ready to start")


func test_every_in_hand_state_hides_the_control():
	## Starting is meaningless once a hand is underway -- the sidecar would
	## answer "already_started" -- so offer nothing rather than a no-op.
	var control := _control()
	for state in [
		"your_turn", "waiting", "folded_waiting", "all_in_waiting",
		"resolving", "dealing", "hand_complete", "voided", "eliminated",
		"match_complete",
	]:
		control.apply_turn_state(state)
		assert_false(control.visible, "expected hidden for state: %s" % state)


func test_pressing_start_emits_start_game_pressed():
	## THE edge. If this stops holding, the client cannot start a table no
	## matter how completely the protocol supports it.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	assert_signal_emitted(control, "start_game_pressed")


func test_a_second_press_is_ignored_while_the_first_is_in_flight():
	## Starting runs an entire mental-poker deal inside one round-trip --
	## about a second at three seats -- and snapshots keep arriving during
	## that window. Without the latch each one would re-enable the button.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.get_node("%StartGameButton").pressed.emit()
	assert_signal_emit_count(control, "start_game_pressed", 1)


func test_a_lobby_snapshot_arriving_mid_start_does_not_re_enable_the_button():
	## Narrow claim: snapshots alone must not clear the latch. Only an
	## explicit start_failed() does -- see the wedge tests below, which
	## exist because this assertion on its own cannot tell "hold the latch
	## during a start" from "hold it forever after a failed one".
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.apply_turn_state("lobby")          # another lobby snapshot
	assert_true(control.get_node("%StartGameButton").disabled)
	assert_eq(control.get_node("%LobbyMessageLabel").text, "Starting...")


func test_a_failed_start_releases_the_button_instead_of_wedging_it():
	## Review found the panel wedged forever on any failed start, because
	## the latch cleared only when turn.state LEFT the lobby -- and every
	## failure (refused, hand_failed, socket down) leaves the client IN the
	## lobby. The user saw a disabled button reading "Starting..." with no
	## way back. The test above asserted that state as correct.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()

	control.start_failed("Table refused")

	assert_false(control.get_node("%StartGameButton").disabled,
		"the start button stayed disabled after a failed start")
	assert_eq(control.get_node("%LobbyMessageLabel").text, "Table refused")


func test_the_button_works_again_after_a_failed_start():
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.start_failed("Table refused")
	control.get_node("%StartGameButton").pressed.emit()
	assert_signal_emit_count(control, "start_game_pressed", 2)


func test_a_failure_message_survives_later_lobby_snapshots():
	## Snapshots keep arriving after the failure. The panel must keep
	## saying what went wrong rather than reverting to "Ready to start"
	## and losing the only feedback the user gets.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.start_failed("First hand failed")
	control.apply_turn_state("lobby")
	assert_eq(control.get_node("%LobbyMessageLabel").text, "First hand failed")
	assert_false(control.get_node("%StartGameButton").disabled)


func test_a_retry_clears_the_previous_failure():
	## A real retry always goes through the button, and pressing clears the
	## failure itself. This test used to jump straight from start_failed()
	## to "dealing" with no second press -- a sequence the client cannot
	## actually produce, and one that collides with the hand_failed case,
	## where "dealing" with a failure still latched is precisely the state
	## that must keep showing it. Failing that way is what surfaced this.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.start_failed("Table refused")

	control.get_node("%StartGameButton").pressed.emit()   # try again

	control.apply_turn_state("dealing")                   # and it took
	# Asserted HERE, on the one transition that tells the two cases apart.
	# Stepping straight on to your_turn hides it: that state clears the
	# failure regardless, so the test passed even with the press's own
	# clear removed -- a healthy retry would have shown a stale failure
	# with the button hidden for the whole dealing window.
	assert_false(control.visible,
		"a healthy retry showed a stale failure while dealing")

	control.apply_turn_state("your_turn")
	assert_false(control.visible)
	control.apply_turn_state("lobby")
	assert_eq(control.get_node("%LobbyMessageLabel").text, "Ready to start")


func test_returning_to_lobby_after_a_hand_offers_a_working_button_again():
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.apply_turn_state("dealing")        # the hand began
	control.apply_turn_state("lobby")          # ...and we are back
	assert_false(control.get_node("%StartGameButton").disabled)
	assert_eq(control.get_node("%LobbyMessageLabel").text, "Ready to start")


func test_a_failure_survives_a_snapshot_that_left_the_lobby():
	## The hand_failed shape: the table went live and its hand did not, so
	## the client leaves the lobby and never comes back. Hiding on the next
	## snapshot wiped the only explanation the user gets and left them on a
	## table stuck at "Dealing" with no controls and no reason.
	var control := _control()
	control.apply_turn_state("lobby")
	control.get_node("%StartGameButton").pressed.emit()
	control.start_failed("First hand failed")

	control.apply_turn_state("dealing")

	assert_true(control.visible, "the failure message was hidden away")
	assert_eq(control.get_node("%LobbyMessageLabel").text, "First hand failed")
	assert_false(control.get_node("%StartGameButton").visible,
		"a dead table still offered a start button")


func test_a_genuinely_live_hand_clears_a_failure():
	## The other side of it: if the table really is playing, the panel must
	## get out of the way regardless of what an earlier attempt reported.
	var control := _control()

	for state in [
		"your_turn", "waiting", "folded_waiting", "all_in_waiting",
		"resolving", "hand_complete", "voided", "eliminated",
		"match_complete",
	]:
		control.apply_turn_state("lobby")
		control.get_node("%StartGameButton").pressed.emit()
		control.start_failed("First hand failed")
		control.apply_turn_state(state)
		assert_false(control.visible,
			"stayed visible over a live hand in state: %s" % state)
