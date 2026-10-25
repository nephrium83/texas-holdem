# Direct setup service

This component exchanges authenticated room descriptors and encrypted connection
setup. It does not receive a complete invite or its private admission capability,
decrypt descriptions, run poker, or forward gameplay. Closing it affects pending
joins; established direct connections are owned by the clients.

From the repository root, with the reviewed Python runtime and `cryptography`
plus `websockets==16.1.1` installed:

```powershell
python -m tools.run_introduction_service --service poker --host 127.0.0.1 --port 8766
```

This command starts a new foreground service. No deployment or public endpoint
has been configured by these source files. Production requires a reviewed WSS
reverse proxy forwarding `/v1` to the private listener. Clients select its
trusted URL by service identifier; invitations cannot choose arbitrary URLs.
Plain `ws` is accepted by the client only in explicitly injected loopback tests.

If the reverse proxy is on a separately trusted address, pass its exact socket
address with `--trusted-proxy`. The default `--proxy-header x-forwarded-for`
requires that proxy to overwrite `X-Forwarded-For` with **one** validated client
IP. Cloudflare normally appends to an existing forwarding chain; a tunnel alone
does not establish this overwrite contract. For a private listener reached only
through the reviewed Cloudflare Tunnel, select `--proxy-header cf-connecting-ip`
and trust only the exact observed `cloudflared` container IP. Cloudflare supplies
that consistent single-address header. [Cloudflare header reference](https://developers.cloudflare.com/fundamentals/reference/http-headers/#x-forwarded-for).

The selected header must occur exactly once and contain one valid IP. Missing,
duplicate or comma-separated values fail closed. Other forwarding headers are
ignored, and all such headers are ignored when the socket peer is not an exact
configured proxy IP. Keep the listener private to that proxy; do not expose an
unauthenticated forwarded-header boundary. Verify real proxy attribution and
spoofed/multiple-header rejection through the actual route before publication.

The server caps rooms at 128, total accepted sockets at 512, pending challenges
at 128 and pending guests per room at eight. JSON is limited to 256 KiB, depth
eight and 256 aggregate members; duplicates and nonfinite numbers are refused.
Each socket has at most 16 pending sends and 1 MiB queued data; total retained
records and queued data are capped at 64 MiB. IP, host, room and guest rates use
at most 4,096 expiring token buckets. Compression is disabled, sends have a
five-second timeout, and incomplete WebSocket handshakes have a five-second
timeout. Signed host registrations expire after at most 30 minutes, a renewable
host lease lasts 60 seconds, challenges last 30 seconds, and guest attempts last
at most 90 seconds. A successful `{type:renew}` receives `{type:renewed}`.

`GET /healthz` returns counts only. Application messages, routing tokens,
descriptions and raw exceptions are not logged. Graceful interrupt closes the
owned listener, setup sockets, sender tasks and retained room state. The async
test API offers the same cleanup through `await service.aclose()`.

Run focused acceptance with:

```powershell
python -m pytest tests/test_introduction_service.py -q
```
