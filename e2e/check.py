#!/usr/bin/env python3
"""
Check an end-to-end run: every line of EXPECTED must appear in the output of
`agentltl trace` (TRACE), in order.

    python3 check.py TRACE EXPECTED

EXPECTED holds one expected piece of the trace per line, such as
``DENY  [no-sudo] Bash sudo ls``; blank lines and lines starting with # are skipped. Exits 1,
listing what is missing, when one is not found.
"""

import sys


def main(trace_path: str, expected_path: str) -> int:
    with open(trace_path, encoding="utf-8") as fh:
        trace = fh.read()
    with open(expected_path, encoding="utf-8") as fh:
        expected = [line.rstrip("\n") for line in fh
                    if line.strip() and not line.lstrip().startswith("#")]
    missing, at = [], 0
    for item in expected:
        found = trace.find(item, at)
        if found < 0:
            missing.append(item)
        else:
            at = found + len(item)
    if missing:
        print("FAILED: the trace lacks, in this order:", *missing, sep="\n  ")
        print("\n--- agentltl trace ---\n" + trace)
        return 1
    print(f"OK: {len(expected)} expected interventions, in order.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
