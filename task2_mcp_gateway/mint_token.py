"""CLI: python -m task2_mcp_gateway.mint_token --role admin"""

import argparse
import os
import sys

from task2_mcp_gateway.auth import mint_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a test JWT for the MCP gateway")
    parser.add_argument(
        "--role",
        default="viewer",
        help="admin or viewer (anything else is rejected by the gateway)",
    )
    parser.add_argument(
        "--ttl", type=int, default=3600, help="lifetime in seconds (negative = already expired)"
    )
    parser.add_argument(
        "--secret", default=os.environ.get("JWT_SECRET"), help="defaults to $JWT_SECRET"
    )
    args = parser.parse_args()
    if not args.secret:
        sys.stderr.write("JWT_SECRET is not set; pass --secret or export it\n")
        return 2
    sys.stdout.write(mint_token(args.role, args.secret, args.ttl) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
