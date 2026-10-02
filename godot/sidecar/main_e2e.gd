## Headless end-to-end driver: the real client, played through its buttons.
##
## Run via:
##   godot --headless --path <project_dir> \
##         -s res://sidecar/main_e2e.gd \
##         -- --sidecar-port=<n> [--hands=<n>]
##
## Instantiates the real main.tscn -- which connects itself to the sidecar
## from --sidecar-port -- and plays the way a person does: Start, then on
## each turn Check/Call, Fold, Raise (sized on the slider) or All In, and
## Next Hand after each hand. Hand 1 is checked/called throughout; Fold and
## Raise are each used at the first chance after that; All In waits for the
## last hand, because it can end the match.
##
## It presses only a button that is visible and enabled, and only once the
## previous command has been answered -- its command_result and the snapshot
## that follows it -- so a refused command is a client defect, never a race
## in this driver.
##
## Prints E2E_* progress lines, then one machine-readable line:
##   E2E_SUMMARY <json>
## and exits 0, or 1 on a timeout or a driver error. The Python test
## (tests/test_godot_sidecar.py) asserts on the summary.

extends SceneTree

## Inside the Python test's own timeout, so a stuck run still reports.
const DEADLINE_MS := 40000
## Hands allowed past the target to find a turn on which to go all in.
const EXTRA_HANDS := 5

var _main: Main
var _target_hands := 10
var _deadline_ms := 0
var _finished := false
var _error := ""

var _awaiting_reply := false
var _reply_seen := false
var _pending := ""

var _state := ""
var _hand_num := 0
var _settled := {}
var _voided := {}
var _settled_stacks := {}
var _stack_carry_checks := 0
var _stack_carry_failures := []
var _results := []
var _refused := []
var _actions := {"check_call": 0, "fold": 0, "raise": 0, "all_in": 0}
var _face_up_checks := 0
var _face_up_failures := []
var _lobby_names := []
## The latest snapshot received, which is the one Main has on screen.
var _on_screen := {}


func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--hands="):
			_target_hands = int(arg.substr("--hands=".length()))
	_deadline_ms = Time.get_ticks_msec() + DEADLINE_MS
	_main = load("res://main.tscn").instantiate()
	root.add_child(_main)
	# Deferred, so each runs after Main's own handler has rendered the
	# message: the driver judges the UI the player would be looking at.
	# Main connects in its _ready, which has not run yet at this point, so a
	# direct connection here would fire first and read the previous frame.
	var sidecar: SidecarClient = _main.get_node("%SidecarClient")
	sidecar.snapshot_received.connect(func(s: Dictionary): _on_screen = s)
	sidecar.snapshot_received.connect(_on_snapshot, CONNECT_DEFERRED)
	sidecar.command_result_received.connect(_on_result, CONNECT_DEFERRED)
	sidecar.disconnected_from_sidecar.connect(
		func(): _fail("sidecar disconnected"), CONNECT_DEFERRED)


func _process(_delta: float) -> bool:
	if _finished:
		return false
	if Time.get_ticks_msec() > _deadline_ms:
		_fail("timed out")
	elif not _awaiting_reply:
		_step()
	if _finished:
		_report()
	return false


func _step() -> void:
	if _state.is_empty():
		return                      # no snapshot yet: nothing is rendered
	if _state in ["match_complete", "eliminated", "table_closed"]:
		_finished = true
		return
	var lobby := _main.get_node("%LobbyControl")
	var start: Button = lobby.get_node("%StartGameButton")
	if _state == "lobby" and start.is_visible_in_tree() and not start.disabled:
		_press(start, "start_game")
		return
	if _main.get_node("%BettingControls").visible:
		_act()
		return
	var next: Button = _main.get_node("%NextHandControl").get_node("%NextHandButton")
	if next.is_visible_in_tree() and not next.disabled:
		if _settled.size() >= _target_hands and _actions["all_in"] > 0:
			_finished = true
		elif _settled.size() + _voided.size() >= _target_hands + EXTRA_HANDS:
			_fail("no turn to go all in on within %d hands" % _hand_num)
		else:
			_press(next, "next_hand")


func _act() -> void:
	var controls := _main.get_node("%BettingControls")
	var raise: Button = controls.get_node("%RaiseButton")
	var can_raise := not raise.disabled
	if _hand_num > 1 and _actions["fold"] == 0:
		_press(controls.get_node("%FoldButton"), "fold")
	elif _hand_num > 1 and _actions["raise"] == 0 and can_raise:
		var slider: HSlider = controls.get_node("%RaiseSlider")
		# A modest raise, set on the slider the way a drag would set it.
		slider.value = min(slider.min_value * 2, (slider.min_value + slider.max_value) / 2)
		if raise.text != "Raise to %d" % int(slider.value):
			_fail("the slider did not drive the raise button: %s" % raise.text)
			return
		_press(raise, "raise")
	elif _settled.size() >= _target_hands - 1 and _actions["all_in"] == 0 and can_raise:
		var all_in: Button = controls.get_node("%PresetAllIn")
		if not _usable(all_in):
			return
		all_in.pressed.emit()
		var slider: HSlider = controls.get_node("%RaiseSlider")
		if slider.value != slider.max_value:
			_fail("All In did not move the slider to its maximum")
			return
		_press(raise, "all_in")
	else:
		_press(controls.get_node("%CheckCallButton"), "check_call")


func _press(button: Button, what: String) -> void:
	if not _usable(button):
		return
	_awaiting_reply = true
	_reply_seen = false
	_pending = what
	print("E2E_PRESS %s hand=%d (%s)" % [what, _hand_num, button.text])
	button.pressed.emit()


## A person cannot click what is hidden or disabled, and emitting pressed
## would bypass both checks -- so the driver refuses rather than cheat.
func _usable(button: Button) -> bool:
	if button.is_visible_in_tree() and not button.disabled:
		return true
	_fail("%s was hidden or disabled when the driver needed it" % button.name)
	return false


func _on_result(result: Dictionary) -> void:
	_results.append(result)
	print("E2E_RESULT %s" % JSON.stringify(result))
	if not bool(result.get("ok", false)):
		_refused.append(result)
	elif _pending in _actions:
		_actions[_pending] += 1
	if _awaiting_reply:
		_reply_seen = true


func _on_snapshot(snapshot: Dictionary) -> void:
	_state = str(snapshot.get("turn", {}).get("state", ""))
	_hand_num = int(snapshot.get("hand_num", 0))
	var phase := str(snapshot.get("phase", ""))
	if phase == "settled" and not _settled.has(_hand_num):
		_settled[_hand_num] = true
		_check_stack_carry(snapshot)
		print("E2E_HAND_SETTLED hand=%d state=%s" % [_hand_num, _state])
	elif phase == "void":
		_voided[_hand_num] = true
	# Main renders each snapshot the moment it arrives, so when two arrive in
	# one frame the screen already shows the second: judge the UI only
	# against the snapshot it is actually showing.
	if is_same(snapshot, _on_screen):
		if phase == "lobby" and _lobby_names.is_empty():
			for i in range(snapshot.get("seats", []).size()):
				_lobby_names.append(_seat_view(i).get_node("%NameLabel").text)
		_check_own_cards(snapshot)
	if _reply_seen:
		_awaiting_reply = false
		_reply_seen = false


## Ten successful button-driven hands must be one continuous match, not ten
## isolated deals: each hand is dealt from the stacks the one before settled.
## A hand's first snapshot cannot show that. The bot may act first -- even fold
## and end the hand -- before the client is shown the hand at all, so stacks
## plus bets on that snapshot are not always the carry-in.
##
## The settlement states the player's net against the stacks its hand was
## dealt from, so stack minus net is the player's carry-in, which must equal
## what the previous hand settled them. The table's chip total must not
## change either; with two seats, the two together fix every seat's carry-in.
func _check_stack_carry(snapshot: Dictionary) -> void:
	var stacks := []
	for seat: Dictionary in snapshot.get("seats", []):
		stacks.append(int(seat.get("stack", 0)))
	_settled_stacks[_hand_num] = stacks
	if not _settled_stacks.has(_hand_num - 1):
		return
	_stack_carry_checks += 1
	var previous: Array = _settled_stacks[_hand_num - 1]
	var settlement: Variant = snapshot.get("settlement")
	var you: Dictionary = settlement.get("you", {}) if settlement is Dictionary else {}
	var seat := int(you.get("seat", -1))
	if you.get("net") == null or seat < 0 or seat >= previous.size():
		_stack_carry_failures.append("hand %d settled without the player's net" % _hand_num)
		return
	var carry_in := int(you.get("stack", 0)) - int(you.get("net"))
	if carry_in != int(previous[seat]):
		_stack_carry_failures.append("hand %d dealt the player %d after hand %d settled them %d" % [
			_hand_num, carry_in, _hand_num - 1, int(previous[seat])])
	var total := _sum(stacks)
	if total != _sum(previous):
		_stack_carry_failures.append("hand %d settled %d chips after hand %d settled %d" % [
			_hand_num, total, _hand_num - 1, _sum(previous)])


func _sum(values: Array) -> int:
	return values.reduce(func(acc: int, value: int) -> int: return acc + value, 0)


## Whenever the snapshot carries the player's own cards, the player's seat
## must show exactly those cards face up -- unless the seat has folded, which
## hides its cards by design.
func _check_own_cards(snapshot: Dictionary) -> void:
	var hole: Variant = snapshot.get("you", {}).get("hole")
	if not (hole is Array):
		return
	var seats: Array = snapshot.get("seats", [])
	for i in range(seats.size()):
		var seat: Dictionary = seats[i]
		if not bool(seat.get("is_you", false)) or bool(seat.get("folded", false)):
			continue
		_face_up_checks += 1
		var view := _seat_view(i)
		for k in range(2):
			var card: CardView = view.get_node("%CardA" if k == 0 else "%CardB")
			var label: Label = card.get_node("%CardLabel")
			var expected := CardFormat.display_text(str(hole[k]))
			if not (card.visible and label.visible and label.text == expected):
				_face_up_failures.append("hand %d seat %d card %d: wanted %s face up, saw %s (visible=%s)" % [
					_hand_num, i, k, str(hole[k]), label.text, label.visible])


func _seat_view(index: int) -> SeatView:
	return _main.get_node("%%TableView/Seat%d" % index)


func _fail(message: String) -> void:
	if _error.is_empty():
		_error = message
		print("E2E_ERROR %s" % message)
	_finished = true


func _report() -> void:
	var settled := _settled.keys()
	settled.sort()
	var summary := {
		"hands_settled": _settled.size(),
		"settled_hand_numbers": settled,
		"hands_voided": _voided.size(),
		"stack_carry_checks": _stack_carry_checks,
		"stack_carry_failures": _stack_carry_failures,
		"command_results": _results.size(),
		"refused": _refused,
		"actions": _actions,
		"face_up_checks": _face_up_checks,
		"face_up_failures": _face_up_failures,
		"lobby_names": _lobby_names,
		"final_state": _state,
		"error": _error,
	}
	print("E2E_SUMMARY %s" % JSON.stringify(summary))
	quit(1 if not _error.is_empty() else 0)
