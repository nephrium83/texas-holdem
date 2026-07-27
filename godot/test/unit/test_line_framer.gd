extends GutTest

const LineFramerScript := preload("res://net/line_framer.gd")


func _framer() -> LineFramer:
	return LineFramerScript.new()


func _bytes(s: String) -> PackedByteArray:
	return s.to_utf8_buffer()


func test_single_complete_line():
	var framer := _framer()
	var lines := framer.feed(_bytes("{\"type\":\"hello\"}\n"))
	assert_eq(lines, ["{\"type\":\"hello\"}"])
	assert_false(framer.has_partial_data())


func test_multiple_lines_in_one_chunk():
	var framer := _framer()
	var lines := framer.feed(_bytes("{\"a\":1}\n{\"b\":2}\n"))
	assert_eq(lines, ["{\"a\":1}", "{\"b\":2}"])


func test_line_split_across_two_feeds():
	var framer := _framer()
	var first := framer.feed(_bytes("{\"type\":\"sna"))
	assert_eq(first, [])
	assert_true(framer.has_partial_data())
	var second := framer.feed(_bytes("pshot\"}\n"))
	assert_eq(second, ["{\"type\":\"snapshot\"}"])
	assert_false(framer.has_partial_data())


func test_partial_trailing_data_retained():
	var framer := _framer()
	var lines := framer.feed(_bytes("{\"a\":1}\n{\"b\":2"))
	assert_eq(lines, ["{\"a\":1}"])
	assert_true(framer.has_partial_data())


func test_empty_feed_yields_no_lines():
	var framer := _framer()
	var lines := framer.feed(PackedByteArray())
	assert_eq(lines, [])


func test_blank_lines_pass_through_as_empty_strings():
	# Blank-line filtering is the caller's job (protocol section 2 says
	# framing is newline-delimited; skipping empties happens one layer up).
	var framer := _framer()
	var lines := framer.feed(_bytes("\n{\"a\":1}\n"))
	assert_eq(lines, ["", "{\"a\":1}"])
