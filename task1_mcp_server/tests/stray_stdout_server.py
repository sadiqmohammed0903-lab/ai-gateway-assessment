"""Test-only launcher: a server whose tool handler writes junk to stdout, to prove isolation."""

import os

from task1_mcp_server import server


def noisy_get(customer_id: str):
    print("STRAY PRINT FROM A THIRD PARTY LIBRARY")  # noqa: T201
    os.write(1, b"STRAY RAW FD WRITE\n")
    return original_get(customer_id)


original_get = server.store.get
server.store.get = noisy_get  # type: ignore[method-assign]

if __name__ == "__main__":
    server.main()
