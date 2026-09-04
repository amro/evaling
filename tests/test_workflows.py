"""CI workflow invariants that break quietly.

A workflow is not exercised by the suite and, in `publish.yml`'s case, not
exercised at all until a release fires. What it does wrong is therefore found
late, holding a credential.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
SHA = re.compile(r"^[0-9a-f]{40}$")


def uses(path: Path) -> list[str]:
    """Every `uses:` reference in a workflow, in file order."""
    return re.findall(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)


class TestPublishIsPinnedToImmutableRefs:
    """`publish.yml` runs with id-token: write and can publish as evaling.

    A tag is mutable, so `@v1` is a standing grant to whoever can move it. The
    other workflows hold no credentials, so they stay on tags deliberately —
    the same compromise costs them a red build, not a release.
    """

    def test_every_action_is_a_commit_sha(self):
        unpinned = [
            ref for ref in uses(WORKFLOWS / "publish.yml") if not SHA.match(ref.split("@", 1)[-1])
        ]
        assert not unpinned, (
            "publish.yml can publish to PyPI, so its actions must be pinned to "
            f"commit SHAs rather than mutable tags: {unpinned}"
        )

    def test_each_pin_records_the_ref_it_came_from(self):
        """A bare SHA is unreviewable; the comment is what makes it auditable."""
        text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")
        bare = [
            line.strip()
            for line in text.splitlines()
            if re.search(r"uses:\s*\S+@[0-9a-f]{40}\s*$", line)
        ]
        assert not bare, f"pinned without a version comment: {bare}"


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_workflows_reference_actions_by_owner_and_repo(workflow):
    """A `uses:` with no owner is a local path; typos read as one."""
    for ref in uses(workflow):
        assert "/" in ref or ref.startswith("./"), f"{workflow.name}: odd reference {ref!r}"
