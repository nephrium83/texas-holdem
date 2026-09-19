# Signed-envelope journaling

Status: implementation workspace; not yet implemented or accepted.

Owner-approved goal:

> The signed envelope bytes are durably journalled, byte-identically, before any local
> state mutation that depends on them.

This PR is one vertical ordering fix for the production signed transport and
its local producers. It requires exact-byte and durable-before-apply tests,
including deliberate-break controls. It does not implement suspension,
reconnect, crash recovery, abandonment, deal context v3, or rules-profile
binding, and it changes none of their pending policy decisions.

Implementation details and verified test evidence will replace this workspace
status when the code is independently reviewed. No merge is authorized by
creating this workspace.
