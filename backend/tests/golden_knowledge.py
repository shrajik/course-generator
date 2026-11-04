"""A fixed corpus and queries with the passages the library must offer for them.

This is the parity oracle for the knowledge-base migration: every backend must
return the same references for the same queries (a different embedding model
may reorder near-ties, so each query pins the *top* reference, or pins that
nothing is offered). The Postgres backend is compared against these in shadow
mode before cutover.
"""

from __future__ import annotations

from tests.sample_files import POLICY_TEXT

COOKING = (
    b"COOKING PASTA\n\nBoil the pasta for ten minutes in plenty of salted water, then drain it well.\n\n"
    b"1.1 Sauces\n\nSimmer the tomato sauce slowly with garlic and fresh basil until it thickens.\n"
)
SECURITY = (
    b"PASSWORD STANDARD\n\nPasswords must contain at least fourteen characters and must be rotated "
    b"every ninety days across all corporate systems.\n"
)

# Upload order fixes the permanent numbers: policy = G1, cooking = G2, security = G3.
CORPUS = (("policy.txt", POLICY_TEXT), ("cooking.txt", COOKING), ("security.txt", SECURITY))

# query -> reference that must be ranked first, or None when nothing may be offered.
# Pinned from the file backend on 2026-11-02 (offline mock embeddings, so keyword
# ranking decides): a characterisation, not a claim that each is the ideal answer -
# "how long do refunds take" ranks the eligibility passage first today.
GOLDEN: dict[str, str | None] = {
    "how long do refunds take after the product is returned": "G1.2",
    "refund eligibility for returned products": "G1.2",
    "boil pasta and drain it": "G2.1",
    "how often must passwords be rotated": "G3.1",
    "tomato sauce with garlic and basil": "G2.2",
    "billing team escalation": "G1.3",
    # Off-subject or too thinly matched: the library must offer nothing.
    "minimum password length": None,
    "quantum chromodynamics of gluons": None,
}
