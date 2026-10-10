extends GutTest


const PANEL_SCENE := preload("res://ui/player_info_panel.tscn")


func _panel():
	var panel: Node = PANEL_SCENE.instantiate()
	add_child_autofree(panel)
	return panel


func _turn_snapshot() -> Dictionary:
	return {
		"hand_num": 12,
		"pot": 90,
		"turn": {
			"state": "your_turn",
			"headline": "Your turn | River | 30 to call",
			"street_label": "River",
			"pot": 90,
			"decision": {
				"action_label": "Call 30",
				"pot_now": 90,
				"pot_after_call": 120,
				"pot_odds_pct": 25.0,
				"stack_after_call": 310,
				"effective_stack": 340,
				"can_raise": true,
				"min_raise_to": 80,
				"max_raise_to": 340,
			},
		},
		"you": {
			"made_hand": {
				"description": "Pair of Kings, Ace-Queen-Four kickers",
				"board_plays": false,
			},
		},
		"deal_progress": {
			"state": "in_hand",
			"label": "Hand in progress",
		},
		"events": [
			{"seq": 1, "text": "--- Hand #12 (10/20) ---"},
			{"seq": 2, "text": "Maya raises to 60 | pot 90"},
		],
		"settlement": null,
	}


func test_river_turn_renders_authoritative_decision_facts():
	var panel: Variant = _panel()
	panel.apply_snapshot(_turn_snapshot())

	assert_eq(panel.state_badge.text, "YOUR TURN")
	assert_eq(panel.status_label.text, "Your turn | River | 30 to call")
	assert_eq(panel.street_label.text, "Hand #12 | River | pot 90")
	assert_true(panel.decision_card.visible)
	assert_eq(panel.call_value.text, "Call 30")
	assert_eq(panel.pot_after_value.text, "120")
	assert_eq(panel.odds_value.text, "25.0%")
	assert_eq(panel.raise_value.text, "80 to 340")
	assert_eq(
		panel.hand_label.text,
		"Pair of Kings, Ace-Queen-Four kickers",
	)
	assert_string_contains(panel.event_log.text, "Maya raises to 60")


func test_waiting_state_removes_all_decision_information():
	var panel: Variant = _panel()
	var snapshot := _turn_snapshot()
	snapshot["turn"] = {
		"state": "waiting",
		"headline": "Waiting for Maya",
		"street_label": "Flop",
		"pot": 90,
	}
	panel.apply_snapshot(snapshot)

	assert_eq(panel.status_label.text, "Waiting for Maya")
	assert_false(panel.decision_card.visible)


func test_settled_hand_replaces_decision_card_with_result():
	var panel: Variant = _panel()
	var snapshot := _turn_snapshot()
	snapshot["turn"] = {
		"state": "hand_complete",
		"headline": "You won 120",
		"street_label": "Idle",
		"pot": 0,
	}
	snapshot["deal_progress"] = {
		"state": "settled",
		"label": "Hand settled",
	}
	snapshot["settlement"] = {
		"headline": "You won 120",
		"pots": [{
			"label": "Main pot",
			"awards": [{
				"payouts": [{"name": "You", "amount": 120}],
			}],
		}],
		"you": {"net": 70, "stack": 570},
	}
	panel.apply_snapshot(snapshot)

	assert_false(panel.decision_card.visible)
	assert_true(panel.result_card.visible)
	assert_string_contains(panel.result_text.text, "Main pot: You +120")
	assert_string_contains(panel.result_text.text, "Your net: +70")
	assert_eq(panel.deal_progress_label.text, "Hand settled")


func _settled_snapshot(settlement: Dictionary) -> Dictionary:
	var snapshot := _turn_snapshot()
	snapshot["turn"] = {
		"state": "hand_complete",
		"headline": "You won 120",
		"street_label": "Idle",
		"pot": 0,
	}
	snapshot["settlement"] = settlement
	return snapshot


func test_a_refund_is_listed():
	## The shape player_info.settlement_view gives an uncalled bet.
	var panel: Variant = _panel()
	panel.apply_snapshot(_settled_snapshot({
		"headline": "You won 120",
		"pots": [],
		"refund": {"seat": 1, "name": "Maya", "amount": 40},
		"showdown": [],
		"you": {"net": 60, "stack": 560},
	}))
	assert_string_contains(panel.result_text.text, "40 returned to Maya (uncalled)")


func test_each_showdown_seat_gets_its_hand_description():
	var panel: Variant = _panel()
	panel.apply_snapshot(_settled_snapshot({
		"headline": "You won 120",
		"pots": [],
		"refund": null,
		"showdown": [
			{"seat": 0, "name": "You", "shown": true, "mucked": false, "won": 120,
				"hands": [{"run": 1, "name": "Pair",
					"description": "Pair of Kings, Ace-Queen-Four kickers"}]},
			{"seat": 1, "name": "Maya", "shown": true, "mucked": false, "won": 0,
				"hands": [{"run": 1, "name": "High Card",
					"description": "Ace-high, Jack-Nine-Six-Two kickers"}]},
		],
		"you": {"net": 60, "stack": 560},
	}))
	assert_string_contains(panel.result_text.text,
		"You shows Pair of Kings, Ace-Queen-Four kickers")
	assert_string_contains(panel.result_text.text,
		"Maya shows Ace-high, Jack-Nine-Six-Two kickers")


func test_a_closed_table_shows_the_reason_and_the_last_settled_chips():
	## The shape client_view sends when the session ends abnormally: the
	## seats still hold the cut-off hand's live stacks, terminal holds the
	## stacks the last settled hand left.
	var panel: Variant = _panel()
	var snapshot := _turn_snapshot()
	snapshot["turn"] = {
		"state": "table_closed",
		"headline": "host connection peer0 dropped during play",
		"street_label": "Flop",
		"pot": 100,
	}
	snapshot["seats"] = [
		{"name": "You", "stack": 950}, {"name": "Maya", "stack": 950},
	]
	snapshot["terminal"] = {
		"state": "HOST_LOST",
		"reason": "host connection peer0 dropped during play",
		"last_settled_stacks": [970, 1030],
	}
	panel.apply_snapshot(snapshot)

	assert_eq(panel.state_badge.text, "TABLE CLOSED")
	assert_eq(panel.status_label.text, "host connection peer0 dropped during play")
	assert_false(panel.decision_card.visible)
	assert_true(panel.result_card.visible)
	assert_string_contains(panel.result_text.text, "You: 970")
	assert_string_contains(panel.result_text.text, "Maya: 1030")
	assert_false(panel.result_text.text.contains("950"),
		"showed the cut-off hand's stacks")


func test_match_complete_has_no_next_turn_decision():
	var panel: Variant = _panel()
	var snapshot := _turn_snapshot()
	snapshot["turn"] = {
		"state": "match_complete",
		"headline": "You won the match",
		"street_label": "Idle",
		"pot": 0,
	}
	snapshot["settlement"] = null
	snapshot["seats"] = [{"name": "You"}, {"name": "Maya"}]
	snapshot["final_stacks"] = [1000, 0]
	panel.apply_snapshot(snapshot)

	assert_false(panel.decision_card.visible)
	assert_true(panel.result_card.visible)
	assert_string_contains(panel.result_text.text, "You won the match")
	assert_string_contains(panel.result_text.text, "You: 1000")
	assert_string_contains(panel.result_text.text, "Maya: 0")


func test_a_seat_out_before_the_end_sees_the_final_chips():
	## The shape client_view sends a seat eliminated before the match ended:
	## its settlement and seats still describe the hand it busted in, while
	## final_stacks holds what the match ended with.
	var panel: Variant = _panel()
	var snapshot := _settled_snapshot({
		"headline": "Hand complete",
		"pots": [],
		"refund": null,
		"showdown": [],
		"you": {"net": -20, "stack": 0},
	})
	snapshot["turn"] = {
		"state": "match_complete",
		"headline": "Ravi won the match",
		"street_label": "Idle",
		"pot": 0,
	}
	snapshot["seats"] = [
		{"name": "You", "stack": 0},
		{"name": "Maya", "stack": 1040},
		{"name": "Ravi", "stack": 980},
	]
	snapshot["final_stacks"] = [0, 0, 2020]
	panel.apply_snapshot(snapshot)

	assert_true(panel.result_card.visible)
	assert_string_contains(panel.result_text.text, "Ravi won the match")
	assert_string_contains(panel.result_text.text, "Your net: -20 | stack 0")
	assert_string_contains(panel.result_text.text, "Final chips:\nYou: 0\nMaya: 0\nRavi: 2020")
	assert_false(panel.result_text.text.contains("1040"),
		"showed the busting hand's stacks as the outcome")
