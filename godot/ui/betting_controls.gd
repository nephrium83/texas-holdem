class_name BettingControls
extends PanelContainer

## Fold / Check-Call / Raise controls per GODOT_PROTOCOL.md section 4,
## driven entirely by you.legal (section 5): its presence is the
## client's cue that it is this seat's turn, and its fields are the
## only source of truth for what is legal. This component never decides
## legality itself -- only renders what apply_legal() hands it, and the
## sidecar validates every command exactly as it would a remote peer's
## action regardless of what this UI allowed the player to click.

signal fold_pressed
signal check_call_pressed
signal raise_pressed(amount: int)

@onready var _fold_button: Button = %FoldButton
@onready var _check_call_button: Button = %CheckCallButton
@onready var _raise_button: Button = %RaiseButton
@onready var _raise_slider: HSlider = %RaiseSlider
@onready var _raise_amount_label: Label = %RaiseAmountLabel
@onready var _preset_half_pot: Button = %PresetHalfPot
@onready var _preset_two_thirds_pot: Button = %PresetTwoThirdsPot
@onready var _preset_pot: Button = %PresetPot
@onready var _preset_all_in: Button = %PresetAllIn

var _legal: Dictionary = {}


func _ready() -> void:
	_fold_button.pressed.connect(_on_fold_pressed)
	_check_call_button.pressed.connect(_on_check_call_pressed)
	_raise_button.pressed.connect(_on_raise_pressed)
	_raise_slider.value_changed.connect(_on_slider_value_changed)
	_preset_half_pot.pressed.connect(_on_preset_half_pot_pressed)
	_preset_two_thirds_pot.pressed.connect(_on_preset_two_thirds_pot_pressed)
	_preset_pot.pressed.connect(_on_preset_pot_pressed)
	_preset_all_in.pressed.connect(_on_preset_all_in_pressed)


## legal is you.legal from a snapshot (section 5), or {} when it is not
## this seat's turn (phase != "betting" or action_on != this seat). An
## empty dict hides and disables every control, matching the protocol's
## cue that there is nothing to decide right now.
func apply_legal(legal: Dictionary) -> void:
	_legal = legal
	var has_turn := not legal.is_empty()
	visible = has_turn
	_fold_button.disabled = not has_turn
	_check_call_button.disabled = not has_turn
	if not has_turn:
		_set_raise_controls_enabled(false)
		return

	var to_call := int(legal.get("to_call", 0))
	_check_call_button.text = "Check" if to_call <= 0 else "Call %d" % to_call

	var can_raise := bool(legal.get("can_raise", false))
	_set_raise_controls_enabled(can_raise)
	if can_raise:
		var min_to := int(legal.get("min_to", 0))
		var max_to := int(legal.get("max_to", 0))
		_raise_slider.min_value = min_to
		_raise_slider.max_value = max_to
		_raise_slider.value = clamp(_raise_slider.value, min_to, max_to)
		_update_raise_display()


func current_raise_amount() -> int:
	return int(_raise_slider.value)


func _set_raise_controls_enabled(enabled: bool) -> void:
	_raise_button.disabled = not enabled
	_raise_slider.editable = enabled
	_preset_half_pot.disabled = not enabled
	_preset_two_thirds_pot.disabled = not enabled
	_preset_pot.disabled = not enabled
	_preset_all_in.disabled = not enabled


func _update_raise_display() -> void:
	var amount := int(_raise_slider.value)
	_raise_amount_label.text = str(amount)
	_raise_button.text = "Raise to %d" % amount


func _on_fold_pressed() -> void:
	fold_pressed.emit()


func _on_check_call_pressed() -> void:
	check_call_pressed.emit()


func _on_raise_pressed() -> void:
	raise_pressed.emit(current_raise_amount())


func _on_slider_value_changed(_value: float) -> void:
	_update_raise_display()


func _on_preset_half_pot_pressed() -> void:
	_apply_preset(0.5)


func _on_preset_two_thirds_pot_pressed() -> void:
	_apply_preset(2.0 / 3.0)


func _on_preset_pot_pressed() -> void:
	_apply_preset(1.0)


func _on_preset_all_in_pressed() -> void:
	_raise_slider.value = _raise_slider.max_value
	_update_raise_display()


## Pot-fraction presets size the raise as a fraction of the pot AFTER
## calling (pot + to_call) -- e.g. "1/2 pot" adds half of that as the
## raise on top of the call, matching how bet-sizing UIs commonly frame
## a fractional raise. The result is clamped to [min_to, max_to]; the
## sidecar is the actual authority on legality regardless.
func _apply_preset(fraction: float) -> void:
	var pot := int(_legal.get("pot", 0))
	var to_call := int(_legal.get("to_call", 0))
	var pot_after_call := pot + to_call
	var target := to_call + int(round(pot_after_call * fraction))
	var min_to := int(_legal.get("min_to", 0))
	var max_to := int(_legal.get("max_to", 0))
	_raise_slider.value = clamp(target, min_to, max_to)
	_update_raise_display()
