class_name DealPresentation
extends Control

## A presentation of a fresh deal, never a source of game state. Only seat
## centers enter this component; every animated card uses the existing back.
signal finished
signal stage_changed(value: String)

const CardViewScene := preload("res://ui/card_view.tscn")
const CARD_SIZE := Vector2(56.0, 78.0)
const PACKET_CARDS := 12
const SHUFFLE_SECONDS := 0.82
const FLIGHT_SECONDS := 0.28
const CARD_INTERVAL := 0.045
const HOLD_SECONDS := 0.12
const FADE_SECONDS := 0.16

## Empty uses the center of this table-sized overlay. Coordinates passed to
## start() and this optional origin are local to the overlay.
var deck_center := Vector2.ZERO
var stage := "idle"

var _elapsed := 0.0
var _playing := false
var _targets: Array[Vector2] = []
var _deck: Array[CardView] = []
var _dealt: Array[CardView] = []
var _label: Label
var _hand_label: Label
var _origin := Vector2.ZERO


func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	_label = Label.new()
	_label.name = "StageLabel"
	_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_label.add_theme_font_size_override("font_size", 22)
	_label.add_theme_color_override("font_color", Color(0.96, 0.79, 0.48))
	add_child(_label)
	_hand_label = Label.new()
	_hand_label.name = "DealHandLabel"
	_hand_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_hand_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_hand_label.add_theme_font_size_override("font_size", 13)
	_hand_label.add_theme_color_override("font_color", Color(0.72, 0.81, 0.76))
	add_child(_hand_label)
	set_process(false)
	hide()


## Two rounds of dealing, preserving the caller's table/dealer seat order.
## cancel() never emits finished; a completed current run emits it once.
func start(hand_num: int, targets: Array[Vector2]) -> void:
	cancel()
	if targets.is_empty():
		return
	_targets.assign(targets.slice(0, 9))
	_origin = deck_center if deck_center != Vector2.ZERO else Vector2(size.x * 0.5, size.y * 0.43)
	_elapsed = 0.0
	_playing = true
	_set_stage("shuffling")
	modulate.a = 1.0
	_label.text = "Shuffling…"
	_label.position = _origin + Vector2(-110, 52)
	_label.size = Vector2(220, 32)
	_hand_label.text = "Hand #%d" % hand_num
	_hand_label.position = _origin + Vector2(-110, 85)
	_hand_label.size = Vector2(220, 22)
	for index in range(PACKET_CARDS):
		_deck.append(_new_back("DeckBack%d" % index))
	for index in range(_targets.size() * 2):
		var card := _new_back("DealtBack%d" % index)
		card.hide()
		_dealt.append(card)
	# Text stays above the deck and every flight, including paths below it.
	move_child(_label, -1)
	move_child(_hand_label, -1)
	show()
	_render_shuffle()
	set_process(true)


func cancel() -> void:
	_playing = false
	_set_stage("idle")
	set_process(false)
	hide()
	for card in _deck + _dealt:
		remove_child(card)
		card.free()
	_deck.clear()
	_dealt.clear()
	_targets.clear()
	modulate.a = 1.0


func is_playing() -> bool:
	return _playing


func duration_seconds() -> float:
	return SHUFFLE_SECONDS + _deal_seconds() + HOLD_SECONDS + FADE_SECONDS


func _process(delta: float) -> void:
	if not _playing:
		return
	_elapsed += maxf(delta, 0.0)
	if _elapsed < SHUFFLE_SECONDS:
		_render_shuffle()
		return
	_set_stage("dealing")
	_label.text = "Dealing…"
	_render_deal(_elapsed - SHUFFLE_SECONDS)
	var fade_start := SHUFFLE_SECONDS + _deal_seconds() + HOLD_SECONDS
	modulate.a = 1.0 - clampf((_elapsed - fade_start) / FADE_SECONDS, 0.0, 1.0)
	if _elapsed >= duration_seconds():
		cancel()
		finished.emit()


func _render_shuffle() -> void:
	# Separate two packets, then riffle their alternating backs into one
	# stack. Each card lands a little after the last for visible motion.
	var split := smoothstep(0.0, 0.24, _elapsed)
	for index in range(_deck.size()):
		var bank := -1.0 if index % 2 == 0 else 1.0
		var riffle := smoothstep(0.0, 0.22, _elapsed - 0.24 - index * 0.025)
		var spread := split * (1.0 - riffle)
		var stack_offset := Vector2(index * 0.35, -index * 0.75)
		var center := _origin + stack_offset + Vector2(bank * 35.0 * spread, -sin(riffle * PI) * 16.0)
		_place_card(_deck[index], center, 0.82, bank * 0.18 * spread)


func _render_deal(deal_elapsed: float) -> void:
	for index in range(_deck.size()):
		_place_card(_deck[index], _origin + Vector2(index * 0.35, -index * 0.75), 0.82, 0.0)
	for index in range(_dealt.size()):
		var card := _dealt[index]
		var local_time := deal_elapsed - index * CARD_INTERVAL
		card.visible = local_time >= 0.0
		if not card.visible:
			continue
		var fraction := clampf(local_time / FLIGHT_SECONDS, 0.0, 1.0)
		var eased := 1.0 - pow(1.0 - fraction, 3.0)
		var target := _targets[index % _targets.size()] + Vector2(-30.0 if index < _targets.size() else 30.0, 0.0)
		var center := _origin.lerp(target, eased)
		center.y -= sin(fraction * PI) * 36.0
		var twist := (-0.17 if index % 2 == 0 else 0.17) * sin(fraction * PI)
		_place_card(card, center, lerpf(0.82, 1.0, eased), twist)


func _deal_seconds() -> float:
	return maxf(0.0, (_targets.size() * 2 - 1) * CARD_INTERVAL) + FLIGHT_SECONDS


func _new_back(card_name: String) -> CardView:
	var card: CardView = CardViewScene.instantiate()
	card.name = card_name
	add_child(card)
	card.size = CARD_SIZE
	card.pivot_offset = CARD_SIZE * 0.5
	card.set_face_down()
	_ignore_mouse(card)
	return card


func _ignore_mouse(node: Node) -> void:
	if node is Control:
		node.mouse_filter = Control.MOUSE_FILTER_IGNORE
	for child in node.get_children():
		_ignore_mouse(child)


func _place_card(card: CardView, center: Vector2, card_scale: float, angle: float) -> void:
	card.position = center - CARD_SIZE * 0.5
	card.scale = Vector2.ONE * card_scale
	card.rotation = angle


func _set_stage(value: String) -> void:
	if stage != value:
		stage = value
		stage_changed.emit(value)


func _exit_tree() -> void:
	# There are no deferred callbacks or tweens to outlive this presentation.
	_playing = false
	set_process(false)
