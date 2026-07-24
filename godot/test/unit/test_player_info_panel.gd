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
		"verification": {
			"state": "audit_pending",
			"label": "Deal active | final audit pending",
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
	snapshot["verification"] = {
		"state": "verified",
		"label": "Deal and settlement verified",
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
	assert_eq(panel.verification_label.text, "Deal and settlement verified")


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
