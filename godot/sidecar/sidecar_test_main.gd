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

# Persistent read buffer: bytes received from the TCP stream but not yet
# dispatched as a complete JSON line.  Must survive across _read_json_line
# calls so that a single TCP segment carrying multiple messages is handled
# correctly — the first call reads both, returns one, and keeps the rest
# here so the second call finds them immediately instead of timing out.
var _tcp_buf := PackedByteArray()


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
	var hello: Variant = _read_json_line(client)
	if not hello is Dictionary:
		printerr("GODOT_ERROR: expected hello dict, got: %s" % str(hello))
		return 1
	if hello.get("type") != "hello":
		printerr("GODOT_ERROR: expected type=hello, got: %s" % str(hello.get("type")))
		return 1
	print("GODOT_HELLO:%d" % int(hello.get("protocol", -1)))

	# Read and verify the initial snapshot.
	var snap: Variant = _read_json_line(client)
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
## Returns the parsed value (a Dictionary for well-formed messages), or null
## on timeout or JSON parse error.
##
## Uses the class-level _tcp_buf so bytes read past the newline in one call
## are available to the next call.  This is essential when the server sends
## two messages back-to-back and they arrive in the same TCP segment (the
## common case on loopback): without persistence the first call would
## consume all bytes, return the first message, and the second call would
## find an empty stream and time out.
func _read_json_line(client: StreamPeerTCP, timeout_ms: int = 5000) -> Variant:
	var deadline := Time.get_ticks_msec() + timeout_ms
	while Time.get_ticks_msec() < deadline:
		# Pull in whatever bytes the stream has ready right now.
		client.poll()
		var available := client.get_available_bytes()
		if available > 0:
			var result := client.get_data(available)
			if result[0] == OK:
				_tcp_buf.append_array(result[1])
		# Scan for newline byte (0x0A = '\n').
		for i in _tcp_buf.size():
			if _tcp_buf[i] == 0x0A:
				var line := _tcp_buf.slice(0, i).get_string_from_utf8()
				# Preserve bytes after the newline for the next call.
				_tcp_buf = _tcp_buf.slice(i + 1)
				return JSON.parse_string(line)
		OS.delay_msec(5)
	printerr("GODOT_ERROR: read timed out after %d ms" % timeout_ms)
	return null
