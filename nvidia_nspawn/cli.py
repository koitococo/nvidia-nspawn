"""Host CLI; JSON on stdout, actionable failures on stderr."""

import argparse
import json
import sys

from . import runtime
from .common import Error, require_root
from .verify import verify


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nvidia-nspawn",
        description="Inject NVIDIA devices into a running systemd-nspawn container without restarting it.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "attach", "status", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--machine", required=True)
        if name in ("plan", "attach"):
            command.add_argument("--gpu", required=True, help="Full GPU UUID (GPU-...)")
        if name == "verify":
            command.add_argument("--user", required=True, help="Existing non-root container user")
            command.add_argument("--cuda-sample", required=True,
                                 help="Absolute container path to NVIDIA vectorAdd")
    args = parser.parse_args()
    try:
        require_root()
        if args.command == "verify":
            health = runtime.status(args.machine)
            if not health["healthy"]:
                raise Error("Attachment is not healthy: " + json.dumps(health))
            state = runtime.load_state(args.machine)
            result = verify(health["target"], args.user, args.cuda_sample, state)
        elif args.command == "status":
            result = runtime.status(args.machine)
        else:
            result = getattr(runtime, args.command)(args.machine, args.gpu)
        print(json.dumps(result, indent=2))
        return 0
    except (Error, OSError, ValueError, KeyError) as exc:
        print(f"nvidia-nspawn: {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
