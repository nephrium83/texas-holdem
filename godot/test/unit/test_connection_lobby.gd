extends GutTest

const LobbyScene := preload("res://ui/connection_lobby.tscn")


func _control() -> ConnectionLobby:
	var control: ConnectionLobby = LobbyScene.instantiate()
	add_child_autofree(control)
	watch_signals(control)
	return control


func _host(can_start := false) -> Dictionary:
	return {
		"mode": "host", "state": "lobby", "invite": "test-invite-do-not-log",
		"can_start": can_start,
		"players": [
			{"name": "Ada", "seat": 0, "ready": true, "is_you": true, "is_host": true},
			{"name": "Ben", "seat": 1, "ready": can_start, "is_you": false},
		],
	}


func test_menu_actions_carry_the_entered_name():
	for pair in [["LocalGameButton", "local_game"], ["HostLanButton", "host_lan"]]:
		var control := _control()
		control.apply_lobby({"mode": "menu", "state": "idle"})
		control.get_node("%NicknameInput").text = " Ada "
		control.get_node("%" + pair[0]).pressed.emit()
		assert_signal_emitted_with_parameters(control, "command_requested", [pair[1], {"nickname": "Ada"}])


func test_join_sends_the_pasted_invite_without_changing_it():
	var control := _control()
	control.apply_lobby({"mode": "menu", "state": "idle"})
	control.get_node("%NicknameInput").text = "Ben"
	control.get_node("%JoinInviteInput").text = "  exact-invite-value  "
	control.get_node("%JoinLanButton").pressed.emit()
	assert_signal_emitted_with_parameters(control, "command_requested", [
		"join_lan", {"nickname": "Ben", "invite": "exact-invite-value"},
	])


func test_empty_invite_gives_an_actionable_message_without_sending():
	var control := _control()
	control.apply_lobby({"mode": "menu", "state": "idle"})
	control.get_node("%JoinLanButton").pressed.emit()
	assert_signal_not_emitted(control, "command_requested")
	assert_eq(control.get_node("%ConnectionStatusLabel").text, "Paste the invite from your host first.")


func test_pending_command_cannot_be_duplicated_and_failure_allows_retry():
	var control := _control()
	control.apply_lobby({"mode": "menu", "state": "idle"})
	control.get_node("%HostLanButton").pressed.emit()
	control.get_node("%HostLanButton").pressed.emit()
	assert_signal_emit_count(control, "command_requested", 1)
	control.handle_command_result({"command": "host_lan", "ok": false, "error": "Address unavailable"})
	assert_eq(control.get_node("%ConnectionStatusLabel").text, "Address unavailable")
	assert_false(control.get_node("%HostLanButton").disabled)
	control.get_node("%HostLanButton").pressed.emit()
	assert_signal_emit_count(control, "command_requested", 2)


func test_host_roster_and_start_follow_sidecar_readiness():
	var control := _control()
	control.apply_lobby(_host())
	assert_true(control.get_node("%InviteSection").visible)
	assert_eq(control.get_node("%HostInviteText").text, "test-invite-do-not-log")
	assert_true(control.get_node("%LobbyStartButton").disabled)
	assert_string_contains(control.get_node("%RosterLabel").text, "Ada (you) · host — ready")
	assert_string_contains(control.get_node("%RosterLabel").text, "Ben — not ready")
	control.apply_lobby(_host(true))
	assert_false(control.get_node("%LobbyStartButton").disabled)


func test_joiner_can_toggle_own_ready_but_cannot_start_or_copy_host_invite():
	var control := _control()
	var metadata := _host(true)
	metadata["mode"] = "join"
	metadata["players"][0]["is_you"] = false
	metadata["players"][1]["is_you"] = true
	control.apply_lobby(metadata)
	assert_false(control.get_node("%LobbyStartButton").visible)
	assert_false(control.get_node("%InviteSection").visible)
	assert_eq(control.get_node("%ReadyButton").text, "Not ready")
	control.get_node("%ReadyButton").pressed.emit()
	assert_signal_emitted_with_parameters(control, "command_requested", ["set_ready", {"ready": false}])


func test_successful_start_stays_latched_until_playing_snapshot():
	var control := _control()
	control.apply_lobby(_host(true))
	control.get_node("%LobbyStartButton").pressed.emit()
	control.handle_command_result({"command": "start_game", "ok": true})
	control.apply_lobby(_host(true))
	assert_true(control.get_node("%LobbyStartButton").disabled)
	var metadata := _host(true)
	metadata["state"] = "playing"
	control.apply_lobby(metadata)
	assert_false(control.get_node("%ConnectionPanel").visible)
	assert_true(control.get_node("%PlayingConnectionBar").visible)
	control.get_node("%LeavePlayingButton").pressed.emit()
	assert_signal_emitted_with_parameters(control, "command_requested", ["leave_table", {}])


func test_closed_table_explains_failure_and_allows_return_to_menu():
	var control := _control()
	var metadata := _host()
	metadata["state"] = "closed"
	metadata["error"] = "Host disconnected"
	control.apply_lobby(metadata)
	assert_eq(control.get_node("%ConnectionStatusLabel").text, "Host disconnected")
	assert_false(control.get_node("%ReadyButton").visible)
	assert_false(control.get_node("%LobbyStartButton").visible)
	assert_eq(control.get_node("%LeaveLobbyButton").text, "Back to menu")
	control.get_node("%LeaveLobbyButton").pressed.emit()
	assert_signal_emitted_with_parameters(control, "command_requested", ["leave_table", {}])


func test_host_panel_and_menu_fit_the_preview_window():
	var control := _control()
	for metadata in [{"mode": "menu", "state": "idle"}, _host(true)]:
		control.apply_lobby(metadata)
		await wait_process_frames(2)
		var panel: Control = control.get_node("%ConnectionPanel")
		assert_true(Rect2(0, 0, 1370, 850).encloses(panel.get_rect()))
		var content: Control = panel.get_node("Margin/Content")
		assert_true(panel.get_global_rect().encloses(content.get_global_rect()), "lobby controls overflow the panel")
