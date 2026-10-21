## Exercise the normal desktop menu, real deal presentation, and real bots.
## No card values, keys, or room capabilities are written to the report.
extends SceneTree

const DEADLINE_MS := 75000
var _main: Main
var _snapshot: Dictionary = {}
var _on_screen: Dictionary = {}
var _awaiting := false
var _reply_seen := false
var _pending := ""
var _phase := "first_menu"
var _deadline := 0
var _error := ""
var _finished := false
var _reported := false
var _capture_dir := ""
var _report_path := ""
var _captures := {}
var _capture_pending := false
var _menu_since := 0
var _game := 0
var _seen_openings := {}
var _completed := {}
var _motion := {}
var _was_presenting := false
var _presentation_hand := 0
var _all_in_at := 0
var _all_in_applied := false
var _all_in_seq := -1
var _bot_reacted := false
var _refused := []
var _metrics := {
	"menu_fresh_checks": 0, "opening_preaction_checks": 0,
	"shuffle_frames": 0, "deal_frames": 0, "moving_back_checks": 0,
	"shuffle_motion_checks": 0, "deal_motion_checks": 0,
	"masked_control_checks": 0, "private_card_checks": 0,
	"presentations_completed": 0, "next_hand_presentations": 0,
	"all_in_clicks": 0, "bot_reaction_checks": 0,
	"returned_to_menu": false, "cancelled_during_deal": false,
	"menu_quiet_ms": 0,
}


func _initialize() -> void:
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--presentation-capture-dir="):
			_capture_dir = argument.trim_prefix("--presentation-capture-dir=")
		elif argument.begins_with("--presentation-report="):
			_report_path = argument.trim_prefix("--presentation-report=")
	if not _capture_dir.is_empty() and DirAccess.make_dir_recursive_absolute(_capture_dir) != OK:
		_fail("Could not create the presentation capture folder")
		return
	_deadline = Time.get_ticks_msec() + DEADLINE_MS
	_main = load("res://main.tscn").instantiate()
	root.add_child(_main)
	var sidecar: SidecarClient = _main.get_node("%SidecarClient")
	sidecar.snapshot_received.connect(func(snapshot: Dictionary): _on_screen = snapshot)
	sidecar.snapshot_received.connect(_on_snapshot, CONNECT_DEFERRED)
	sidecar.command_result_received.connect(_on_result, CONNECT_DEFERRED)
	sidecar.disconnected_from_sidecar.connect(func(): _fail("Local game service disconnected"))


func _process(_delta: float) -> bool:
	if not _finished:
		if Time.get_ticks_msec() >= _deadline:
			_fail("Presentation play test timed out in " + _phase)
		elif not _snapshot.is_empty() and is_same(_snapshot, _on_screen):
			_observe_presentation()
			if not _awaiting and not _capture_pending:
				_step()
	if _finished and not _reported and not _capture_pending:
		_report()
	return false


func _lobby() -> ConnectionLobby:
	return _main.get_node("%ConnectionLobby")


func _step() -> void:
	var metadata: Dictionary = _snapshot.get("lobby", {})
	var mode := str(metadata.get("mode", ""))
	var state := str(metadata.get("state", ""))
	var hand := int(_snapshot.get("hand_num", 0))
	var presenting := _main.is_hand_presentation_active()
	if mode == "menu":
		_check_fresh_menu()
		if _menu_since == 0:
			_menu_since = Time.get_ticks_msec()
		var quiet := Time.get_ticks_msec() - _menu_since
		if _phase == "first_menu" and quiet >= 200:
			_capture("01-menu")
			if _capture_pending:
				return
			_game = 1
			_phase = "first_game"
			_press(_lobby().get_node("%LocalGameButton"), "local_game")
		elif _phase == "cancel_menu" and quiet >= 3200:
			_metrics["menu_quiet_ms"] = quiet
			_metrics["returned_to_menu"] = true
			_capture("06-menu-after-cancel")
			if _capture_pending:
				return
			_game = 2
			_phase = "all_in_game"
			_press(_lobby().get_node("%LocalGameButton"), "local_game")
		elif _phase == "final_menu" and quiet >= 600:
			_metrics["returned_to_menu"] = true
			_validate()
		return
	_menu_since = 0
	if state == "lobby":
		_press(_lobby().get_node("%LobbyStartButton"), "start_game")
		return
	if _phase == "first_game" and hand == 2 and presenting and _main.presentation_stage() == "dealing":
		_metrics["next_hand_presentations"] += 1
		_metrics["cancelled_during_deal"] = true
		_phase = "cancel_menu"
		_press(_lobby().get_node("%LeavePlayingButton"), "leave_table")
		return
	if presenting:
		return
	_check_private_cards()
	var turn := str(_snapshot.get("turn", {}).get("state", ""))
	if _phase == "first_game":
		if turn == "your_turn":
			_press(_main.get_node("%BettingControls").get_node("%FoldButton"), "fold")
		elif turn == "hand_complete":
			_capture("05-hand-result")
			if _capture_pending:
				return
			_press(_main.get_node("%NextHandControl").get_node("%NextHandButton"), "next_hand")
	elif _phase == "all_in_game":
		if _all_in_at > 0 and turn in ["hand_complete", "match_complete", "eliminated"]:
			if not _all_in_applied or not _bot_reacted:
				_fail("All In did not commit once and receive a subsequent bot action")
				return
			_phase = "final_menu"
			_press(_lobby().get_node("%LeavePlayingButton"), "leave_table")
		elif turn == "your_turn" and _all_in_at == 0:
			_capture("04-cards-ready")
			if _capture_pending:
				return
			_all_in_at = Time.get_ticks_msec()
			_metrics["all_in_clicks"] += 1
			_press(_main.get_node("%BettingControls").get_node("%PresetAllIn"), "all_in", true)
		elif turn == "hand_complete" and hand < 7:
			_press(_main.get_node("%NextHandControl").get_node("%NextHandButton"), "next_hand")
		elif hand >= 7:
			_fail("No real human turn appeared within seven hands")


func _observe_presentation() -> void:
	var presenting := _main.is_hand_presentation_active()
	var hand := int(_snapshot.get("hand_num", 0))
	if presenting:
		_presentation_hand = hand
		if _main.get_node("%BettingControls").visible or _main.get_node("%NextHandControl").visible:
			_fail("A betting or Next Hand control was available during the deal")
			return
		_metrics["masked_control_checks"] += 1
		for seat in range(_snapshot.get("seats", []).size()):
			var view := _main.get_node("%%TableView/Seat%d" % seat)
			for card_name in ["%CardA", "%CardB"]:
				var face: Label = view.get_node(card_name).get_node("%CardLabel")
				if face.is_visible_in_tree() and _effective_alpha(face) > 0.01:
					_fail("A private card face appeared before the deal finished")
					return
		var stage := _main.presentation_stage()
		_metrics["shuffle_frames" if stage == "shuffling" else "deal_frames"] += 1
		var animation := _main.get_node("DealPresentation")
		for child in animation.get_children():
			if not child is CardView or not child.visible:
				continue
			if child.get_node("%CardLabel").visible:
				_fail("An animated card carried a face")
				return
			var key := "%d/%d/%s" % [_game, hand, child.name]
			if _motion.has(key) and child.position.distance_to(_motion[key]) > 0.5:
				_metrics["moving_back_checks"] += 1
				_metrics["shuffle_motion_checks" if str(child.name).begins_with("DeckBack") else "deal_motion_checks"] += 1
			_motion[key] = child.position
		if _game == 1 and hand == 1:
			if stage == "shuffling" and int(_metrics["shuffle_frames"]) > 8:
				_capture("02-shuffling")
			elif stage == "dealing" and int(_metrics["deal_frames"]) > 3:
				_capture("03-dealing")
	elif _was_presenting and str(_snapshot.get("lobby", {}).get("state", "")) == "playing" and hand == _presentation_hand:
		var key := "%d/%d" % [_game, _presentation_hand]
		if not _completed.has(key):
			_completed[key] = true
			_metrics["presentations_completed"] += 1
		_check_private_cards()
	_was_presenting = presenting


func _on_snapshot(snapshot: Dictionary) -> void:
	_snapshot = snapshot
	var hand := int(snapshot.get("hand_num", 0))
	if str(snapshot.get("phase", "")) == "betting":
		var key := "%d/%d" % [_game, hand]
		if not _seen_openings.has(key):
			_seen_openings[key] = true
			if snapshot.get("you", {}).get("hole", []).size() != 2:
				_fail("The initial betting view did not contain a genuinely dealt hand")
			for event: Dictionary in snapshot.get("events", []):
				if str(event.get("event", "")) == "action":
					_fail("A bot acted before the opening hand could be presented")
			_metrics["opening_preaction_checks"] += 1
	for event: Dictionary in snapshot.get("events", []):
		if str(event.get("event", "")) != "action":
			continue
		if _main.is_hand_presentation_active():
			_fail("The game progressed during its opening deal presentation")
		if _all_in_at > 0 and int(event.get("seat", -1)) == 0 and bool(event.get("all_in", false)):
			_all_in_seq = int(event.get("seq", -1))
		if _all_in_seq >= 0 and int(event.get("seat", -1)) == 1 and int(event.get("seq", -1)) > _all_in_seq:
			_bot_reacted = true
			_metrics["bot_reaction_checks"] += 1
	if _reply_seen:
		_awaiting = false
		_reply_seen = false


func _on_result(result: Dictionary) -> void:
	if not bool(result.get("ok", false)):
		_refused.append(str(result.get("command", "")) + ":" + str(result.get("verdict", "")))
		_fail("A real button command was refused")
	elif _pending == "all_in" and str(result.get("command", "")) == "raise_to":
		_all_in_applied = true
	_reply_seen = true


func _check_fresh_menu() -> void:
	if _main.is_hand_presentation_active() or int(_snapshot.get("hand_num", 0)) != 0 or _snapshot.get("settlement") != null:
		_fail("The menu retained the previous hand or presentation")
	elif _main.get_node("%BettingControls").visible or _main.get_node("%NextHandControl").visible or _main.get_node("%PlayerInfoPanel").get_node("%ResultCard").visible:
		_fail("The menu retained a previous result or action")
	else:
		_metrics["menu_fresh_checks"] += 1


func _check_private_cards() -> void:
	var hole: Variant = _snapshot.get("you", {}).get("hole")
	if not hole is Array or hole.size() != 2:
		return
	for seat: Dictionary in _snapshot.get("seats", []):
		var view := _main.get_node("%%TableView/Seat%d" % int(seat.get("seat", -1)))
		if bool(seat.get("is_you", false)) and not bool(seat.get("folded", false)):
			for index in range(2):
				var card: CardView = view.get_node("%CardA" if index == 0 else "%CardB")
				if not card.visible or not card.get_node("%CardLabel").visible or _effective_alpha(card.get_node("%CardLabel")) < 0.99 or card.get_node("%CardLabel").text != CardFormat.display_text(str(hole[index])):
					_fail("The genuinely dealt private cards did not appear after the animation")
					return
			_metrics["private_card_checks"] += 1
		elif not bool(seat.get("is_you", false)) and str(_snapshot.get("phase", "")) == "betting":
			if view.get_node("%CardA").get_node("%CardLabel").visible or view.get_node("%CardB").get_node("%CardLabel").visible:
				_fail("An opponent's private cards appeared before showdown")


func _effective_alpha(node: Node) -> float:
	var alpha := 1.0
	var current := node
	while current != null:
		if current is CanvasItem:
			alpha *= current.modulate.a
		current = current.get_parent()
	return alpha


func _press(button: Button, command: String, mouse := false) -> void:
	if button.disabled or not button.is_visible_in_tree():
		_fail("A required real button was hidden or disabled: " + command)
		return
	_awaiting = true
	_reply_seen = false
	_pending = command
	if not mouse:
		button.pressed.emit()
		return
	var viewport := button.get_viewport()
	var position := button.get_global_transform_with_canvas() * (button.size / 2)
	viewport.notify_mouse_entered()
	for pressed in [true, false]:
		var event := InputEventMouseButton.new()
		event.position = position
		event.global_position = position
		event.button_index = MOUSE_BUTTON_LEFT
		event.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
		event.pressed = pressed
		viewport.push_input(event, true)


func _capture(label: String) -> void:
	if _capture_dir.is_empty() or _captures.has(label) or _capture_pending:
		return
	_captures[label] = true
	_capture_pending = true
	_capture_frame.call_deferred(label)


func _capture_frame(label: String) -> void:
	await RenderingServer.frame_post_draw
	var screenshot := root.get_texture().get_image()
	if screenshot == null or screenshot.is_empty() or screenshot.save_png(_capture_dir.path_join(label + ".png")) != OK:
		_fail("Could not capture the presentation frame")
	_capture_pending = false


func _validate() -> void:
	for metric in ["menu_fresh_checks", "opening_preaction_checks", "shuffle_frames", "deal_frames", "shuffle_motion_checks", "deal_motion_checks", "masked_control_checks", "private_card_checks", "next_hand_presentations", "bot_reaction_checks"]:
		if int(_metrics[metric]) <= 0:
			_fail("A required observation was not exercised: " + metric)
	if int(_metrics["presentations_completed"]) < 2 or int(_metrics["all_in_clicks"]) != 1 or not _all_in_applied or not _refused.is_empty():
		_fail("The deal, cancellation, and one-click All In path was incomplete")
	_finished = true


func _fail(message: String) -> void:
	if _error.is_empty():
		_error = message
	_finished = true


func _report() -> void:
	_reported = true
	var summary := _metrics.duplicate(true)
	summary.merge({"ok": _error.is_empty(), "error": _error, "refused": _refused, "all_in_applied": _all_in_applied, "captures": _captures.keys()})
	if not _report_path.is_empty():
		var file := FileAccess.open(_report_path, FileAccess.WRITE)
		if file == null:
			summary["ok"] = false
			summary["error"] = "Could not save presentation report"
		else:
			file.store_string(JSON.stringify(summary, "\t") + "\n")
			file.close()
	print("PRESENTATION_SUMMARY " + JSON.stringify(summary))
	quit(0 if summary["ok"] else 1)
