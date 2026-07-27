extends GutTest

const BettingControlsScene := preload("res://ui/betting_controls.tscn")


func _controls() -> BettingControls:
	var controls: BettingControls = BettingControlsScene.instantiate()
	add_child_autofree(controls)
	watch_signals(controls)
	return controls


func _legal(overrides: Dictionary = {}) -> Dictionary:
	var base := {
		"to_call": 0, "can_check": true, "can_raise": true,
		"min_to": 20, "max_to": 455, "pot": 90, "your_bet": 0,
	}
	for key in overrides:
		base[key] = overrides[key]
	return base


func test_no_turn_hides_and_disables_everything():
	var controls := _controls()
	controls.apply_legal({})
	assert_false(controls.visible)
	assert_true(controls.get_node("%FoldButton").disabled)
	assert_true(controls.get_node("%CheckCallButton").disabled)
	assert_true(controls.get_node("%RaiseButton").disabled)
	assert_false(controls.get_node("%RaiseSlider").editable)


func test_your_turn_shows_and_enables_fold_and_check_call():
	var controls := _controls()
	controls.apply_legal(_legal())
	assert_true(controls.visible)
	assert_false(controls.get_node("%FoldButton").disabled)
	assert_false(controls.get_node("%CheckCallButton").disabled)


func test_zero_to_call_shows_check_label():
	var controls := _controls()
	controls.apply_legal(_legal({"to_call": 0}))
	assert_eq(controls.get_node("%CheckCallButton").text, "Check")


func test_positive_to_call_shows_call_amount_label():
	var controls := _controls()
	controls.apply_legal(_legal({"to_call": 30}))
	assert_eq(controls.get_node("%CheckCallButton").text, "Call 30")


func test_cannot_raise_disables_raise_controls_only():
	var controls := _controls()
	controls.apply_legal(_legal({"can_raise": false}))
	assert_false(controls.get_node("%FoldButton").disabled)
	assert_false(controls.get_node("%CheckCallButton").disabled)
	assert_true(controls.get_node("%RaiseButton").disabled)
	assert_false(controls.get_node("%RaiseSlider").editable)
	assert_true(controls.get_node("%PresetPot").disabled)


func test_raise_slider_bounds_match_legal_range():
	var controls := _controls()
	controls.apply_legal(_legal({"min_to": 20, "max_to": 455}))
	var slider: HSlider = controls.get_node("%RaiseSlider")
	assert_eq(slider.min_value, 20.0)
	assert_eq(slider.max_value, 455.0)


func test_fold_button_emits_fold_pressed():
	var controls := _controls()
	controls.apply_legal(_legal())
	controls.get_node("%FoldButton").pressed.emit()
	assert_signal_emitted(controls, "fold_pressed")


func test_check_call_button_emits_check_call_pressed():
	var controls := _controls()
	controls.apply_legal(_legal())
	controls.get_node("%CheckCallButton").pressed.emit()
	assert_signal_emitted(controls, "check_call_pressed")


func test_raise_button_emits_raise_pressed_with_slider_value():
	var controls := _controls()
	controls.apply_legal(_legal({"min_to": 20, "max_to": 455}))
	controls.get_node("%RaiseSlider").value = 100
	controls.get_node("%RaiseButton").pressed.emit()
	assert_signal_emitted_with_parameters(controls, "raise_pressed", [100])


func test_pot_preset_sets_slider_to_pot_after_call_and_clamps():
	var controls := _controls()
	# pot=90, to_call=10 -> pot_after_call=100 -> full-pot raise target = 10+100=110
	controls.apply_legal(_legal({"pot": 90, "to_call": 10, "min_to": 20, "max_to": 455}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 110)


func test_half_pot_preset():
	var controls := _controls()
	# pot=90, to_call=10 -> pot_after_call=100 -> half-pot raise = 50 -> target=60
	controls.apply_legal(_legal({"pot": 90, "to_call": 10, "min_to": 20, "max_to": 455}))
	controls.get_node("%PresetHalfPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 60)


func test_preset_clamps_to_max_to():
	var controls := _controls()
	# pot=1000 dwarfs a short max_to -- must clamp, never exceed the legal ceiling.
	controls.apply_legal(_legal({"pot": 1000, "to_call": 0, "min_to": 20, "max_to": 150}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 150)


func test_preset_clamps_to_min_to():
	var controls := _controls()
	# A tiny pot must not produce a target below the legal minimum raise.
	controls.apply_legal(_legal({"pot": 2, "to_call": 0, "min_to": 20, "max_to": 455}))
	controls.get_node("%PresetHalfPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 20)


func test_all_in_preset_maxes_the_slider_and_does_not_emit():
	var controls := _controls()
	controls.apply_legal(_legal({"min_to": 20, "max_to": 455}))
	controls.get_node("%PresetAllIn").pressed.emit()
	assert_eq(controls.current_raise_amount(), 455)
	assert_signal_not_emitted(controls, "raise_pressed")


func test_reapplying_legal_clamps_stale_slider_value_into_new_range():
	var controls := _controls()
	controls.apply_legal(_legal({"min_to": 20, "max_to": 455}))
	controls.get_node("%RaiseSlider").value = 400
	# A later street's legal range can be much narrower (e.g. short stack
	# calling down); the slider must not keep a now-illegal value.
	controls.apply_legal(_legal({"min_to": 20, "max_to": 60}))
	assert_eq(controls.current_raise_amount(), 60)


## --- your_bet / re-raise preset tests ---
## Raise targets are absolute (total wagered this street), so the formula
## must use current_bet = your_bet + to_call, not to_call alone.
## Tests 1-4 fail with the old formula; 5-7 are regression / clamp coverage.

func test_pot_preset_reraise_uses_current_bet_as_base():
	var controls := _controls()
	# 3-bet scenario: you opened to 50, villain 3-bet, you now face 100 more.
	# pot=300, to_call=100, your_bet=50 -> current_bet=150, pot_after_call=400
	# Full-pot raise target = 150 + 400 = 550.
	controls.apply_legal(_legal({"pot": 300, "to_call": 100, "your_bet": 50,
			"min_to": 200, "max_to": 2000}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 550)


func test_half_pot_preset_reraise():
	var controls := _controls()
	# Same 3-bet setup. Half-pot target = 150 + round(400*0.5) = 350.
	controls.apply_legal(_legal({"pot": 300, "to_call": 100, "your_bet": 50,
			"min_to": 200, "max_to": 2000}))
	controls.get_node("%PresetHalfPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 350)


func test_two_thirds_pot_preset_reraise():
	var controls := _controls()
	# Same 3-bet setup. 2/3-pot target = 150 + round(400*2/3) = 150+267 = 417.
	controls.apply_legal(_legal({"pot": 300, "to_call": 100, "your_bet": 50,
			"min_to": 200, "max_to": 2000}))
	controls.get_node("%PresetTwoThirdsPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 417)


func test_pot_preset_reraise_alternate_sizing():
	var controls := _controls()
	# Different chip counts to confirm no magic-number dependency.
	# pot=200, to_call=80, your_bet=40 -> current_bet=120, pot_after_call=280
	# Full-pot target = 120 + 280 = 400.
	controls.apply_legal(_legal({"pot": 200, "to_call": 80, "your_bet": 40,
			"min_to": 100, "max_to": 2000}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 400)


func test_your_bet_zero_pot_preset_regression():
	var controls := _controls()
	# Regression: with your_bet=0 the formula must still match the original
	# behaviour. pot=90, to_call=10, your_bet=0 -> target = 10 + 100 = 110.
	controls.apply_legal(_legal({"pot": 90, "to_call": 10, "your_bet": 0,
			"min_to": 20, "max_to": 455}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 110)


func test_reraise_preset_clamps_to_max_to():
	var controls := _controls()
	# Large pot -- correct target exceeds max_to and must be clamped.
	# pot=800, to_call=100, your_bet=100, max_to=600
	# current_bet=200, pot_after_call=900, unclamped target=1100 -> 600.
	controls.apply_legal(_legal({"pot": 800, "to_call": 100, "your_bet": 100,
			"min_to": 200, "max_to": 600}))
	controls.get_node("%PresetPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 600)


func test_reraise_preset_clamps_to_min_to():
	var controls := _controls()
	# Tiny pot -- target falls below the legal minimum raise; must clamp up.
	# pot=10, to_call=5, your_bet=5 -> current_bet=10, pot_after_call=15
	# half-pot target = 10 + round(7.5) = 18 -> clamped to min_to=100.
	controls.apply_legal(_legal({"pot": 10, "to_call": 5, "your_bet": 5,
			"min_to": 100, "max_to": 500}))
	controls.get_node("%PresetHalfPot").pressed.emit()
	assert_eq(controls.current_raise_amount(), 100)
