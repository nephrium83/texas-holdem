extends GutTest

const DealScene := preload("res://ui/deal_presentation.tscn")


func _deal() -> DealPresentation:
	var deal: DealPresentation = DealScene.instantiate()
	add_child_autofree(deal)
	deal.size = Vector2(1000, 700)
	return deal


func _targets(count: int = 2) -> Array[Vector2]:
	var targets: Array[Vector2] = []
	for index in range(count):
		targets.append(Vector2(160 + index * 75, 580))
	return targets


func test_fresh_deal_shuffles_two_packets_using_only_card_backs():
	var deal := _deal()
	deal.start(3, _targets())
	deal.set_process(false)
	assert_true(deal.visible)
	assert_eq(deal.stage, "shuffling")
	assert_eq(deal.get_node("StageLabel").text, "Shuffling…")
	var first: CardView = deal.get_node("DeckBack0")
	var second: CardView = deal.get_node("DeckBack1")
	var initial_distance := first.position.distance_to(second.position)
	deal._process(0.22)
	assert_gt(first.position.distance_to(second.position), initial_distance + 50.0)
	for child in deal.get_children():
		if child is CardView:
			assert_false(child.get_node("CardLabel").visible, "animations must never expose a face")
			assert_eq(child.mouse_filter, Control.MOUSE_FILTER_IGNORE)
			assert_eq(child.get_node("CardLabel").mouse_filter, Control.MOUSE_FILTER_IGNORE)
	assert_eq(deal.mouse_filter, Control.MOUSE_FILTER_IGNORE)


func test_cards_travel_then_land_in_two_rounds_at_seat_centers():
	var deal := _deal()
	var targets := _targets()
	deal.start(1, targets)
	deal.set_process(false)
	deal._process(DealPresentation.SHUFFLE_SECONDS + 0.05)
	assert_eq(deal.stage, "dealing")
	assert_eq(deal.get_node("StageLabel").text, "Dealing…")
	var first: CardView = deal.get_node("DealtBack0")
	assert_true(first.visible)
	assert_gt(first.position.distance_to(deal._origin), 40.0, "card should have left the deck")
	assert_false(deal.get_node("DealtBack3").visible, "second round cannot arrive immediately")
	deal._process(deal._deal_seconds() - 0.05)
	for index in range(4):
		var card: CardView = deal.get_node("DealtBack%d" % index)
		var shift := Vector2(-30, 0) if index < 2 else Vector2(30, 0)
		assert_almost_eq(card.position + DealPresentation.CARD_SIZE * 0.5, targets[index % 2] + shift, Vector2.ONE * 0.01)
		assert_true(card.visible)


func test_nine_seat_deal_finishes_once_in_under_two_point_four_seconds():
	var deal := _deal()
	watch_signals(deal)
	deal.start(1, _targets(9))
	deal.set_process(false)
	assert_lte(deal.duration_seconds(), 2.4)
	deal._process(deal.duration_seconds() + 0.01)
	assert_signal_emit_count(deal, "finished", 1)
	assert_false(deal.visible)
	assert_false(deal.is_playing())
	assert_eq(deal.stage, "idle")
	deal._process(10.0)
	assert_signal_emit_count(deal, "finished", 1)


func test_cancel_and_restart_discard_the_old_flights_without_finishing():
	var deal := _deal()
	watch_signals(deal)
	deal.start(1, _targets(9))
	deal.set_process(false)
	deal._process(1.1)
	deal.cancel()
	assert_false(deal.is_playing())
	assert_false(deal.visible)
	assert_eq(deal.get_child_count(), 2, "only the permanent labels remain")
	assert_signal_not_emitted(deal, "finished")
	deal.start(2, _targets())
	deal.set_process(false)
	assert_eq(deal.stage, "shuffling")
	assert_eq(deal.get_node("DealHandLabel").text, "Hand #2")
	assert_eq(deal.get_child_count(), 18, "restart should contain only the new deck and four cards")
	assert_eq(deal.modulate.a, 1.0)
	assert_signal_not_emitted(deal, "finished")
	deal.start(3, _targets(4))
	deal.set_process(false)
	assert_eq(deal.get_node("DealHandLabel").text, "Hand #3")
	assert_eq(deal.get_child_count(), 22, "a repeated start replaces rather than appends cards")
	assert_eq(deal.stage, "shuffling")
