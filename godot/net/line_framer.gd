class_name LineFramer
extends RefCounted

## Buffers raw bytes from the sidecar socket and yields complete
## newline-delimited lines (GODOT_PROTOCOL.md section 2). A TCP read can
## split a JSON object across chunks or bundle several lines into one
## chunk, so this holds partial data across feed() calls rather than
## assuming one read equals one message.

var _buffer: PackedByteArray = PackedByteArray()

const _NEWLINE := 10  # "\n"


## Feed newly-read bytes in; returns zero or more complete lines (as
## UTF-8 decoded strings, newline stripped). Incomplete trailing data is
## kept for the next feed() call.
func feed(data: PackedByteArray) -> Array[String]:
	_buffer.append_array(data)
	var lines: Array[String] = []
	while true:
		var newline_index := _buffer.find(_NEWLINE)
		if newline_index == -1:
			break
		var line_bytes := _buffer.slice(0, newline_index)
		_buffer = _buffer.slice(newline_index + 1)
		lines.append(line_bytes.get_string_from_utf8())
	return lines


## True if bytes are buffered that haven't formed a complete line yet.
func has_partial_data() -> bool:
	return not _buffer.is_empty()
