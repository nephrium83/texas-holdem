## One packaged client, controlled through its real buttons on one computer.
## Configuration and invite coordination use private files, never arguments
## containing an invite. Reports deliberately contain no invite or hole cards.
extends SceneTree

var _main: Main
var _config: Dictionary = {}
var _snapshot: Dictionary = {}
var _on_screen: Dictionary = {}
var _awaiting := false
var _reply_seen := false
var _pending := ""
var _role := ""
var _target_hands := 3
var _deadline := 0
var _error := ""
var _finished := false
var _reported := false
var _completed_at := 0
var _selected := false
var _invite_written := false
var _settled := {}
var _commands := {}
var _own_seat := -1
var _face_up_checks := 0
var _opponent_hidden_checks := 0
var _roster_names := []


func _initialize() -> void:
	var config_path := ""
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--lan-peer-config="):
			config_path = argument.trim_prefix("--lan-peer-config=")
	if not FileAccess.file_exists(config_path):
		_fail("LAN test configuration is missing")
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(config_path))
	if not parsed is Dictionary:
		_fail("LAN test configuration is invalid")
		return
	_config = parsed
	_role = str(_config.get("role", ""))
	if _role not in ["host", "join"] or str(_config.get("report_path", "")).is_empty():
		_fail("LAN test role or report path is missing")
		return
	_target_hands = max(1, int(_config.get("target_hands", 3)))
	_deadline = Time.get_ticks_msec() + int(_config.get("deadline_seconds", 120)) * 1000
	_main = load("res://main.tscn").instantiate()
	root.add_child(_main)
	var sidecar: SidecarClient = _main.get_node("%SidecarClient")
	sidecar.snapshot_received.connect(func(snapshot: Dictionary): _on_screen = snapshot)
	sidecar.snapshot_received.connect(_on_snapshot, CONNECT_DEFERRED)
	sidecar.command_result_received.connect(_on_result, CONNECT_DEFERRED)
	sidecar.disconnected_from_sidecar.connect(func():
		if _completed_at == 0:
			_fail("Local game service disconnected"))


func _process(_delta: float) -> bool:
	if _finished:
		_finish()
		return false
	if _completed_at > 0:
		# The coordinator can wait for both reports before releasing either
		# app. This avoids closing the host while the guest records the result.
		var finish_path := str(_config.get("finish_signal_path", ""))
		if (not finish_path.is_empty() and FileAccess.file_exists(finish_path)) or (finish_path.is_empty() and Time.get_ticks_msec() - _completed_at >= 1500) or Time.get_ticks_msec() >= _deadline:
			_finished = true
			_finish()
		return false
	if Time.get_ticks_msec() >= _deadline:
		_fail("LAN play test timed out")
	elif not _awaiting:
		_step()
	return false


func _lobby() -> ConnectionLobby:
	return _main.get_node("%ConnectionLobby")


func _press(button: Button, command: String) -> bool:
	if button.disabled or not button.is_visible_in_tree():
		return false
	_awaiting = true
	_reply_seen = false
	_pending = command
	_commands[command] = int(_commands.get(command, 0)) + 1
	button.pressed.emit()
	return true


func _step() -> void:
	if _snapshot.is_empty():
		return
	var metadata: Dictionary = _snapshot.get("lobby", {})
	var state := str(metadata.get("state", ""))
	var mode := str(metadata.get("mode", ""))
	if not _selected and mode == "menu":
		_lobby().get_node("%NicknameInput").text = str(_config.get("nickname", "LAN Player"))
		if _role == "host":
			_selected = _press(_lobby().get_node("%HostLanButton"), "host_lan")
		else:
			var invite_path := str(_config.get("invite_read_path", ""))
			if not FileAccess.file_exists(invite_path):
				return
			_lobby().get_node("%JoinInviteInput").text = FileAccess.get_file_as_string(invite_path).strip_edges()
			_selected = _press(_lobby().get_node("%JoinLanButton"), "join_lan")
		return
	if _role == "host" and not _invite_written:
		var invite := str(_lobby().get_node("%HostInviteText").text)
		if not invite.is_empty():
			var output := FileAccess.open(str(_config.get("invite_write_path", "")), FileAccess.WRITE)
			if output == null:
				_fail("Could not write private invite coordination file")
				return
			output.store_string(invite)
			output.close()
			_invite_written = true
	if state == "closed":
		_fail("LAN table closed before the target hands settled")
		return
	if state == "lobby":
		var players: Array = metadata.get("players", [])
		_roster_names = []
		for player: Dictionary in players:
			_roster_names.append(str(player.get("name", "")))
			if bool(player.get("is_you", false)) and not bool(player.get("ready", false)):
				_press(_lobby().get_node("%ReadyButton"), "set_ready")
				return
		if _role == "host" and bool(_config.get("start_when_ready", true)) and bool(metadata.get("can_start", false)):
			_press(_lobby().get_node("%LobbyStartButton"), "start_game")
		return
	if state != "playing":
		return
	var turn := str(_snapshot.get("turn", {}).get("state", ""))
	if turn == "your_turn":
		_press(_main.get_node("%BettingControls").get_node("%CheckCallButton"), "check_call")
	elif turn == "hand_complete":
		if _settled.size() >= _target_hands:
			if _face_up_checks == 0 or _opponent_hidden_checks == 0 or _roster_names.size() < 2:
				_fail("Private cards or multiplayer roster were not exercised")
			else:
				_completed_at = Time.get_ticks_msec()
				_write_report()
		else:
			_press(_main.get_node("%NextHandControl").get_node("%NextHandButton"), "next_hand")
	elif turn in ["voided", "match_complete", "eliminated", "table_closed"]:
		_fail("Unexpected poker lifecycle state: " + turn)


func _on_result(result: Dictionary) -> void:
	if not bool(result.get("ok", false)):
		# A hand can settle before all peers' matching reports arrive. The
		# real UI releases Next Hand on not_ready; retry on a later snapshot.
		if str(result.get("command", "")) != "next_hand" or str(result.get("verdict", "")) != "not_ready":
			_fail("Player command was refused: " + str(result.get("command", "")))
	_reply_seen = true


func _on_snapshot(snapshot: Dictionary) -> void:
	_snapshot = snapshot
	if _reply_seen:
		_awaiting = false
		_reply_seen = false
	if str(snapshot.get("phase", "")) == "settled":
		var stacks := []
		for seat: Dictionary in snapshot.get("seats", []):
			stacks.append(int(seat.get("stack", 0)))
		_settled[str(int(snapshot.get("hand_num", 0)))] = stacks
	if is_same(snapshot, _on_screen):
		_check_cards(snapshot)


func _check_cards(snapshot: Dictionary) -> void:
	var you: Dictionary = snapshot.get("you", {})
	var hole: Variant = you.get("hole")
	if not hole is Array or hole.size() != 2:
		return
	_own_seat = int(snapshot.get("seat", you.get("seat", -1)))
	if _own_seat < 0:
		_fail("The client has no own seat")
		return
	var seats: Array = snapshot.get("seats", [])
	for seat: Dictionary in seats:
		var index := int(seat.get("seat", -1))
		var view: SeatView = _main.get_node("%%TableView/Seat%d" % index)
		if bool(seat.get("is_you", false)):
			if index != _own_seat:
				_fail("Own seat identity differs from the rendered table")
				return
			for card_index in range(2):
				var card: CardView = view.get_node("%CardA" if card_index == 0 else "%CardB")
				var label: Label = card.get_node("%CardLabel")
				if not card.visible or not label.visible or label.text != CardFormat.display_text(str(hole[card_index])):
					_fail("Own private cards were not rendered face up")
					return
			_face_up_checks += 1
		elif str(snapshot.get("phase", "")) == "betting":
			if view.get_node("%CardA").get_node("%CardLabel").visible or view.get_node("%CardB").get_node("%CardLabel").visible:
				_fail("Opponent private cards were visible before showdown")
				return
			_opponent_hidden_checks += 1


func _fail(message: String) -> void:
	if _error.is_empty():
		_error = message
	_finished = true


func _report() -> Dictionary:
	return {
		"ok": _error.is_empty() and _settled.size() >= _target_hands,
		"error": _error, "role": _role, "hands_settled": _settled.size(),
		"settled_stacks": _settled, "own_seat": _own_seat,
		"face_up_checks": _face_up_checks, "opponent_hidden_checks": _opponent_hidden_checks,
		"roster_names": _roster_names, "commands": _commands,
	}


func _write_report() -> void:
	var path := str(_config.get("report_path", ""))
	if path.is_empty():
		return
	var output := FileAccess.open(path, FileAccess.WRITE)
	if output == null:
		_error = "Could not save LAN play test report"
		_finished = true
		return
	output.store_string(JSON.stringify(_report(), "  ") + "\n")
	output.close()


func _finish() -> void:
	if _reported:
		return
	_reported = true
	_write_report()
	print("LAN_PEER_E2E_SUMMARY " + JSON.stringify(_report()))
	quit(0 if bool(_report()["ok"]) else 1)
