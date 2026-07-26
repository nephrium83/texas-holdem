## Headless sidecar integration test harness for Godot.
##
## Run via:
##   godot --headless --path <project_dir> \
##         -s res://sidecar/sidecar_test_main.gd \
##         -- --sidecar-port=<n>
##
## Prints sentinel lines to stdout:
##   GODOT_CONNECTED
##   GODOT_HELLO:<protocol_version>
##   GODOT_SNAPSHOT:<phase>
##   GODOT_DONE
##
## Exits 0 on success, 1 on any error (also prints GODOT_ERROR:<msg> to stderr).
##
## The Python test (tests/test_godot_sidecar.py) captures this output
## and asserts on the sentinels.

extends SceneTree


func _initialize() -> void:
	var exit_code := _run()
	quit(exit_code)


func _run() -> int:
	var port := _parse_port()
	if port <= 0:
		printerr("GODOT_ERROR: --sidecar-port not provided or invalid")
		return 1

	# Connect to the sidecar.
	var client := StreamPeerTCP.new()
	var err := client.connect_to_host("127.0.0.1", port)
	if err != OK:
		printerr("GODOT_ERROR: connect_to_host returned %d" % err)
		return 1

	# Poll until the TCP handshake completes (or we time out).
	var start := Time.get_ticks_msec()
	while client.get_status() == StreamPeerTCP.STATUS_CONNECTING:
		client.poll()
		OS.delay_msec(10)
		if Time.get_ticks_msec() - start > 3000:
			printerr("GODOT_ERROR: TCP connect timed out")
			return 1

	if client.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		printerr("GODOT_ERROR: unexpected status %d" % client.get_status())
		return 1

	print("GODOT_CONNECTED")

	# Read and verify the hello message.
	var hello := _read_json_line(client)
	if not hello is Dictionary:
		printerr("GODOT_ERROR: expected hello dict, got: %s" % str(hello))
		return 1
	if hello.get("type") != "hello":
		printerr("GODOT_ERROR: expected type=hello, got: %s" % str(hello.get("type")))
		return 1
	print("GODOT_HELLO:%d" % int(hello.get("protocol", -1)))

	# Read and verify the initial snapshot.
	var snap := _read_json_line(client)
	if not snap is Dictionary:
		printerr("GODOT_ERROR: expected snapshot dict, got: %s" % str(snap))
		return 1
	if snap.get("type") != "snapshot":
		printerr("GODOT_ERROR: expected type=snapshot, got: %s" % str(snap.get("type")))
		return 1
	print("GODOT_SNAPSHOT:%s" % str(snap.get("phase", "unknown")))

	print("GODOT_DONE")
	return 0


## Parse --sidecar-port=<n> from the user command-line args (after --).
func _parse_port() -> int:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--sidecar-port="):
			var val := arg.substr("--sidecar-port=".length())
			if val.is_valid_int():
				return val.to_int()
	return -1


## Read one newline-terminated JSON message from a connected StreamPeerTCP.
## Returns the parsed Dictionary, or null on timeout or parse error.
func _read_json_line(client: StreamPeerTCP, timeout_ms: int = 5000) -> Variant:
	var buf := PackedByteArray()
	var deadline := Time.get_ticks_msec() + timeout_ms
	while Time.get_ticks_msec() < deadline:
		client.poll()
		var available := client.get_available_bytes()
		if available > 0:
			var result := client.get_data(available)
			if result[0] == OK:
				buf.append_array(result[1])
		# Scan for newline byte (0x0A).
		for i in buf.size():
			if buf[i] == 0x0A:
				var line := buf.slice(0, i).get_string_from_utf8()
				return JSON.parse_string(line)
		OS.delay_msec(5)
	printerr("GODOT_ERROR: read timed out after %d ms" % timeout_ms)
	return null
