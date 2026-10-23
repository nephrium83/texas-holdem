class_name ConnectionLobby
extends Control

## Connection and seating controls only. The sidecar owns the table and
## readiness rules; this control renders its lobby metadata and sends intent.
signal command_requested(command: String, payload: Dictionary)

const COMMANDS := [
	"local_game", "host_lan", "host_internet", "join_lan", "set_ready", "leave_table", "start_game",
]

@onready var _panel: PanelContainer = %ConnectionPanel
@onready var _shade: ColorRect = %ConnectionShade
@onready var _menu: VBoxContainer = %ModeMenu
@onready var _table_lobby: VBoxContainer = %TableLobby
@onready var _nickname: LineEdit = %NicknameInput
@onready var _join_invite: TextEdit = %JoinInviteInput
@onready var _invite_section: VBoxContainer = %InviteSection
@onready var _invite: TextEdit = %HostInviteText
@onready var _roster: RichTextLabel = %RosterLabel
@onready var _status: Label = %ConnectionStatusLabel
@onready var _title: Label = %ConnectionTitle
@onready var _ready_button: Button = %ReadyButton
@onready var _start_button: Button = %LobbyStartButton
@onready var _leave_button: Button = %LeaveLobbyButton
@onready var _playing_bar: HBoxContainer = %PlayingConnectionBar

var _metadata: Dictionary = {}
var _pending := ""
var _failure := ""
var _local_ready := false


func _ready() -> void:
	%LocalGameButton.pressed.connect(func(): _request("local_game", _identity()))
	%HostLanButton.pressed.connect(func(): _request("host_lan", _identity()))
	%HostInternetButton.pressed.connect(func(): _request("host_internet", _identity()))
	%JoinLanButton.pressed.connect(_on_join_pressed)
	%CopyInviteButton.pressed.connect(_copy_invite)
	_ready_button.pressed.connect(func(): _request("set_ready", {"ready": not _local_ready}))
	_start_button.pressed.connect(func(): _request("start_game"))
	_leave_button.pressed.connect(func(): _request("leave_table"))
	%LeavePlayingButton.pressed.connect(func(): _request("leave_table"))
	visible = false


func apply_lobby(metadata: Dictionary) -> void:
	_metadata = metadata.duplicate(true)
	var state := str(_metadata.get("state", "idle"))
	if (state == "playing" and _pending == "start_game") or (state == "closed" and _pending != "leave_table"):
		_pending = ""
	_render()


func handle_command_result(result: Dictionary) -> void:
	var command := str(result.get("command", ""))
	if command not in COMMANDS:
		return
	if not bool(result.get("ok", false)):
		_pending = ""
		_failure = str(result.get("error", result.get("verdict", "Could not complete request")))
	elif command != "start_game":
		_pending = ""
	_render()


func command_failed(reason: String) -> void:
	_pending = ""
	_failure = reason
	_render()


func _render() -> void:
	visible = true
	var mode := str(_metadata.get("mode", "menu"))
	var state := str(_metadata.get("state", "idle"))
	var internet := str(_metadata.get("network", "lan")) == "internet"
	var playing := state == "playing"
	_panel.visible = not playing
	_shade.visible = not playing
	_playing_bar.visible = playing
	if playing:
		%LeavePlayingButton.disabled = not _pending.is_empty()
		%PlayingConnectionLabel.text = "Local bots" if mode == "local" else "Internet table" if internet else "LAN table"
		return
	var in_menu := mode == "menu"
	_menu.visible = in_menu
	_table_lobby.visible = not in_menu
	_nickname.editable = in_menu and _pending.is_empty() and state != "connecting"
	%NicknameRow.visible = in_menu
	_title.text = "Poker" if in_menu else {
		"local": "Practice with bots",
		"host": "Your Internet table" if internet else "Your LAN table",
		"join": "Internet table" if internet else "LAN table",
	}.get(mode, "Poker table")
	var busy := not _pending.is_empty() or state == "connecting"
	for button in [%LocalGameButton, %HostLanButton, %HostInternetButton, %JoinLanButton]:
		button.disabled = busy
	_join_invite.editable = not busy
	_invite_section.visible = mode == "host" and state != "closed"
	_invite.text = str(_metadata.get("invite", ""))
	%CopyInviteButton.disabled = _invite.text.is_empty()
	_apply_roster(_metadata.get("players", []))
	_ready_button.visible = mode in ["host", "join"] and state == "lobby"
	_ready_button.text = "Not ready" if _local_ready else "Ready"
	_ready_button.disabled = busy
	_start_button.visible = mode in ["local", "host"] and state == "lobby"
	_start_button.disabled = busy or not bool(_metadata.get("can_start", false))
	# A join may still be awaiting peer admission after its command reply.
	# Keep a way back to the menu while that remote connection is pending.
	_leave_button.disabled = not _pending.is_empty()
	_leave_button.text = "Back to menu" if state == "closed" else "Leave table"
	if not _failure.is_empty():
		_status.text = _failure
	elif not str(_metadata.get("error", "")).is_empty():
		_status.text = str(_metadata["error"])
	elif busy:
		_status.text = "Starting hand..." if _pending == "start_game" else "Connecting..." if state == "connecting" or _pending in ["host_lan", "host_internet", "join_lan"] else "Please wait..."
	elif state == "closed":
		_status.text = "This table has closed. Return to the menu to play again."
	elif mode == "host":
		_status.text = "Share the invite with friends outside your home. Everyone must be ready." if internet else "Share the invite with friends on the same network. Everyone must be ready."
	elif mode == "join":
		_status.text = "Press Ready when you want the host to start."
	elif mode == "local":
		_status.text = "Start a practice game against bots."
	else:
		_status.text = "Play for fun with bots or friends nearby or online. No VPN needed."


func _apply_roster(raw_players: Variant) -> void:
	_local_ready = false
	var lines := PackedStringArray()
	if raw_players is Array:
		for raw in raw_players:
			if raw is Dictionary:
				var player: Dictionary = raw
				var suffix := " (you)" if bool(player.get("is_you", false)) else ""
				if bool(player.get("is_host", false)):
					suffix += " · host"
				lines.append("%s%s — %s" % [str(player.get("name", "Player")), suffix,
					"ready" if bool(player.get("ready", false)) else "not ready"])
				if bool(player.get("is_you", false)):
					_local_ready = bool(player.get("ready", false))
	_roster.text = "\n".join(lines) if not lines.is_empty() else "Waiting for players..."


func _identity() -> Dictionary:
	return {"nickname": _nickname.text.strip_edges()}


func _on_join_pressed() -> void:
	var invite := _join_invite.text.strip_edges()
	if invite.is_empty():
		_failure = "Paste the invite from your host first."
		_render()
		return
	var payload := _identity()
	payload["invite"] = invite
	_request("join_lan", payload)


func _request(command: String, payload: Dictionary = {}) -> void:
	if not _pending.is_empty():
		return
	_failure = ""
	_pending = command
	_render()
	command_requested.emit(command, payload)


func _copy_invite() -> void:
	if not _invite.text.is_empty():
		DisplayServer.clipboard_set(_invite.text)
		_status.text = "Invite copied. Send it to friends outside your home." if str(_metadata.get("network", "lan")) == "internet" else "Invite copied. Send it to friends on the same network."
