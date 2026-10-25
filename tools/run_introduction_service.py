"""Run the setup-only service behind a separately reviewed TLS proxy."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal

from holdem.introduction_service import IntroductionService


async def run(args):
    limits = {name: getattr(args, name) for name in
              ("max_rooms", "max_sockets", "max_challenges", "max_retained_bytes") if getattr(args, name) is not None}
    service = IntroductionService(args.service, trusted_proxies=args.trusted_proxy,
                                  proxy_header=args.proxy_header, limits=limits)
    await service.start(args.host, args.port)
    print(json.dumps({"service": args.service, "endpoint": service.endpoint}), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                loop.add_signal_handler(value, stop.set)
                installed.append(value)
            except NotImplementedError:
                pass
    try:
        await stop.wait()
    finally:
        await service.close()
        for value in installed:
            loop.remove_signal_handler(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, help="Configured short service identifier")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    for name in ("rooms", "sockets", "challenges", "retained-bytes"):
        parser.add_argument("--max-" + name, type=int, help="Lower the service's bounded capacity")
    parser.add_argument("--trusted-proxy", action="append", default=[],
                        help="Exact proxy IP allowed to supply the selected client-IP header")
    parser.add_argument("--proxy-header", choices=("x-forwarded-for", "cf-connecting-ip"),
                        default="x-forwarded-for", help="Client-IP header accepted only from exact trusted proxy IPs")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
