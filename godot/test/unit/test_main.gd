extends GutTest

const MainScene := preload("res://main.tscn")
const FakeSidecarScript := preload("res://test/fixtures/fake_sidecar.gd")


func _main() -> Main:
	var main: Main = MainScene.instantiate()
	add_child_autofree(main)
	return main


func _heads_up_snapshot() -> Dictionary:
	return {
		"hand_num": 7,
		"pot": 90,
		"board": ["2c", "7d", "10h"],
		"action_on": 0,
		"seats": [
			{
				"seat": 0, "name": "Ada", "stack": 455, "bet": 0,
				"folded": false, "all_in": false, "in_seat": true,
				"sitting_out": false, "last_action": "", "pos": "SB", "is_you": true,
			},
			{
				"seat": 1, "name": "Ben", "stack": 480, "bet": 20,
				"folded": false, "all_in": false, "in_seat": true,
				"sitting_out": false, "last_action": "RAISE 20", "pos": "BB", "is_you": false,
			},
		],
		"turn": {
			"state": "your_turn", "headline": "Your turn | Flop | 20 to call",
			"street_label": "Flop", "pot": 90,
		},
		"you": {
			"legal": {
				"to_call": 20, "can_check": false, "can_raise": true,
				"min_to": 40, "max_to": 455, "pot": 90,
			},
		},
	}


func test_snapshot_fans_out_to_table_view():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	assert_eq(main.get_node("%TableView/Seat0/Content/NameLabel").text, "Ada (You)")
	assert_eq(main.get_node("%TableView/PotLabel").text, "Pot: 90")


func test_snapshot_fans_out_to_player_info_panel():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	assert_eq(main.get_node("%PlayerInfoPanel/Margin/Content/StatusLabel").text, "Your turn | Flop | 20 to call")


func test_snapshot_fans_out_legal_to_betting_controls():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	var controls := main.get_node("%BettingControls")
	assert_true(controls.visible)
	assert_eq(
		main.get_node("%BettingControls/Margin/Content/ActionRow/CheckCallButton").text,
		"Call 20"
	)


func test_absent_legal_hides_betting_controls():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["you"] = {}
	main._on_snapshot_received(snapshot)
	assert_false(main.get_node("%BettingControls").visible)


func test_fold_pressed_calls_sidecar_fold():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	main.get_node("%BettingControls").fold_pressed.emit()
	assert_eq(fake.calls, [["fold"]])


func test_check_call_pressed_calls_sidecar_check_call():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	main.get_node("%BettingControls").check_call_pressed.emit()
	assert_eq(fake.calls, [["check_call"]])


func test_raise_pressed_calls_sidecar_raise_to_with_amount():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	main.get_node("%BettingControls").raise_pressed.emit(120)
	assert_eq(fake.calls, [["raise_to", 120]])


func test_snapshot_fans_out_turn_state_to_next_hand_control():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	assert_true(main.get_node("%NextHandControl").visible)
	assert_true(main.get_node("%NextHandControl/Margin/Content/NextHandButton").visible)


func test_mid_hand_state_hides_next_hand_control():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())  # turn.state == "your_turn"
	assert_false(main.get_node("%NextHandControl").visible)


func test_next_hand_pressed_calls_sidecar_next_hand():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	main.get_node("%NextHandControl").next_hand_pressed.emit()
	assert_eq(fake.calls, [["next_hand"]])


# ---------------------------------------------------------------- lobby start
# The edge that was missing entirely: Main must route the lobby control's
# press to the sidecar. Everything below the socket was already proven by
# the Python suite; nothing proved the client could invoke it.

func test_lobby_state_shows_the_lobby_control():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	assert_true(main.get_node("%LobbyControl").visible)
	assert_true(main.get_node("%LobbyControl/Margin/Content/StartGameButton").visible)


func test_mid_hand_state_hides_the_lobby_control():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())  # turn.state == "your_turn"
	assert_false(main.get_node("%LobbyControl").visible)


func test_start_game_pressed_calls_sidecar_start_game():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl").start_game_pressed.emit()
	assert_eq(fake.calls, [["start_game"]])


func test_pressing_the_real_button_reaches_the_sidecar():
	## The whole client-side chain in one assertion, driven by an actual
	## button press rather than a synthesised signal: real scene ->
	## real control -> real Button.pressed -> Main -> sidecar command.
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()
	assert_eq(fake.calls, [["start_game"]])


func test_a_failed_start_result_releases_the_lobby_control():
	var main := _main()
	# A sidecar whose send SUCCEEDS. Against the real %SidecarClient the
	# send fails (nothing is connected in a unit test), which clears the
	# latch by the dropped-send path -- and this test would then pass
	# without the verdict routing it exists to check ever running.
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()

	main._on_command_result_received({
		"type": "command_result", "command": "start_game",
		"ok": false, "verdict": "refused",
	})

	assert_false(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled,
		"a refused start left the button wedged"
	)


func test_a_successful_start_result_does_not_release_the_latch():
	## The hand is starting; the control should stay latched until the
	## snapshot moves it out of the lobby.
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()

	main._on_command_result_received({
		"type": "command_result", "command": "start_game",
		"ok": true, "verdict": "started",
	})

	assert_true(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled
	)


func test_a_send_that_never_left_the_client_releases_the_lobby_control():
	## No reply can arrive for a command the socket dropped, so nothing
	## else would ever clear the latch.
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	fake.send_succeeds = false
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)

	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()

	assert_false(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled
	)


func test_a_command_result_for_another_command_is_ignored():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()

	main._on_command_result_received({
		"type": "command_result", "command": "fold",
		"ok": false, "verdict": "rejected",
	})

	assert_true(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled,
		"a fold result cleared the start latch"
	)
