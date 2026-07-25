class_name CardFormat
extends RefCounted

## Card-string parsing and display helpers per GODOT_PROTOCOL.md section 3:
## rank = all but the last character, suit = last character. Ten is "10",
## not "T", so a card string is 2-3 characters -- never assume a fixed
## length. This is the shared implementation; test_smoke.gd separately
## pins the same parse rule inline as an independent smoke check, so the
## two intentionally do not share code.

const SUIT_GLYPHS := {
	"c": "♣",
	"d": "♦",
	"h": "♥",
	"s": "♠",
}

const RED_SUITS := ["d", "h"]


static func rank(card: String) -> String:
	return card.substr(0, card.length() - 1)


static func suit(card: String) -> String:
	return card.substr(card.length() - 1)


static func glyph(card: String) -> String:
	return SUIT_GLYPHS.get(suit(card), "?")


static func is_red(card: String) -> bool:
	return suit(card) in RED_SUITS


static func display_text(card: String) -> String:
	return "%s%s" % [rank(card), glyph(card)]
