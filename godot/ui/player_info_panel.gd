class_name PlayerInfoPanel
extends PanelContainer


@onready var state_badge: Label = %StateBadge
@onready var status_label: Label = %StatusLabel
@onready var street_label: Label = %StreetLabel
@onready var decision_card: PanelContainer = %DecisionCard
@onready var pot_value: Label = %PotValue
@onready var call_value: Label = %CallValue
@onready var pot_after_value: Label = %PotAfterValue
@onready var odds_value: Label = %OddsValue
@onready var stack_value: Label = %StackValue
@onready var effective_value: Label = %EffectiveValue
@onready var raise_value: Label = %RaiseValue
@onready var hand_label: Label = %HandLabel
@onready var deal_progress_label: Label = %DealProgressLabel
@onready var event_log: RichTextLabel = %EventLog
@onready var result_card: PanelContainer = %ResultCard
@onready var result_text: Label = %ResultText


func apply_snapshot(snapshot: Dictionary) -> void:
	var turn: Dictionary = snapshot.get("turn", {})
	var state := str(turn.get("state", "lobby"))
	state_badge.text = state.replace("_", " ").to_upper()
	status_label.text = str(turn.get("headline", "Waiting for state"))
	street_label.text = _street_context(snapshot, turn)
	_apply_state_color(state)
	_apply_decision(turn)
	_apply_hand(snapshot)
	_apply_deal_progress(snapshot)
	_apply_events(snapshot.get("events", []))
	_apply_result(snapshot, state)


func _street_context(snapshot: Dictionary, turn: Dictionary) -> String:
	var hand_num := int(snapshot.get("hand_num", 0))
	var street := str(turn.get("street_label", "Idle"))
	var pot := int(turn.get("pot", snapshot.get("pot", 0)))
	if hand_num <= 0:
		return "%s | pot %d" % [street, pot]
	return "Hand #%d | %s | pot %d" % [hand_num, street, pot]


func _apply_state_color(state: String) -> void:
	match state:
		"your_turn":
			state_badge.modulate = Color("#efb76f")
		"hand_complete", "match_complete":
			state_badge.modulate = Color("#6fd5a7")
		"voided", "table_closed":
			state_badge.modulate = Color("#ef6f72")
		_:
			state_badge.modulate = Color("#9ab8aa")


func _apply_decision(turn: Dictionary) -> void:
	var decision: Dictionary = turn.get("decision", {})
	decision_card.visible = not decision.is_empty()
	if decision.is_empty():
		return
	pot_value.text = str(int(decision.get("pot_now", 0)))
	call_value.text = str(decision.get("action_label", "Check"))
	pot_after_value.text = str(int(decision.get("pot_after_call", 0)))
	odds_value.text = "%.1f%%" % float(decision.get("pot_odds_pct", 0.0))
	stack_value.text = str(int(decision.get("stack_after_call", 0)))
	effective_value.text = str(int(decision.get("effective_stack", 0)))
	if bool(decision.get("can_raise", false)):
		raise_value.text = "%d to %d" % [
			int(decision.get("min_raise_to", 0)),
			int(decision.get("max_raise_to", 0)),
		]
	else:
		raise_value.text = "Not available"


func _apply_hand(snapshot: Dictionary) -> void:
	var you: Dictionary = snapshot.get("you", {})
	var made: Dictionary = you.get("made_hand", {})
	if made.is_empty():
		hand_label.text = "Made hand appears on the flop"
		return
	var suffix := " | board plays" if bool(made.get("board_plays", false)) else ""
	hand_label.text = "%s%s" % [str(made.get("description", "")), suffix]


## Renders deal PROGRESS, not a verification verdict. The label this
## reads is derived from the hand's phase and says nothing about whether
## proofs checked out; snapshot.deal_policy and snapshot.proofs_verified
## carry that, and are deliberately not collapsed into a claim here.
func _apply_deal_progress(snapshot: Dictionary) -> void:
	var progress: Dictionary = snapshot.get("deal_progress", {})
	deal_progress_label.text = str(
		progress.get("label", "Deal state unavailable")
	)


func _apply_events(raw_events: Variant) -> void:
	var scroll_bar := event_log.get_v_scroll_bar()
	var follow_latest := (
		scroll_bar.value >= scroll_bar.max_value - scroll_bar.page - 2.0
	)
	var lines := PackedStringArray()
	if raw_events is Array:
		for raw_event in raw_events:
			if raw_event is Dictionary:
				lines.append(str(raw_event.get("text", "")))
	event_log.text = "\n".join(lines) if not lines.is_empty() else "No hand events yet."
	if follow_latest:
		event_log.call_deferred("scroll_to_line", max(0, lines.size() - 1))


func _apply_result(snapshot: Dictionary, state: String) -> void:
	if state == "table_closed":
		_apply_closed(snapshot)
		return
	var settlement: Variant = snapshot.get("settlement")
	result_card.visible = settlement is Dictionary or state == "match_complete"
	if settlement is not Dictionary:
		var final_lines := PackedStringArray([
			str(snapshot.get("turn", {}).get("headline", "Match complete")),
		])
		var final_stacks: Variant = snapshot.get("final_stacks")
		var seats: Variant = snapshot.get("seats", [])
		if final_stacks is Array and seats is Array:
			final_lines.append_array(_stack_lines(final_stacks, seats))
		result_text.text = "\n".join(final_lines)
		return

	var summary: Dictionary = settlement
	var turn_headline := str(
		snapshot.get("turn", {}).get("headline", "Hand complete")
	)
	var summary_headline := str(summary.get("headline", "Hand complete"))
	var lines := PackedStringArray([turn_headline])
	if summary_headline != turn_headline:
		lines.append(summary_headline)
	# The settlement view has always carried the showdown hands and any
	# refund; the panel dropped both, so the hands that decided a showdown
	# and an uncalled bet coming back appeared nowhere a player could read.
	for raw_show in summary.get("showdown", []):
		if raw_show is Dictionary:
			lines.append(_showdown_line(raw_show))
	for raw_pot in summary.get("pots", []):
		if raw_pot is not Dictionary:
			continue
		var pot: Dictionary = raw_pot
		for raw_award in pot.get("awards", []):
			if raw_award is not Dictionary:
				continue
			var award: Dictionary = raw_award
			var paid := PackedStringArray()
			for raw_payout in award.get("payouts", []):
				if raw_payout is Dictionary:
					paid.append("%s +%d" % [
						str(raw_payout.get("name", "Seat")),
						int(raw_payout.get("amount", 0)),
					])
			lines.append("%s: %s" % [
				str(pot.get("label", "Pot")),
				", ".join(paid),
			])
	var refund: Variant = summary.get("refund")
	if refund is Dictionary:
		lines.append("%d returned to %s (uncalled)" % [
			int(refund.get("amount", 0)),
			str(refund.get("name", "Seat")),
		])
	var you: Dictionary = summary.get("you", {})
	if you.get("net") != null:
		var net := int(you.get("net", 0))
		var net_text := "+%d" % net if net >= 0 else str(net)
		lines.append(
			"Your net: %s | stack %d" % [
				net_text,
				int(you.get("stack", 0)),
			]
		)
	result_text.text = "\n".join(lines)


## A closed table (snapshot.terminal, GODOT_PROTOCOL.md section 5) has no
## next hand. Shows why, and the chips as the last settled hand left them: a
## hand cut off mid-play never paid out, so the seats' own stacks still have
## its pot taken out of them.
func _apply_closed(snapshot: Dictionary) -> void:
	result_card.visible = true
	var lines := PackedStringArray([
		str(snapshot.get("turn", {}).get("headline", "Table closed")),
	])
	var terminal: Variant = snapshot.get("terminal")
	var stacks: Variant = (
		terminal.get("last_settled_stacks") if terminal is Dictionary else null
	)
	var seats: Variant = snapshot.get("seats", [])
	if stacks is Array and seats is Array:
		lines.append("Chips after the last settled hand:")
		lines.append_array(_stack_lines(stacks, seats))
	result_text.text = "\n".join(lines)


func _stack_lines(stacks: Array, seats: Array) -> PackedStringArray:
	var lines := PackedStringArray()
	for index in range(min(stacks.size(), seats.size())):
		var seat: Variant = seats[index]
		var name := "Seat %d" % index
		if seat is Dictionary:
			name = str(seat.get("name", name))
		lines.append("%s: %d" % [name, int(stacks[index])])
	return lines


## One settlement.showdown entry: the seat's exact hand description for each
## run, read from the settlement rather than decoded from score tuples.
func _showdown_line(show: Dictionary) -> String:
	var name := str(show.get("name", "Seat"))
	if bool(show.get("mucked", false)):
		return "%s mucks" % name
	var hands := PackedStringArray()
	for raw_hand in show.get("hands", []):
		if raw_hand is Dictionary:
			hands.append(str(raw_hand.get("description", "")))
	return "%s shows %s" % [name, " / ".join(hands)]
