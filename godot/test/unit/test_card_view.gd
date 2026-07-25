extends GutTest

const CardViewScene := preload("res://ui/card_view.tscn")


func _card() -> CardView:
	var card: CardView = CardViewScene.instantiate()
	add_child_autofree(card)
	return card


func test_set_card_shows_rank_and_glyph():
	var card := _card()
	card.set_card("10h")
	assert_eq(card.get_node("%CardLabel").text, "10♥")
	assert_true(card.visible)
	assert_true(card.get_node("%CardLabel").visible)


func test_set_face_down_hides_label_but_shows_card():
	var card := _card()
	card.set_face_down()
	assert_true(card.visible)
	assert_false(card.get_node("%CardLabel").visible)


func test_clear_hides_the_whole_card():
	var card := _card()
	card.set_card("Ah")
	card.clear()
	assert_false(card.visible)


func test_switching_from_face_down_to_a_card_shows_label_again():
	var card := _card()
	card.set_face_down()
	card.set_card("Qs")
	assert_true(card.get_node("%CardLabel").visible)
	assert_eq(card.get_node("%CardLabel").text, "Q♠")
