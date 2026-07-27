extends GutTest

const CardFormatScript := preload("res://ui/card_format.gd")


func test_rank_two_char_card():
	assert_eq(CardFormatScript.rank("As"), "A")


func test_rank_ten_is_three_chars():
	assert_eq(CardFormatScript.rank("10s"), "10")


func test_suit_two_char_card():
	assert_eq(CardFormatScript.suit("As"), "s")


func test_suit_ten_is_three_chars():
	assert_eq(CardFormatScript.suit("10d"), "d")


func test_glyph_for_each_suit():
	assert_eq(CardFormatScript.glyph("2c"), "♣")
	assert_eq(CardFormatScript.glyph("2d"), "♦")
	assert_eq(CardFormatScript.glyph("2h"), "♥")
	assert_eq(CardFormatScript.glyph("2s"), "♠")


func test_hearts_and_diamonds_are_red():
	assert_true(CardFormatScript.is_red("Kh"))
	assert_true(CardFormatScript.is_red("10d"))


func test_clubs_and_spades_are_not_red():
	assert_false(CardFormatScript.is_red("Kc"))
	assert_false(CardFormatScript.is_red("10s"))


func test_display_text_combines_rank_and_glyph():
	assert_eq(CardFormatScript.display_text("Ah"), "A♥")
	assert_eq(CardFormatScript.display_text("10s"), "10♠")
