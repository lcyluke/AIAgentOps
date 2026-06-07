"""Context Injection — produce injection text for agent hooks (§6.3).

Queries the blackboard digest and active claims, then returns a combined
prompt fragment suitable for prepending to an agent's system/user prompt.

Used by apex-hook.sh to inject cross-agent context before each agent turn.

Usage:
  python3 -m apex.core.injector <agent> [options]
  or programmatically:
    from apex.core.injector import inject
    print(inject("architect"))
"""

from __future__ import annotations

import argparse
import sys

from apex.core.blackboard import Blackboard
from apex.core.claims import ClaimsRegistry


def inject(
    agent: str,
    exclude_author: str | None = None,
    blackboard_max_tokens: int = 800,
    include_claims: bool = True,
    include_blackboard: bool = True,
) -> str:
    """Produce injection text for an agent's context window.

    Queries the blackboard digest and active claims, returning a combined
    prompt fragment that apex-hook.sh prepends to the agent prompt.

    Args:
        agent: The agent name receiving the injection.
        exclude_author: Author to exclude from blackboard digest
                        (defaults to *agent* — agents don't need to re-read
                        their own conclusions).
        blackboard_max_tokens: Soft token cap for the blackboard section.
        include_claims: Whether to include the claims digest.
        include_blackboard: Whether to include the blackboard digest.

    Returns:
        Injection text ready for STDOUT.
    """
    bb = Blackboard()
    claims_reg = ClaimsRegistry()

    sections: list[str] = []

    # Header
    sections.append("<!-- BEGIN APEX CROSS-AGENT CONTEXT -->")

    # Blackboard digest
    if include_blackboard:
        digest = bb.digest(
            exclude_author=exclude_author if exclude_author is not None else agent,
            max_tokens=blackboard_max_tokens,
        )
        sections.append(digest)

    # Claims digest
    if include_claims:
        claims_text = claims_reg.claims_digest()
        sections.append(claims_text)

    sections.append("<!-- END APEX CROSS-AGENT CONTEXT -->")

    return "\n\n".join(sections)


# ── CLI entry point ──────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> None:
    """CLI: inject <agent> — output injection text to STDOUT."""
    parser = argparse.ArgumentParser(
        prog="apex-inject",
        description="Produce cross-agent context injection for an agent",
    )
    parser.add_argument(
        "agent",
        help="Agent name to inject context for",
    )
    parser.add_argument(
        "--exclude-author",
        default=None,
        help="Author to exclude from blackboard digest (default: same as agent)",
    )
    parser.add_argument(
        "--blackboard-max-tokens",
        type=int,
        default=800,
        help="Soft token cap for blackboard section (default: 800)",
    )
    parser.add_argument(
        "--no-claims",
        action="store_true",
        help="Omit the claims digest section",
    )
    parser.add_argument(
        "--no-blackboard",
        action="store_true",
        help="Omit the blackboard digest section",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON instead of plain text",
    )

    args = parser.parse_args(argv)

    result = inject(
        agent=args.agent,
        exclude_author=args.exclude_author,
        blackboard_max_tokens=args.blackboard_max_tokens,
        include_claims=not args.no_claims,
        include_blackboard=not args.no_blackboard,
    )

    if args.json_output:
        import json

        json.dump({"agent": args.agent, "injection": result}, sys.stdout)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(result)
        sys.stdout.write("\n")


if __name__ == "__main__":
    _main()
