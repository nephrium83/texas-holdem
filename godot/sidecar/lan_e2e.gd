## Drives two real clients against two independently running desktop sidecars.
## The invite is handed between the actual UI fields and is never printed.
extends SceneTree

const DEADLINE_MS := 50000
const TARGET_HANDS := 3

var _mains: Array[Main] = []
var _snapshots: Array[Dictionary] = [{}, {}]
var _on_screen: Array[Dictionary] = [{}, {}]
var _awaiting := [false, false]
var _replied := [false, false]
var _settled: Array[Dictionary] = [{}, {}]
var _face_up_checks := [0, 0]
var _roster_checks := [false, false]
var _commands := {}
var _host_clicked := false
var _join_clicked := false
var _leave_clicked := [false, false]
var _error := ""
var _finished := false
var _reported := false
var _deadline := 0
var _screenshot_dir := ""
var _screenshots := {}
var _capture_pending := false
var _presentation_was_active := [false, false]


func _initialize() -> void:
	var ports := [0, 0]
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--host-sidecar-port="):
			ports[0] = int(arg.trim_prefix("--host-sidecar-port="))
		elif arg.begins_with("--join-sidecar-port="):
			ports[1] = int(arg.trim_prefix("--join-sidecar-port="))
		elif arg.begins_with("--screenshot-dir="):
			_screenshot_dir = arg.trim_prefix("--screenshot-dir=")
	_deadline = Time.get_ticks_msec() + DEADLINE_MS
	for peer in range(2):
		var main: Main = load("res://main.tscn").instantiate()
		root.add_child(main)
		_mains.append(main)
		var sidecar: SidecarClient = main.get_node("%SidecarClient")
		sidecar.snapshot_received.connect(func(snapshot: Dictionary): _on_screen[peer] = snapshot)
		sidecar.snapshot_received.connect(func(snapshot: Dictionary): _on_snapshot(peer, snapshot), CONNECT_DEFERRED)
		# Match snapshot delivery order. A synchronous result could mark an
		# older queued snapshot as the post-command view and send a second
		# action before the fresh snapshot removed the previous turn.
		sidecar.command_result_received.connect(func(result: Dictionary): _on_result(peer, result), CONNECT_DEFERRED)
		sidecar.disconnected_from_sidecar.connect(func(): _fail("local sidecar disconnected"))
		sidecar.connect_to_sidecar("127.0.0.1", ports[peer])
		# Only one scene is shown at once, but both use the same real controls.
		main.visible = peer == 0


func _process(_delta: float) -> bool:
	for peer in range(_mains.size()):
		var presenting := _mains[peer].is_hand_presentation_active()
		if _presentation_was_active[peer] and not presenting and not _on_screen[peer].is_empty():
			_check_cards(peer, _on_screen[peer])
		_presentation_was_active[peer] = presenting
	if not _finished:
		if Time.get_ticks_msec() > _deadline:
			_fail("timed out")
		else:
			_step()
	if _finished and not _reported:
		_report()
	return false


func _lobby(peer: int) -> ConnectionLobby:
	return _mains[peer].get_node("%ConnectionLobby")


func _metadata(peer: int) -> Dictionary:
	return _snapshots[peer].get("lobby", {})


func _press(peer: int, button: Button, command: String) -> bool:
	# The hidden peer is made visible before judging/pressing its controls.
	for index in range(2):
		_mains[index].visible = index == peer
	if button.disabled or not button.is_visible_in_tree():
		return false
	_awaiting[peer] = true
	_replied[peer] = false
	_commands[command] = int(_commands.get(command, 0)) + 1
	button.pressed.emit()
	return true


func _step() -> void:
	if _capture_pending:
		return
	if _snapshots[0].is_empty() or _snapshots[1].is_empty():
		return
	if not _host_clicked and not _awaiting[0]:
		_lobby(0).get_node("%NicknameInput").text = "Ada"
		_host_clicked = _press(0, _lobby(0).get_node("%HostLanButton"), "host_lan")
		return
	if _host_clicked and not _join_clicked and not _awaiting[1]:
		var invite := str(_metadata(0).get("invite", ""))
		if invite.is_empty():
			return
		_capture("host-lobby", 0)
		_lobby(1).get_node("%NicknameInput").text = "Ben"
		_lobby(1).get_node("%JoinInviteInput").text = invite
		_join_clicked = _press(1, _lobby(1).get_node("%JoinLanButton"), "join_lan")
		return
	if not _join_clicked:
		return
	for peer in range(2):
		if _awaiting[peer] or _mains[peer].is_hand_presentation_active() or not is_same(_snapshots[peer], _on_screen[peer]):
			continue
		var metadata := _metadata(peer)
		var state := str(metadata.get("state", ""))
		if _leave_clicked[peer]:
			continue
		if _settled[0].size() >= TARGET_HANDS and _settled[1].size() >= TARGET_HANDS:
			var leave: Button = _lobby(peer).get_node("%LeaveLobbyButton" if state == "closed" else "%LeavePlayingButton")
			_leave_clicked[peer] = _press(peer, leave, "leave_table")
			return
		if state == "closed":
			_fail("LAN table closed before three hands")
			return
		if state == "lobby":
			var players: Array = metadata.get("players", [])
			if players.size() < 2:
				continue
			var roster: RichTextLabel = _lobby(peer).get_node("%RosterLabel")
			_roster_checks[peer] = "Ada" in roster.text and "Ben" in roster.text
			for player: Dictionary in players:
				if bool(player.get("is_you", false)) and not bool(player.get("ready", false)):
					_press(peer, _lobby(peer).get_node("%ReadyButton"), "set_ready")
					return
			if peer == 0 and bool(metadata.get("can_start", false)):
				_press(peer, _lobby(peer).get_node("%LobbyStartButton"), "start_game")
				return
		elif state == "playing":
			var turn := str(_snapshots[peer].get("turn", {}).get("state", ""))
			if turn == "your_turn":
				_capture("joiner-playing" if peer == 1 else "host-playing", peer)
				_press(peer, _mains[peer].get_node("%BettingControls").get_node("%CheckCallButton"), "check_call")
				return
			if turn == "hand_complete":
				if not _settled[1 - peer].has(int(_snapshots[peer].get("hand_num", 0))):
					continue
				_press(peer, _mains[peer].get_node("%NextHandControl").get_node("%NextHandButton"), "next_hand")
				return
			if turn in ["voided", "match_complete", "eliminated", "table_closed"]:
				_fail("unexpected turn state: " + turn)
				return
	if _leave_clicked[0] and _leave_clicked[1] and not _awaiting[0] and not _awaiting[1]:
		if str(_metadata(0).get("mode", "")) != "menu" or str(_metadata(1).get("mode", "")) != "menu":
			return
		if not _roster_checks[0] or not _roster_checks[1] or _face_up_checks[0] == 0 or _face_up_checks[1] == 0:
			_fail("roster or private-card rendering was not exercised")
		else:
			for hand in _settled[0]:
				if _settled[0][hand] != _settled[1].get(hand):
					_fail("peer settlement stacks disagree")
					return
			_finished = true


func _on_result(peer: int, result: Dictionary) -> void:
	if not bool(result.get("ok", false)):
		# A settlement view can precede the matching signed reports. Main
		# releases the Next Hand latch on not_ready; retry on a fresh view.
		# Keep every refused betting action a failure, and never echo errors
		# that might contain a room invite.
		if str(result.get("command", "")) != "next_hand" or str(result.get("verdict", "")) != "not_ready":
			_fail("command refused: " + str(result.get("command", "")))
	_replied[peer] = true


func _on_snapshot(peer: int, snapshot: Dictionary) -> void:
	_snapshots[peer] = snapshot
	if _replied[peer]:
		_awaiting[peer] = false
		_replied[peer] = false
	if str(snapshot.get("phase", "")) == "settled":
		var stacks := []
		for seat: Dictionary in snapshot.get("seats", []):
			stacks.append(int(seat.get("stack", 0)))
		_settled[peer][int(snapshot.get("hand_num", 0))] = stacks
	if is_same(snapshot, _on_screen[peer]):
		_check_cards(peer, snapshot)


func _check_cards(peer: int, snapshot: Dictionary) -> void:
	if _mains[peer].is_hand_presentation_active():
		return
	var you: Dictionary = snapshot.get("you", {})
	var hole: Variant = you.get("hole")
	if not (hole is Array) or hole.size() != 2:
		return
	var own_seat := int(snapshot.get("seat", you.get("seat", -1)))
	if own_seat != peer:
		_fail("joiner was assigned the wrong own seat")
		return
	var view: SeatView = _mains[peer].get_node("%%TableView/Seat%d" % own_seat)
	for index in range(2):
		var card: CardView = view.get_node("%CardA" if index == 0 else "%CardB")
		var label: Label = card.get_node("%CardLabel")
		if not card.visible or not label.visible or label.text != CardFormat.display_text(str(hole[index])):
			_fail("own private cards were not rendered face up")
			return
	_face_up_checks[peer] += 1
	if str(snapshot.get("phase", "")) == "betting":
		var other: SeatView = _mains[peer].get_node("%%TableView/Seat%d" % (1 - own_seat))
		if other.get_node("%CardA").get_node("%CardLabel").visible or other.get_node("%CardB").get_node("%CardLabel").visible:
			_fail("opponent private cards were rendered before showdown")


func _capture(label: String, peer: int) -> void:
	if _screenshot_dir.is_empty() or _screenshots.has(label):
		return
	_screenshots[label] = true
	_capture_pending = true
	_capture_frame.call_deferred(label, peer)


func _capture_frame(label: String, peer: int) -> void:
	for index in range(2):
		_mains[index].visible = index == peer
	await RenderingServer.frame_post_draw
	var screenshot := root.get_texture().get_image()
	if screenshot == null or screenshot.is_empty() or screenshot.save_png(_screenshot_dir.path_join(label + ".png")) != OK:
		_fail("could not capture UI screenshot")
	_capture_pending = false


func _fail(reason: String) -> void:
	if _error.is_empty():
		_error = reason
	_finished = true


func _report() -> void:
	_reported = true
	print("LAN_E2E_SUMMARY " + JSON.stringify({
		"error": _error, "hands_settled": [_settled[0].size(), _settled[1].size()],
		"face_up_checks": _face_up_checks, "roster_checks": _roster_checks,
		"returned_to_menu": [str(_metadata(0).get("mode", "")) == "menu", str(_metadata(1).get("mode", "")) == "menu"],
		"commands": _commands,
	}))
	quit(0 if _error.is_empty() else 1)
