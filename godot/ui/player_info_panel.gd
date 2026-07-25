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
@onready var verification_label: Label = %VerificationLabel
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
	_apply_verification(snapshot)
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
		"voided":
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


func _apply_verification(snapshot: Dictionary) -> void:
	var verification: Dictionary = snapshot.get("verification", {})
	verification_label.text = str(
		verification.get("label", "Verification state unavailable")
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
	var settlement: Variant = snapshot.get("settlement")
	result_card.visible = settlement is Dictionary or state == "match_complete"
	if settlement is not Dictionary:
		var final_lines := PackedStringArray([
			str(snapshot.get("turn", {}).get("headline", "Match complete")),
		])
		var final_stacks: Variant = snapshot.get("final_stacks")
		var seats: Variant = snapshot.get("seats", [])
		if final_stacks is Array and seats is Array:
			for index in range(min(final_stacks.size(), seats.size())):
				var seat: Variant = seats[index]
				var name := "Seat %d" % index
				if seat is Dictionary:
					name = str(seat.get("name", name))
				final_lines.append("%s: %d" % [name, int(final_stacks[index])])
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
