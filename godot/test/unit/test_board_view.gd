extends GutTest

const BoardViewScene := preload("res://ui/board_view.tscn")


func _board() -> BoardView:
	var board: BoardView = BoardViewScene.instantiate()
	add_child_autofree(board)
	return board


func test_empty_board_shows_no_cards():
	var view := _board()
	view.apply_board([])
	for i in range(5):
		assert_false(view.get_node("%%Card%d" % i).visible)


func test_flop_shows_three_cards_and_clears_the_rest():
	var view := _board()
	view.apply_board(["2c", "7d", "10h"])
	assert_eq(view.get_node("%Card0/CardLabel").text, "2♣")
	assert_eq(view.get_node("%Card1/CardLabel").text, "7♦")
	assert_eq(view.get_node("%Card2/CardLabel").text, "10♥")
	assert_false(view.get_node("%Card3").visible)
	assert_false(view.get_node("%Card4").visible)


func test_full_board_shows_all_five_cards():
	var view := _board()
	view.apply_board(["2c", "7d", "10h", "Ks", "3d"])
	for i in range(5):
		assert_true(view.get_node("%%Card%d" % i).visible)


func test_board_shrinking_across_snapshots_clears_stale_cards():
	# A void/redeal can reset street progression; a stale card from a
	# previous snapshot must not linger once the board reports fewer cards.
	var view := _board()
	view.apply_board(["2c", "7d", "10h"])
	view.apply_board([])
	assert_false(view.get_node("%Card0").visible)
