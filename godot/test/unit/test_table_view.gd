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


func test_own_hole_cards_render_face_up():
	## The sidecar sends the local cards only in you.hole. Every snapshot of
	## a live run carried them while the local seat still showed card backs.
	var table := _table()
	var snapshot := _heads_up_snapshot()
	snapshot["you"] = {"hole": ["Ah", "Kd"]}
	table.apply_snapshot(snapshot)
	assert_true(table.get_node("%Seat0/Content/HoleCards/CardA/CardLabel").visible)
	assert_eq(table.get_node("%Seat0/Content/HoleCards/CardA/CardLabel").text, "A♥")
	assert_eq(table.get_node("%Seat0/Content/HoleCards/CardB/CardLabel").text, "K♦")


func test_own_hole_cards_go_to_no_other_seat():
	var table := _table()
	var snapshot := _heads_up_snapshot()
	snapshot["you"] = {"hole": ["Ah", "Kd"]}
	table.apply_snapshot(snapshot)
	assert_true(table.get_node("%Seat1/Content/HoleCards/CardA").visible)
	assert_false(table.get_node("%Seat1/Content/HoleCards/CardA/CardLabel").visible,
		"the opponent's seat showed a face")
	assert_false(snapshot["seats"][0].has("hole"), "the snapshot itself was altered")


func _lobby_snapshot() -> Dictionary:
	## The shape client_view._lobby_snapshot sends: membership only.
	return {
		"type": "snapshot", "phase": "lobby", "hand_num": 0,
		"seats": [
			{"seat": 0, "conn_id": "seat0", "name": "Player", "is_you": true},
			{"seat": 1, "conn_id": "seat1", "name": "Bot 1", "is_you": false},
		],
		"you": {"seat": 0},
	}


func test_lobby_seats_show_the_players_names():
	## Every lobby seat used to render "Empty seat": the lobby snapshot has
	## no in_seat, which SeatView read as an unoccupied seat.
	var table := _table()
	table.apply_snapshot(_lobby_snapshot())
	assert_eq(table.get_node("%Seat0/Content/NameLabel").text, "Player (You)")
	assert_eq(table.get_node("%Seat1/Content/NameLabel").text, "Bot 1")
	assert_eq(table.get_node("%Seat2/Content/NameLabel").text, "Empty seat")


func test_lobby_seats_show_no_stack_or_cards():
	## Nothing has been dealt, so no card backs and no invented stack.
	var table := _table()
	table.apply_snapshot(_heads_up_snapshot())
	table.apply_snapshot(_lobby_snapshot())
	assert_eq(table.get_node("%Seat1/Content/InfoRow/StackLabel").text, "")
	assert_false(table.get_node("%Seat1/Content/HoleCards/CardA").visible)
	assert_false(table.get_node("%Seat1/Content/HoleCards/CardB").visible)


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


func test_settled_and_closed_hands_never_keep_a_stale_acting_badge():
	var table := _table()
	for phase in ["settled", "void", "dealing"]:
		var snapshot := _heads_up_snapshot()
		snapshot["phase"] = phase
		table.apply_snapshot(snapshot)
		assert_eq(table.get_node("%Seat1/Content/TurnIndicator").text, "")
	var closed := _heads_up_snapshot()
	closed["phase"] = "betting"
	closed["turn"] = {"state": "table_closed"}
	table.apply_snapshot(closed)
	assert_eq(table.get_node("%Seat1/Content/TurnIndicator").text, "")


func test_a_finished_hand_never_keeps_a_stale_wager_label():
	# Sibling of the stale ACTING badge above. Ben's last_action is
	# "RAISE 20"; once the hand is settled, void or closed that raise is no
	# longer out, and the label must go with it.
	var table := _table()
	var live := _heads_up_snapshot()
	live["phase"] = "betting"
	table.apply_snapshot(live)
	assert_eq(table.get_node("%Seat1/Content/StatusLabel").text, "RAISE 20",
		"the fixture shows no wager label on a live hand, so this proves nothing")

	for phase in ["settled", "void"]:
		var snapshot := _heads_up_snapshot()
		snapshot["phase"] = phase
		table.apply_snapshot(snapshot)
		assert_eq(table.get_node("%Seat1/Content/StatusLabel").text, "", phase)
	for state in ["hand_complete", "voided", "table_closed", "match_complete", "eliminated"]:
		var snapshot := _heads_up_snapshot()
		snapshot["phase"] = "betting"
		snapshot["turn"] = {"state": state}
		table.apply_snapshot(snapshot)
		assert_eq(table.get_node("%Seat1/Content/StatusLabel").text, "", state)

	# Blinds posted during the deal are live wagers and stay labelled.
	var dealing := _heads_up_snapshot()
	dealing["phase"] = "dealing"
	table.apply_snapshot(dealing)
	assert_eq(table.get_node("%Seat1/Content/StatusLabel").text, "RAISE 20")


func _full_table(names: Array) -> Dictionary:
	var seats := []
	for i in range(TableView.MAX_SEATS):
		seats.append({
			"seat": i, "name": names[i % names.size()], "stack": 1000 - i,
			"bet": 50 if i % 2 == 0 else 0, "folded": i == 4, "all_in": i == 6,
			"in_seat": true, "sitting_out": false,
			"last_action": "RAISE 250", "pos": "BTN" if i == 1 else null,
			"is_you": i == 0,
		})
	return {"hand_num": 3, "pot": 400, "phase": "betting", "action_on": 2,
		"board": ["2c", "7d", "10h", "Js", "Qd"], "seats": seats}


func _overlaps(table: TableView) -> Array:
	var found := []
	for i in range(TableView.MAX_SEATS):
		for j in range(i + 1, TableView.MAX_SEATS):
			var a: Rect2 = table.get_node("%%Seat%d" % i).get_global_rect()
			var b: Rect2 = table.get_node("%%Seat%d" % j).get_global_rect()
			if a.intersects(b):
				found.append("Seat%d %s x Seat%d %s" % [i, a, j, b])
	return found


func test_no_two_seat_panels_overlap_at_a_full_table():
	# The nine slots were spaced for a 150-high panel; an occupied one
	# rendered 172 high, so neighbours down each side ran into each other.
	var table := _table()
	table.apply_snapshot(_full_table(["Ada", "Ben", "Cy"]))
	await wait_frames(3)
	var first: Rect2 = table.get_node("%Seat0").get_global_rect()
	assert_gt(first.size.y, 150.0,
		"an occupied panel is no taller than an empty one, so the layout is not under test")
	assert_eq(_overlaps(table), [])


func test_long_names_cannot_push_seat_panels_into_each_other():
	# A name is the one piece of seat text a player chooses. It must not be
	# able to grow a panel: every panel keeps the size the layout was
	# spaced for, whatever it is asked to show.
	var table := _table()
	var snapshot := _full_table([
		"Bartholomew the Magnificent of Upper Slaughter",
		"Wolfeschlegelsteinhausenbergerdorff",
		"A somewhat long but ordinary nickname",
	])
	# Chip counts are the other text that can run long: the launcher's own
	# play test seats 100000-chip stacks.
	for seat: Dictionary in snapshot["seats"]:
		seat["stack"] = 123456789
		seat["bet"] = 123456789
		seat["pos"] = "BTN"
		seat["last_action"] = "ALL-IN 123456789 and then some more text"
		seat["folded"] = false
		seat["all_in"] = false
	table.apply_snapshot(snapshot)
	await wait_frames(3)
	assert_eq(_overlaps(table), [])
	var sizes := {}
	for i in range(TableView.MAX_SEATS):
		sizes[table.get_node("%%Seat%d" % i).size] = true
	assert_eq(sizes.size(), 1,
		"seat panels differ in size, so a neighbour's content can move them: %s" % [sizes.keys()])


func test_a_seat_panel_keeps_its_size_when_the_seat_empties_or_folds():
	# Panels that shrink when a seat folds or empties shift what the eye
	# was tracking, and a layout checked at one size says nothing at another.
	var table := _table()
	table.apply_snapshot(_full_table(["Ada", "Ben", "Cy"]))
	await wait_frames(3)
	var occupied: Vector2 = table.get_node("%Seat0").size
	table.apply_snapshot({"hand_num": 0, "pot": 0, "board": [], "action_on": -1, "seats": []})
	await wait_frames(3)
	assert_eq(table.get_node("%Seat0").size, occupied)
	assert_eq(_overlaps(table), [])
