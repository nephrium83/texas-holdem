class_name LineFramer
extends RefCounted

## Buffers raw bytes from the sidecar socket and yields complete
## newline-delimited lines (GODOT_PROTOCOL.md section 2). A TCP read can
## split a JSON object across chunks or bundle several lines into one
## chunk, so this holds partial data across feed() calls rather than
## assuming one read equals one message.

# A malformed local peer must not grow the renderer without bound.
const MAX_LINE_BYTES := 1024 * 1024
const _NEWLINE := 10
var _buffer: PackedByteArray = PackedByteArray()
var _failed := false


## Lines exclude the newline. An oversized complete OR partial line poisons
## this connection; no further messages are accepted until reset(). Check
## failed() after each feed and close the socket rather than truncating JSON.
func feed(data: PackedByteArray) -> Array[String]:
	var lines: Array[String] = []
	if _failed:
		return lines
	var start := 0
	while start < data.size():
		var newline := data.find(_NEWLINE, start)
		var end := data.size() if newline == -1 else newline
		if _buffer.size() + end - start > MAX_LINE_BYTES:
			_failed = true
			_buffer.clear()
			return []
		_buffer.append_array(data.slice(start, end))
		if newline == -1:
			break
		lines.append(_buffer.get_string_from_utf8())
		_buffer.clear()
		start = newline + 1
	return lines


func failed() -> bool:
	return _failed


func reset() -> void:
	_buffer.clear()
	_failed = false


func has_partial_data() -> bool:
	return not _buffer.is_empty()
