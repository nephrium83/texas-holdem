class_name CardView
extends PanelContainer

## Renders a single card per docs/GODOT_PROTOCOL.md section 3, or a
## face-down placeholder, or nothing (an absent card -- e.g. a folded
## seat, or an opponent's hole cards before showdown, per the no-leak
## invariant: this component never receives a card it isn't shown).

@onready var _label: Label = %CardLabel

const _FACE_UP_STYLE := preload("res://ui/card_face_up.tres")
const _FACE_DOWN_STYLE := preload("res://ui/card_face_down.tres")

const _RED := Color(0.75, 0.12, 0.14)
const _BLACK := Color(0.1, 0.1, 0.12)


func set_card(card: String) -> void:
	visible = true
	_label.visible = true
	_label.text = CardFormat.display_text(card)
	_label.add_theme_color_override(
		"font_color", _RED if CardFormat.is_red(card) else _BLACK
	)
	add_theme_stylebox_override("panel", _FACE_UP_STYLE)


func set_face_down() -> void:
	visible = true
	_label.visible = false
	add_theme_stylebox_override("panel", _FACE_DOWN_STYLE)


func clear() -> void:
	visible = false
