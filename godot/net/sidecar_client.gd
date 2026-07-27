class_name SidecarClient
extends Node

## Godot's half of GODOT_PROTOCOL.md: connects to the local Python
## sidecar over a localhost TCP socket, frames/parses the newline-JSON
## wire format (section 2), and re-emits messages as signals. Holds no
## game logic and no keys -- it only renders what the sidecar sends and
## forwards player commands, per the protocol's core security property.
##
## The sidecar is the source of truth: this client never advances state
## on its own and never trusts a command locally before the sidecar's
## command_result confirms it.

signal connected_to_sidecar
signal disconnected_from_sidecar
signal hello_received(protocol_version: int)
signal snapshot_received(snapshot: Dictionary)
signal command_result_received(result: Dictionary)
## A line from the sidecar didn't parse as a JSON object; forwarded so a
## caller can log/report it, since the protocol says only to ignore it,
## not fail silently for good.
signal malformed_message(raw_line: String)

var _stream: StreamPeerTCP = StreamPeerTCP.new()
var _framer := LineFramer.new()
var _connected := false


## Begin connecting to the sidecar. Actual connection completion (and
## the resulting `hello` + initial snapshot per protocol section 7) is
## observed asynchronously via _process(); connect connected_to_sidecar
## rather than assuming this call is synchronous.
func connect_to_sidecar(host: String = "127.0.0.1", port: int = 0) -> Error:
	var err := _stream.connect_to_host(host, port)
	if err == OK:
		set_process(true)
	return err


func close() -> void:
	_stream.disconnect_from_host()
	if _connected:
		_connected = false
		disconnected_from_sidecar.emit()
	set_process(false)


func is_connected_to_sidecar() -> bool:
	return _connected


func _process(_delta: float) -> void:
	_stream.poll()
	match _stream.get_status():
		StreamPeerTCP.STATUS_CONNECTED:
			if not _connected:
				_connected = true
				connected_to_sidecar.emit()
			_drain_socket()
		StreamPeerTCP.STATUS_ERROR, StreamPeerTCP.STATUS_NONE:
			if _connected:
				_connected = false
				disconnected_from_sidecar.emit()
			set_process(false)
		StreamPeerTCP.STATUS_CONNECTING:
			pass  # keep polling until it resolves


func _drain_socket() -> void:
	var available := _stream.get_available_bytes()
	if available <= 0:
		return
	var result: Array = _stream.get_partial_data(available)
	var err: Error = result[0]
	if err != OK:
		return
	var chunk: PackedByteArray = result[1]
	for line in _framer.feed(chunk):
		_route_line(line)


## Split out from _drain_socket so it is testable without a live socket.
func _route_line(line: String) -> void:
	var trimmed := line.strip_edges()
	if trimmed.is_empty():
		return
	# Use the instance API rather than JSON.parse_string(): the latter logs
	# an engine-level error on failed parse, but a malformed line from the
	# wire is an expected, handled condition here, not a bug to surface.
	var json := JSON.new()
	if json.parse(trimmed) != OK:
		malformed_message.emit(line)
		return
	var parsed: Variant = json.get_data()
	if not (parsed is Dictionary):
		malformed_message.emit(line)
		return
	_route_message(parsed)


## Split out from _route_line so message dispatch is testable directly
## with a Dictionary, matching how the sidecar's JSON decodes.
func _route_message(msg: Dictionary) -> void:
	match str(msg.get("type", "")):
		"hello":
			hello_received.emit(int(msg.get("protocol", 0)))
		"snapshot":
			snapshot_received.emit(msg)
		"command_result":
			command_result_received.emit(msg)
		_:
			pass  # unknown type: ignore, per protocol section 2


## Section 4 commands. amount in raise_to is an absolute per-street
## target, not a delta -- see GODOT_PROTOCOL.md section 4.
func fold() -> void:
	send_command("fold")


func check_call() -> void:
	send_command("check_call")


func raise_to(amount: int) -> void:
	send_command("raise_to", {"amount": amount})


func next_hand() -> void:
	send_command("next_hand")


func send_command(command: String, payload: Dictionary = {}) -> void:
	_send_raw(_build_command_message(command, payload))


## Pure message construction, split out for testing without a socket.
func _build_command_message(command: String, payload: Dictionary) -> Dictionary:
	var msg := {"type": "command", "command": command}
	if not payload.is_empty():
		msg["payload"] = payload
	return msg


func _send_raw(msg: Dictionary) -> void:
	if not _connected:
		push_warning("SidecarClient: send_command called while not connected to a sidecar")
		return
	_stream.put_data((JSON.stringify(msg) + "\n").to_utf8_buffer())
