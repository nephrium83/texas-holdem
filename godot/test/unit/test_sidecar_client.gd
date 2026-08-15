extends GutTest

const SidecarClientScript := preload("res://net/sidecar_client.gd")


func _client() -> SidecarClient:
	var client: SidecarClient = SidecarClientScript.new()
	add_child_autofree(client)
	watch_signals(client)
	return client


func test_hello_message_emits_hello_received():
	var client := _client()
	client._route_message({"type": "hello", "protocol": 1})
	assert_signal_emitted_with_parameters(client, "hello_received", [1])


func test_snapshot_message_emits_snapshot_received():
	var client := _client()
	var snapshot := {"type": "snapshot", "phase": "lobby"}
	client._route_message(snapshot)
	assert_signal_emitted_with_parameters(client, "snapshot_received", [snapshot])


func test_command_result_emits_command_result_received():
	var client := _client()
	var result := {"type": "command_result", "command": "fold", "ok": true, "verdict": "applied"}
	client._route_message(result)
	assert_signal_emitted_with_parameters(client, "command_result_received", [result])


func test_unknown_type_is_ignored_not_errored():
	var client := _client()
	# Forward compatibility (protocol section 2): unknown types are silently
	# ignored, not treated as malformed.
	client._route_message({"type": "future_feature", "payload": {}})
	assert_signal_not_emitted(client, "snapshot_received")
	assert_signal_not_emitted(client, "hello_received")
	assert_signal_not_emitted(client, "command_result_received")


func test_malformed_json_line_emits_malformed_message():
	var client := _client()
	client._route_line("not valid json{{{")
	assert_signal_emitted_with_parameters(client, "malformed_message", ["not valid json{{{"])


func test_json_that_is_not_an_object_emits_malformed_message():
	var client := _client()
	# A bare JSON array/number is syntactically valid JSON but not the
	# dict-shaped message the protocol requires.
	client._route_line("[1, 2, 3]")
	assert_signal_emitted(client, "malformed_message")


func test_blank_line_is_silently_skipped():
	var client := _client()
	client._route_line("")
	client._route_line("   ")
	assert_signal_not_emitted(client, "malformed_message")
	assert_signal_not_emitted(client, "snapshot_received")


func test_start_game_builds_command_with_no_payload():
	var client := _client()
	var msg := client._build_command_message("start_game", {})
	assert_eq(msg, {"type": "command", "command": "start_game"})


func test_fold_builds_command_with_no_payload():
	var client := _client()
	var msg := client._build_command_message("fold", {})
	assert_eq(msg, {"type": "command", "command": "fold"})


func test_check_call_builds_command_with_no_payload():
	var client := _client()
	var msg := client._build_command_message("check_call", {})
	assert_eq(msg, {"type": "command", "command": "check_call"})


func test_raise_to_builds_command_with_amount_payload():
	var client := _client()
	var msg := client._build_command_message("raise_to", {"amount": 60})
	assert_eq(msg, {"type": "command", "command": "raise_to", "payload": {"amount": 60}})


func test_next_hand_builds_command_with_no_payload():
	var client := _client()
	var msg := client._build_command_message("next_hand", {})
	assert_eq(msg, {"type": "command", "command": "next_hand"})


func test_send_command_while_disconnected_does_not_crash():
	var client := _client()
	# Not connected to any sidecar; should warn, not error or throw.
	client.fold()
	client.check_call()
	client.raise_to(40)
	client.next_hand()
	assert_false(client.is_connected_to_sidecar())
