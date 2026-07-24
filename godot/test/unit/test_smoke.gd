extends GutTest

# Smoke test: GUT runs headless. Also pins the card-string parse rule
# from docs/GODOT_PROTOCOL.md (rank = all but last char, suit = last char,
# ten is '10' so strings are 2-3 chars).


func parse_card(s: String) -> Array:
	return [s.substr(0, s.length() - 1), s.substr(s.length() - 1)]


func test_gut_runs():
	assert_eq(1 + 1, 2)


func test_card_parse_two_char():
	assert_eq(parse_card("As"), ["A", "s"])


func test_card_parse_ten_is_three_chars():
	assert_eq(parse_card("10s"), ["10", "s"])
