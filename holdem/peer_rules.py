"""Shared casual peer-table configuration for LAN and Internet hosts."""
from .p2p.casual_clock import PROFILE
from .p2p.session import Session


def table_settings(settings: dict) -> dict:
    values = {"sb": 25, "bb": 50, "stack": 1000,
              "structure": "No-Limit", "deal_policy": Session.DEAL_POLICY_BG,
              "continuation_policy": Session.CONTINUATION_SURVIVORS,
              "casual_profile": PROFILE}
    values.update(settings)
    for field in ("sb", "bb", "stack"):
        if type(values[field]) is not int:
            raise ValueError(f"{field} must be a whole number")
    if values["sb"] <= 0 or values["bb"] <= values["sb"]:
        raise ValueError("Blinds must be positive and the big blind must be larger")
    if values["stack"] < values["bb"]:
        raise ValueError("Starting chips must cover the big blind")
    if values["structure"] not in ("No-Limit", "Pot-Limit", "Fixed-Limit"):
        raise ValueError("Unsupported betting structure")
    if values["deal_policy"] != Session.DEAL_POLICY_BG:
        raise ValueError("Peer games require Bayer-Groth dealing")
    if values["continuation_policy"] != Session.CONTINUATION_SURVIVORS:
        raise ValueError("Casual peer games require survivor continuation")
    if values["casual_profile"] != PROFILE:
        raise ValueError("All players need the same Poker Preview version")
    return values
