extends GutTest

const MainScene := preload("res://main.tscn")
const FakeSidecarScript := preload("res://test/fixtures/fake_sidecar.gd")


func _desktop_dealt_snapshot() -> Dictionary:
	var snapshot := _heads_up_snapshot()
	snapshot["phase"] = "betting"
	snapshot["you"]["hole"] = ["Ah", "Kd"]
	snapshot["lobby"] = {
		"mode": "local", "state": "playing", "players": [], "can_start": false,
	}
	return snapshot


func test_dealt_hand_is_presented_before_betting_and_reveals_latest_cards():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	main._on_snapshot_received(_desktop_dealt_snapshot())
	assert_true(main.is_hand_presentation_active())
	assert_false(main.get_node("%BettingControls").visible)
	assert_false(main.get_node("%NextHandControl").visible)
	assert_eq(main.get_node("%TableView/Seat0/Content/HoleCards").modulate.a, 0.0)
	main._on_raise_pressed(455)
	assert_eq(fake.calls, [], "a betting command escaped during the deal")
	await wait_process_frames(3)
	var deal: DealPresentation = main.get_node("%DealPresentation")
	assert_true(deal.is_playing())
	deal._process(3.0)
	assert_false(main.is_hand_presentation_active())
	assert_true(main.get_node("%BettingControls").visible)
	assert_eq(main.get_node("%TableView/Seat0/Content/HoleCards").modulate.a, 1.0)
	assert_eq(main.get_node("%TableView/Seat0/Content/HoleCards/CardA/CardLabel").text, "A♥")
	assert_eq(fake.calls, [], "revealing cards must not send an action")


func test_clock_snapshots_do_not_restart_the_same_deal():
	var main := _main()
	var snapshot := _desktop_dealt_snapshot()
	main._on_snapshot_received(snapshot)
	await wait_process_frames(3)
	var deal: DealPresentation = main.get_node("%DealPresentation")
	deal._process(0.2)
	var elapsed := deal._elapsed
	snapshot = snapshot.duplicate(true)
	snapshot["turn"]["clock"] = {"kind": "action", "remaining_seconds": 29}
	main._on_snapshot_received(snapshot)
	assert_eq(deal._elapsed, elapsed)
	assert_true(main.is_hand_presentation_active())


func test_result_arriving_during_the_deal_waits_until_the_deal_is_visible():
	var main := _main()
	var snapshot := _desktop_dealt_snapshot()
	main._on_snapshot_received(snapshot)
	await wait_process_frames(3)
	snapshot = snapshot.duplicate(true)
	snapshot["phase"] = "settled"
	snapshot["turn"] = {"state": "hand_complete", "headline": "You won 50"}
	snapshot["settlement"] = {"headline": "You won 50"}
	snapshot["you"].erase("legal")
	main._on_snapshot_received(snapshot)
	assert_false(main.get_node("%PlayerInfoPanel").result_card.visible)
	assert_false(main.get_node("%NextHandControl").visible)
	main.get_node("%DealPresentation")._process(3.0)
	assert_true(main.get_node("%PlayerInfoPanel").result_card.visible)
	assert_eq(main.get_node("%PlayerInfoPanel").status_label.text, "You won 50")
	assert_true(main.get_node("%NextHandControl").visible)
	assert_eq(main.get_node("%TableView/Seat0/Content/TurnIndicator").text, "")


func test_returning_to_menu_cancels_pending_and_running_deals():
	var main := _main()
	var menu := {
		"phase": "lobby", "hand_num": 0, "seats": [], "you": {},
		"turn": {"state": "lobby", "headline": "Choose a game"},
		"lobby": {"mode": "menu", "state": "idle"},
	}
	main._on_snapshot_received(_desktop_dealt_snapshot())
	main._on_snapshot_received(menu)
	await wait_process_frames(3)
	assert_false(main.is_hand_presentation_active())
	assert_false(main.get_node("%DealPresentation").is_playing())
	main._on_snapshot_received(_desktop_dealt_snapshot())
	await wait_process_frames(3)
	assert_true(main.get_node("%DealPresentation").is_playing())
	main._on_snapshot_received(menu)
	await wait_process_frames(3)
	assert_false(main.is_hand_presentation_active())
	assert_false(main.get_node("%DealPresentation").is_playing())
	assert_eq(main.get_node("%TableView/Seat0/Content/NameLabel").text, "Empty seat")
	assert_false(main.get_node("%BettingControls").visible)


func test_disconnection_cancels_deal_without_reenabling_actions():
	var main := _main()
	main._on_snapshot_received(_desktop_dealt_snapshot())
	await wait_process_frames(3)
	main._on_sidecar_disconnected()
	assert_false(main.is_hand_presentation_active())
	assert_false(main.get_node("%DealPresentation").is_playing())
	assert_false(main.get_node("%BettingControls").visible)
	assert_eq(main.get_node("%PlayerInfoPanel").state_badge.text, "CONNECTION LOST")


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


func test_every_control_fits_the_window_the_project_opens():
	## Godot's default 1152x648 window with stretch disabled cropped the
	## 1370x850 layout, which put Start, the betting controls and Next Hand
	## below the bottom edge: a player could not start or act at all.
	## canvas_items + keep scales the whole layout into whatever window the
	## player has instead of cropping it.
	assert_eq(ProjectSettings.get_setting("display/window/stretch/mode"), "canvas_items")
	assert_eq(ProjectSettings.get_setting("display/window/stretch/aspect"), "keep")
	var window := Rect2(0, 0,
		ProjectSettings.get_setting("display/window/size/viewport_width"),
		ProjectSettings.get_setting("display/window/size/viewport_height"))
	var main := _main()
	# The longest message each label shows.
	main.get_node("%CommandStatusLabel").text = (
		"Check/Call refused: not your turn, or not a legal action")
	main.get_node("%ConnectionBanner").text = Main.CONNECTION_LOST_TEXT
	# Measured after layout: before the first frame an autowrapping label
	# has no width yet and reports a height for wrapping at zero.
	await wait_process_frames(2)
	for path in [
		"%TableView", "%BettingControls", "%PlayerInfoPanel",
		"%NextHandControl", "%LobbyControl", "%CommandStatusLabel",
		"%ConnectionBanner",
	]:
		var control: Control = main.get_node(path)
		assert_true(window.encloses(control.get_rect()),
			"%s at %s falls outside the %s window" % [path, control.get_rect(), window.size])


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


func test_one_all_in_click_sends_one_maximum_raise_to_the_sidecar():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["you"]["legal"] = {
		"to_call": 25, "can_check": false, "can_raise": true,
		"min_to": 100, "max_to": 1025, "pot": 75, "your_bet": 25,
	}
	main._on_snapshot_received(snapshot)
	var controls := main.get_node("%BettingControls")
	controls.get_node("%PresetAllIn").pressed.emit()
	assert_eq(fake.calls, [["raise_to", 1025]],
		"All In must submit without a separate Raise click")
	assert_eq(controls.current_raise_amount(), 1025)


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


func test_clock_reaches_both_displays_without_sending_any_player_command():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	snapshot["turn"]["clock"] = {
		"kind": "next_hand", "label": "Waiting for Ada and Ben", "remaining_seconds": 0,
	}
	main._on_snapshot_received(snapshot)
	assert_eq(main.get_node("%PlayerInfoPanel").clock_label.text, "Waiting for Ada and Ben · 0s")
	assert_eq(main.get_node("%NextHandControl").get_node("%MessageLabel").text, "Hand complete · next in 0s")
	assert_eq(fake.calls, [], "the renderer made a timeout decision")


func test_countdown_controls_fit_the_preview_window():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	snapshot["turn"]["clock"] = {
		"kind": "next_hand", "label": "Next hand", "remaining_seconds": 30,
	}
	main._on_snapshot_received(snapshot)
	await wait_process_frames(2)
	var window := Rect2(0, 0, 1370, 850)
	for path in ["%NextHandControl", "%PlayerInfoPanel"]:
		var control: Control = main.get_node(path)
		assert_true(window.encloses(control.get_global_rect()), "%s overflowed with a countdown" % path)
	var next: Control = main.get_node("%NextHandControl")
	assert_true(next.get_global_rect().encloses(next.get_node("%NextHandButton").get_global_rect()))


func test_next_hand_pressed_calls_sidecar_next_hand():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	main.get_node("%NextHandControl").next_hand_pressed.emit()
	assert_eq(fake.calls, [["next_hand"]])


# ------------------------------------------------------- refused commands
# Every command is answered with a command_result and then a fresh snapshot
# (GODOT_PROTOCOL.md section 4). A refusal used to be dropped here, so a
# click that did not count looked exactly like one that did.

func _refuse(main: Main, command: String, verdict: String) -> void:
	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": command,
		"ok": false, "verdict": verdict,
	})


func test_a_rejected_action_is_shown():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	_refuse(main, "fold", "rejected")
	assert_eq(main.get_node("%CommandStatusLabel").text,
		"Fold refused: not your turn, or not a legal action")


func test_every_player_command_reports_its_refusal():
	for command in ["fold", "check_call", "raise_to", "next_hand"]:
		var main := _main()
		main._on_snapshot_received(_heads_up_snapshot())
		_refuse(main, command, "not_ready" if command == "next_hand" else "rejected")
		assert_ne(main.get_node("%CommandStatusLabel").text, "",
			"no feedback for a refused %s" % command)


func test_an_error_result_shows_the_error():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": "raise_to",
		"ok": false, "error": "invalid literal for int()",
	})
	assert_eq(main.get_node("%CommandStatusLabel").text,
		"Raise failed: invalid literal for int()")


func test_an_applied_action_shows_nothing():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": "fold",
		"ok": true, "verdict": "applied",
	})
	assert_eq(main.get_node("%CommandStatusLabel").text, "")


func test_the_refusal_survives_the_snapshot_that_answers_it():
	## The answering snapshot shows the same table; clearing on it would
	## erase the message the moment it appeared.
	var main := _main()
	var snapshot := _heads_up_snapshot()
	main._on_snapshot_received(snapshot)
	_refuse(main, "check_call", "rejected")
	main._on_snapshot_received(snapshot.duplicate(true))
	main._on_snapshot_received(snapshot.duplicate(true))
	assert_ne(main.get_node("%CommandStatusLabel").text, "")


func test_the_refusal_clears_when_the_table_moves_on():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	main._on_snapshot_received(snapshot)
	_refuse(main, "check_call", "rejected")
	main._on_snapshot_received(snapshot.duplicate(true))
	var moved := snapshot.duplicate(true)
	moved["action_on"] = 1
	moved["turn"]["state"] = "waiting"
	main._on_snapshot_received(moved)
	assert_eq(main.get_node("%CommandStatusLabel").text, "")


func test_countdown_updates_do_not_hide_a_refused_player_action():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["clock"] = {"kind": "action", "label": "Turn: Ada", "remaining_seconds": 30}
	main._on_snapshot_received(snapshot)
	_refuse(main, "check_call", "rejected")
	main._on_snapshot_received(snapshot.duplicate(true))
	snapshot["turn"]["clock"]["remaining_seconds"] = 29
	main._on_snapshot_received(snapshot)
	assert_ne(main.get_node("%CommandStatusLabel").text, "", "a timer refresh hid the refusal")


func test_a_refused_click_is_reported_until_the_table_moves_on():
	## The whole round trip as the player meets it: a real button press
	## reaches the sidecar, the sidecar refuses it and answers with the
	## unchanged table, and the message lasts until the table changes.
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	main._on_snapshot_received(snapshot)
	main.get_node("%BettingControls/Margin/Content/ActionRow/FoldButton").pressed.emit()
	assert_eq(fake.calls, [["fold"]])

	_refuse(main, "fold", "rejected")
	main._on_snapshot_received(snapshot.duplicate(true))
	assert_eq(main.get_node("%CommandStatusLabel").text,
		"Fold refused: not your turn, or not a legal action")

	var moved := snapshot.duplicate(true)
	moved["action_on"] = 1
	moved["turn"]["state"] = "waiting"
	main._on_snapshot_received(moved)
	assert_eq(main.get_node("%CommandStatusLabel").text, "")


func test_a_not_ready_next_hand_result_releases_the_latch():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	var button: Button = main.get_node("%NextHandControl/Margin/Content/NextHandButton")
	button.pressed.emit()
	assert_true(button.disabled)

	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": "next_hand",
		"ok": false, "verdict": "not_ready",
	})
	assert_false(button.disabled, "a refused next_hand left the button latched")


func test_a_started_next_hand_result_keeps_the_latch_until_the_hand_moves():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	var button: Button = main.get_node("%NextHandControl/Margin/Content/NextHandButton")
	button.pressed.emit()
	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": "next_hand",
		"ok": true, "verdict": "started",
	})
	main._on_snapshot_received(snapshot)
	assert_true(button.disabled)


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

	main.get_node("%SidecarClient").command_result_received.emit({
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

	main.get_node("%SidecarClient").command_result_received.emit({
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

	main.get_node("%SidecarClient").command_result_received.emit({
		"type": "command_result", "command": "fold",
		"ok": false, "verdict": "rejected",
	})

	assert_true(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled,
		"a fold result cleared the start latch"
	)


func test_a_closed_table_offers_no_action_buttons():
	## client_view's table_closed: the session ended abnormally, so the
	## snapshot carries no you.legal and turn.state names the closure.
	## Main has no table_closed case for betting; it follows you.legal, so
	## the hidden betting controls below rest on client_view dropping it
	## (pinned by test_a_table_closed_mid_hand_reports_the_last_settlement_
	## not_the_pot in tests/test_client_view.py).
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["you"] = {}
	snapshot["turn"]["state"] = "table_closed"
	snapshot["turn"]["headline"] = "seat 1 disconnected"
	snapshot["terminal"] = {
		"state": "ABORTED_PROTOCOL", "reason": "seat 1 disconnected",
		"last_settled_stacks": [500, 500],
	}
	main._on_snapshot_received(snapshot)
	assert_false(main.get_node("%BettingControls").visible)
	assert_false(main.get_node("%NextHandControl/Margin/Content/NextHandButton").visible)
	assert_false(main.get_node("%LobbyControl").visible)
	assert_eq(main.get_node("%PlayerInfoPanel/Margin/Content/StatusLabel").text,
		"seat 1 disconnected")


func test_a_table_closed_in_the_lobby_takes_the_start_button_away():
	## A session that ends before its first hand still sends the lobby shape,
	## and used to keep turn.state "lobby": Start stayed on offer for a table
	## that no longer existed. client_view now sends table_closed instead.
	var main := _main()
	var lobby := {
		"type": "snapshot", "phase": "lobby", "hand_num": 0,
		"seats": [
			{"seat": 0, "conn_id": "seat0", "name": "Ada", "is_you": true},
			{"seat": 1, "conn_id": "seat1", "name": "Ben", "is_you": false},
		],
		"you": {"seat": 0},
		"turn": {"state": "lobby", "headline": "Waiting for players"},
		"terminal": null,
	}
	main._on_snapshot_received(lobby)
	var start: Button = main.get_node("%LobbyControl/Margin/Content/StartGameButton")
	assert_true(start.is_visible_in_tree())

	var closed := lobby.duplicate(true)
	closed["turn"] = {"state": "table_closed", "headline": "host lost in lobby"}
	closed["terminal"] = {
		"state": "HOST_LOST", "reason": "host lost in lobby",
		"last_settled_stacks": null,
	}
	main._on_snapshot_received(closed)
	assert_false(start.is_visible_in_tree(), "Start is still offered")
	assert_false(main.get_node("%BettingControls").visible)
	assert_false(main.get_node("%NextHandControl/Margin/Content/NextHandButton").visible)
	assert_eq(main.get_node("%PlayerInfoPanel/Margin/Content/StatusLabel").text,
		"host lost in lobby")


# ------------------------------------------------------- lost sidecar
# Killing the sidecar mid-hand used to leave "your turn" on screen with live
# betting buttons whose clicks only logged a warning.

func test_losing_the_sidecar_shows_a_persistent_banner():
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	assert_true(main.get_node("%ConnectionBanner").visible)
	assert_eq(main.get_node("%ConnectionBanner").text,
		"Connection lost — close and relaunch")
	main._on_snapshot_received(_heads_up_snapshot())
	assert_true(main.get_node("%ConnectionBanner").visible, "the banner went away")


func test_losing_the_sidecar_on_your_turn_disables_betting():
	var main := _main()
	var snapshot := _heads_up_snapshot()                # you.legal present
	snapshot["turn"]["decision"] = {"action_label": "Call 20", "pot_now": 90}
	main._on_snapshot_received(snapshot)
	var panel: PlayerInfoPanel = main.get_node("%PlayerInfoPanel")
	assert_true(main.get_node("%BettingControls").visible)
	assert_eq(panel.state_badge.text, "YOUR TURN")
	assert_true(panel.decision_card.visible)
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	assert_false(main.get_node("%BettingControls").visible)
	assert_true(main.get_node(
		"%BettingControls/Margin/Content/ActionRow/FoldButton").disabled)
	assert_true(main.get_node(
		"%BettingControls/Margin/Content/ActionRow/RaiseButton").disabled)
	# The panel must not go on telling the player it is their turn.
	assert_eq(panel.state_badge.text, "CONNECTION LOST")
	assert_eq(panel.status_label.text, "Connection lost")
	assert_false(panel.decision_card.visible, "the decision card is still offered")


func test_losing_the_sidecar_hides_next_hand():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "hand_complete"
	main._on_snapshot_received(snapshot)
	assert_true(main.get_node("%NextHandControl").visible)
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	assert_false(main.get_node("%NextHandControl").visible)


func test_losing_the_sidecar_clears_a_shown_refusal():
	## A refusal is about a table that no longer exists once the sidecar is
	## gone; the banner says what matters now.
	var main := _main()
	main._on_snapshot_received(_heads_up_snapshot())
	_refuse(main, "fold", "rejected")
	assert_ne(main.get_node("%CommandStatusLabel").text, "")
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	assert_eq(main.get_node("%CommandStatusLabel").text, "")


func test_no_command_reaches_the_sidecar_after_the_loss():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	main._on_snapshot_received(_heads_up_snapshot())
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	main.get_node("%BettingControls").fold_pressed.emit()
	main.get_node("%BettingControls").check_call_pressed.emit()
	main.get_node("%BettingControls").raise_pressed.emit(120)
	main.get_node("%NextHandControl").next_hand_pressed.emit()
	main.get_node("%LobbyControl").start_game_pressed.emit()
	assert_eq(fake.calls, [])


func test_a_late_snapshot_does_not_reopen_a_lost_table():
	var main := _main()
	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()
	main._on_snapshot_received(_heads_up_snapshot())  # your_turn, legal
	assert_false(main.get_node("%BettingControls").visible)


func test_losing_the_sidecar_mid_start_takes_the_lobby_away():
	## The one failure that produces no command_result at all. The deal
	## holds the round-trip open for ~1s at three seats; a sidecar that
	## dies in that window means no reply is ever coming, and the panel
	## would wait on "Starting..." forever. It is taken away instead.
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["turn"]["state"] = "lobby"
	main._on_snapshot_received(snapshot)
	main.get_node("%LobbyControl/Margin/Content/StartGameButton").pressed.emit()
	assert_true(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").disabled
	)

	main.get_node("%SidecarClient").disconnected_from_sidecar.emit()

	# What the player sees: no lobby panel left to wait on, and the banner
	# saying why. (No command can be sent after the loss either; see
	# test_no_command_reaches_the_sidecar_after_the_loss.)
	assert_false(main.get_node("%LobbyControl").visible,
		"the lobby panel stayed up after the loss")
	assert_false(
		main.get_node("%LobbyControl/Margin/Content/StartGameButton").is_visible_in_tree(),
		"a dead sidecar left Start on screen"
	)
	assert_true(main.get_node("%ConnectionBanner").visible)


func test_desktop_menu_is_driven_by_snapshot_lobby_metadata():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["lobby"] = {"mode": "menu", "state": "idle"}
	main._on_snapshot_received(snapshot)
	assert_true(main.get_node("%ConnectionLobby").visible)
	assert_true(main.get_node("%ConnectionLobby").get_node("%ModeMenu").visible)
	assert_false(main.get_node("%LobbyControl").visible)
	assert_false(main.get_node("%BettingControls").visible)
	assert_false(main.get_node("%NextHandControl").visible)


func test_actual_host_button_sends_lobby_command_through_main():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["lobby"] = {"mode": "menu", "state": "idle"}
	main._on_snapshot_received(snapshot)
	var lobby: ConnectionLobby = main.get_node("%ConnectionLobby")
	lobby.get_node("%NicknameInput").text = "Ada"
	lobby.get_node("%HostLanButton").pressed.emit()
	assert_eq(fake.calls, [["host_lan", {"nickname": "Ada"}]])


func test_internet_buttons_route_through_main_without_interpreting_private_invite():
	for pair in [["HostInternetButton", "host_internet"], ["JoinInternetButton", "join_internet"]]:
		var main := _main()
		var fake: FakeSidecar = FakeSidecarScript.new()
		main._sidecar = fake
		var snapshot := _heads_up_snapshot()
		snapshot["lobby"] = {"mode": "menu", "state": "idle"}
		main._on_snapshot_received(snapshot)
		var lobby: ConnectionLobby = main.get_node("%ConnectionLobby")
		lobby.get_node("%NicknameInput").text = "Ada"
		lobby.get_node("%JoinInviteInput").text = "private-internet-invite"
		lobby.get_node("%" + pair[0]).pressed.emit()
		var payload := {"nickname": "Ada"}
		if pair[1] == "join_internet":
			payload["invite"] = "private-internet-invite"
		assert_eq(fake.calls, [[pair[1], payload]])


func test_lobby_command_refusal_releases_pending_controls():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["lobby"] = {"mode": "menu", "state": "idle"}
	main._on_snapshot_received(snapshot)
	var lobby: ConnectionLobby = main.get_node("%ConnectionLobby")
	lobby.get_node("%HostLanButton").pressed.emit()
	main._on_command_result_received({"command": "host_lan", "ok": false, "error": "Cannot host"})
	assert_false(lobby.get_node("%HostLanButton").disabled)
	assert_eq(lobby.get_node("%ConnectionStatusLabel").text, "Cannot host")


func test_dropped_lobby_command_releases_pending_controls():
	var main := _main()
	var fake: FakeSidecar = FakeSidecarScript.new()
	fake.send_succeeds = false
	main._sidecar = fake
	var snapshot := _heads_up_snapshot()
	snapshot["lobby"] = {"mode": "menu", "state": "idle"}
	main._on_snapshot_received(snapshot)
	var lobby: ConnectionLobby = main.get_node("%ConnectionLobby")
	lobby.get_node("%HostLanButton").pressed.emit()
	assert_false(lobby.get_node("%HostLanButton").disabled)
	assert_eq(lobby.get_node("%ConnectionStatusLabel").text, "Not connected")


func test_sidecar_loss_hides_the_connection_lobby():
	var main := _main()
	var snapshot := _heads_up_snapshot()
	snapshot["lobby"] = {"mode": "menu", "state": "idle"}
	main._on_snapshot_received(snapshot)
	main._on_sidecar_disconnected()
	assert_false(main.get_node("%ConnectionLobby").visible)
	assert_true(main.get_node("%ConnectionBanner").visible)
