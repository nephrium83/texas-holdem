extends VBoxContainer


func _ready() -> void:
	$Play.pressed.connect(_play)
	if "--self-test" in OS.get_cmdline_user_args():
		var success := _play()
		get_tree().quit(0 if success else 1)


func _play() -> bool:
	var python := OS.get_environment("POKER_PYTHON")
	if python.is_empty():
		python = "python3" if OS.has_feature("windows") == false else "python"
	var examples := ProjectSettings.globalize_path("res://").trim_suffix("/").get_base_dir()
	var output: Array = []
	# This short, bounded example hand runs as one blocking child process.
	# OS.execute returns after the child has exited and released its table.
	var exit_code := OS.execute(python, [examples.path_join("godot_tavern_hand.py")], output, true)
	if exit_code != 0 or output.is_empty():
		return _failed("Could not run the poker example. Set POKER_PYTHON to a Python interpreter with holdem installed.", output)
	var result: Variant = JSON.parse_string(output[0])
	if not result is Dictionary or result.get("phase") != "closed":
		return _failed("The poker process did not return a closed table.", output)
	var balances: Dictionary = result["balances"]
	$Result.text = "Hand settled after %d actions.\nRanger: %d chips   Innkeeper: %d chips\nTable closed. Play again to start a fresh visit." % [
		result["actions"], balances["ranger"], balances["innkeeper"]
	]
	if "--self-test" in OS.get_cmdline_user_args():
		print(JSON.stringify(result))
	return true


func _failed(message: String, output: Array) -> bool:
	$Result.text = message
	push_error(message + " " + str(output))
	return false
