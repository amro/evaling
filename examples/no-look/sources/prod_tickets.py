"""A stand-in for a production API you are not allowed to read.

Pretend this queries your real ticket store. The rows carry names, emails, and
order numbers — exactly the material that makes a normal eval run impossible to
share, and that no-look mode keeps out of every artifact.

Generated deterministically from a seed so this example is reproducible, and so
you can verify for yourself that none of it reaches disk:

    evaling run
    grep -r "@example.com" .evaling/     # finds nothing

A real implementation is the same shape: page your API, hand back Cases.
"""

import random

from evaling import BaseCaseSource, Case, CasePage

FIRST = ["Dana", "Kwame", "Yuki", "Priya", "Tomas", "Aisha", "Lars", "Mei"]
LAST = ["Okafor", "Nakamura", "Silva", "Kaur", "Novak", "Haddad", "Berg", "Chen"]
COMPLAINTS = [
    "was charged twice for order {order}, please refund",
    "cannot log in since the password reset, account {order}",
    "the invoice for order {order} has the wrong VAT",
    "export is throwing a 500, this is blocking production",
    "please cancel the subscription on account {order}",
]


class ProdTickets(BaseCaseSource):
    """Pages through synthetic 'production' tickets."""

    def __init__(self, total: int = 250, seed: int = 7, page_size_hint: int = 50):
        self.total = total
        self.seed = seed
        self.fetches = 0

    def fetch(self, cursor: str | None, limit: int) -> CasePage:
        self.fetches += 1
        start = int(cursor or 0)
        stop = min(start + limit, self.total)
        rng = random.Random(self.seed)
        cases = []
        for i in range(start, stop):
            # Deterministic per-row values, independent of paging.
            row = random.Random(f"{self.seed}-{i}")
            first, last = row.choice(FIRST), row.choice(LAST)
            order = f"ORD-{row.randrange(10**5, 10**6)}"
            body = row.choice(COMPLAINTS).format(order=order)
            cases.append(
                Case(
                    # Even the id is identifying — no-look hashes it by default.
                    id=f"{first.lower()}.{last.lower()}@example.com",
                    vars={
                        "customer": f"{first} {last}",
                        "ticket": f"Hi, this is {first} {last}. I {body}.",
                    },
                    expected="acknowledged",
                )
            )
        del rng
        return CasePage(cases=cases, cursor=str(stop) if stop < self.total else None)

    def count(self) -> int:
        """Optional — lets evaling show a real progress total."""
        return self.total


def make_source(total: int = 250, seed: int = 7):
    return ProdTickets(total=total, seed=seed)
