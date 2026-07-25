extends GutTest

const TableViewScene := preload("res://ui/table_view.tscn")


func _table() -> TableView:
	var table: TableView = TableViewScene.instantiate()
	add_child_autofree(table)
	return table


func _heads_up_snapshot() -> Dictionary:
	return {
		"hand_num": 7,
		"pot": 90,
		"board": ["2c", "7d", "10h"],
		"action_on": 1,
		"seats": [
			{
				"seat": 0, "name": "Ada", "stack": 455, "bet": 0,
				"folded": false, "all_in": false, "in_seat": true,
				"sitting_out": false, "last_action": "", "pos": "SB", "is_you": true,
			},
			{
				"seat": 1, "name": "Ben", "stack": 480, "bet": 20,
				"folded": false, "all_in": false, "in_seat": true,
				"sitting_out": false, "last_action": "RAISE 20", "pos": "BB", "is_you": false,
			},
		],
	}


func test_populates_hand_number_and_pot():
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	assert_eq(table.get_node("%HandLabel").text, "Hand #7")
	assert_eq(table.get_node("%PotLabel").text, "Pot: 90")


func test_populates_occupied_seats():
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	assert_eq(table.get_node("%Seat0/Content/NameLabel").text, "Ada (You)")
	assert_eq(table.get_node("%Seat1/Content/NameLabel").text, "Ben")
	assert_eq(table.get_node("%Seat1/Content/TurnIndicator").text, "ACTING")
	assert_eq(table.get_node("%Seat0/Content/TurnIndicator").text, "")


func test_unused_seat_slots_are_cleared():
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	assert_eq(table.get_node("%Seat2/Content/NameLabel").text, "Empty seat")
	assert_eq(table.get_node("%Seat8/Content/NameLabel").text, "Empty seat")


func test_board_is_forwarded_to_board_view():
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	assert_eq(table.get_node("%BoardView/Card0/CardLabel").text, "2♣")
	assert_eq(table.get_node("%BoardView/Card1/CardLabel").text, "7♦")
	assert_eq(table.get_node("%BoardView/Card2/CardLabel").text, "10♥")
	assert_false(table.get_node("%BoardView/Card3").visible)


func test_hand_num_zero_shows_no_hand_label():
	var table := _table()
	var snapshot := _heads_up_snapshot()
	snapshot["hand_num"] = 0
	table.apply_snapshot(snapshot)
	assert_eq(table.get_node("%HandLabel").text, "")


func test_seats_shrinking_across_snapshots_clears_stale_seat():
	# Mirrors the board-shrink regression: a later snapshot with fewer
	# seats (e.g. after an elimination) must not leave a stale render.
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	table.apply_snapshot({"hand_num": 0, "pot": 0, "board": [], "action_on": -1, "seats": []})
	assert_eq(table.get_node("%Seat0/Content/NameLabel").text, "Empty seat")
	assert_eq(table.get_node("%Seat1/Content/NameLabel").text, "Empty seat")
