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
	client._route_line(JSON.stringify({"type": "future_feature", "payload": {}}))
	assert_signal_not_emitted(client, "malformed_message")
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


func test_lobby_command_payload_preserves_invite_and_ready_types():
	var client := _client()
	assert_eq(client._build_command_message("join_lan", {"nickname": "Ben", "invite": "exact-invite"}), {
		"type": "command", "command": "join_lan", "payload": {"nickname": "Ben", "invite": "exact-invite"},
	})
	assert_eq(client._build_command_message("set_ready", {"ready": false}), {
		"type": "command", "command": "set_ready", "payload": {"ready": false},
	})


func test_send_command_while_disconnected_does_not_crash():
	var client := _client()
	# Not connected to any sidecar; should warn, not error or throw.
	client.fold()
	client.check_call()
	client.raise_to(40)
	client.next_hand()
	assert_false(client.is_connected_to_sidecar())


func test_initial_connection_refusal_or_deadline_emits_failure_once():
	var server := TCPServer.new()
	assert_eq(server.listen(0, "127.0.0.1"), OK)
	var port := server.get_local_port()
	server.stop()
	var client := _client()
	assert_eq(client.connect_to_sidecar("127.0.0.1", port), OK)
	# Some Windows firewalls hold loopback refusal in CONNECTING. Advance
	# the actual process deadline, so either refusal or timeout must report.
	client._process(SidecarClient.CONNECT_TIMEOUT_SECONDS)
	assert_signal_emit_count(client, "connection_failed", 1)
	assert_signal_not_emitted(client, "disconnected_from_sidecar")
	assert_false(client.is_processing())
	client._process(1.0)
	client.close()
	assert_signal_emit_count(client, "connection_failed", 1)


func test_connection_timeout_is_bounded_and_reset_clears_partial_messages():
	var client := _client()
	client._connecting = true
	client._framer.feed("partial old connection".to_utf8_buffer())
	client._advance_connect_timeout(SidecarClient.CONNECT_TIMEOUT_SECONDS - 0.1)
	assert_signal_not_emitted(client, "connection_failed")
	client._advance_connect_timeout(0.2)
	assert_signal_emit_count(client, "connection_failed", 1)
	assert_false(client._framer.has_partial_data())
	client._advance_connect_timeout(100.0)
	assert_signal_emit_count(client, "connection_failed", 1)


func test_oversized_socket_message_disconnects_once_and_resets_framer():
	var server := TCPServer.new()
	assert_eq(server.listen(0, "127.0.0.1"), OK)
	var client := _client()
	assert_eq(client.connect_to_sidecar("127.0.0.1", server.get_local_port()), OK)
	await wait_process_frames(3)
	assert_true(server.is_connection_available())
	var peer := server.take_connection()
	assert_true(client.is_connected_to_sidecar())
	# Fill up to the exact limit first, then send the overflow through the
	# actual socket read path; avoids relying on kernel send-buffer size.
	var payload := PackedByteArray()
	payload.resize(LineFramer.MAX_LINE_BYTES)
	payload.fill(65)
	client._framer.feed(payload)
	assert_eq(peer.put_data("x\n".to_utf8_buffer()), OK)
	await wait_process_frames(5)
	assert_signal_emit_count(client, "malformed_message", 1)
	assert_signal_emit_count(client, "disconnected_from_sidecar", 1)
	assert_false(client.is_connected_to_sidecar())
	assert_false(client._framer.has_partial_data())
	assert_false(client._framer.failed())
	client.close()
	assert_signal_emit_count(client, "disconnected_from_sidecar", 1)
	peer.disconnect_from_host()
	server.stop()


func test_connection_ending_before_success_is_not_silent():
	var client := _client()
	client._connecting = true
	client._process(0.0)  # Native stream is STATUS_NONE, never connected.
	assert_signal_emit_count(client, "connection_failed", 1)
	assert_false(client.is_processing())
