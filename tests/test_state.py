"""`backglass state` — ground truth, with the derivation of every claim.

The tests are about the three properties that make the answer worth trusting, not about
the particular numbers, which are whatever the ledger happens to hold: every claim names
how it was derived, a probe that cannot run says so instead of reading as zero, and one
broken probe does not blank the rest of the answer.
"""

from __future__ import annotations

import json
import sqlite3

from backglass import state as state_mod
from backglass.config import Settings


class TestEveryClaimIsCheckable:
    def test_every_claim_names_its_derivation(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole point. A number with no derivation is one a reader has to trust;
        one that names its query is one they can re-run."""
        snapshot = state_mod.collect(conn, settings)
        assert snapshot.sections, "the snapshot is not empty"
        for section, claims in snapshot.sections.items():
            assert claims, f"{section} has no claims"
            for name, claim in claims.items():
                assert claim.how.strip(), f"{section}.{name} does not say how it was derived"

    def test_the_json_is_parseable_and_carries_how(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`--json` is the surface written for a program to read, so its shape is part
        of the contract rather than a rendering detail."""
        parsed = json.loads(state_mod.as_json(state_mod.collect(conn, settings)))
        for claims in parsed.values():
            for claim in claims.values():
                assert "value" in claim and "how" in claim


class TestUnknownIsAValue:
    def test_a_probe_that_cannot_run_says_so_rather_than_reporting_zero(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch
    ) -> None:
        """The failure this module exists to prevent is a confident answer assembled
        from a missing input. An uninstalled app is a legitimate state, and it must not
        render as "matches source: False" — which would read as *stale*, the opposite
        of the truth."""
        monkeypatch.setattr(state_mod, "INSTALLED_APP", conn_path := _missing_path())
        snapshot = state_mod.collect(conn, settings)
        app = snapshot.sections["deployed"]["app"]
        assert app.unknown == "not installed"
        assert app.value is None
        assert "matches_source" not in snapshot.sections["deployed"], (
            "an absent app must not be reported as a mismatch"
        )
        del conn_path

    def test_an_empty_telemetry_table_is_named_not_silent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A fresh ledger has made no model calls. Reporting `{}` with no explanation
        would read as "the pipeline is doing nothing", which is a different claim."""
        snapshot = state_mod.collect(conn, settings)
        calls = snapshot.sections["pipeline"]["model_calls"]
        assert calls.unknown and "fills from the first sync" in calls.unknown


class TestOneBrokenProbeDoesNotBlankTheAnswer:
    def test_a_raising_probe_is_reported_by_name(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch
    ) -> None:
        """The first version of this module swallowed a KeyError into an anonymous
        lambda, so the report said "a lambda raised" and every other section printed
        happily around the hole. A reader has to know WHICH part of the answer is
        missing, or a partial truth is worse than none."""

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("the ledger probe fell over")

        monkeypatch.setattr(state_mod, "_ledger", boom)
        snapshot = state_mod.collect(conn, settings)
        assert "ledger" in snapshot.sections["errors"]
        assert "fell over" in (snapshot.sections["errors"]["ledger"].unknown or "")
        # And the rest of the answer survived.
        assert "schema" in snapshot.sections
        assert "knowledge_base" in snapshot.sections


class TestTheDeployedComparison:
    """A version number has to be maintained and can lie; a hash cannot.

    This is the claim that motivated the module. The desktop app freezes templates and
    CSS into its bundle, so the running app can be arbitrarily far behind the checkout
    with nothing on either side saying so — and the only way it was ever noticed was by
    rebuilding and looking at the result.
    """

    def _bundle(self, root, matching: bool):  # type: ignore[no-untyped-def]
        """A fake install: the frozen surfaces copied from the repo, optionally with one
        byte changed to stand for a build that has fallen behind."""
        from backglass.config import REPO_ROOT

        internal = root / "Contents/Resources/sidecar/backglass-server/_internal"
        for relative in state_mod.FROZEN_SURFACES:
            target = internal / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (REPO_ROOT / relative).read_text()
            target.write_text(body if matching else body + "\n/* older build */\n")
        return root

    def test_a_bundle_built_from_this_checkout_matches(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(state_mod, "INSTALLED_APP", self._bundle(tmp_path, True))
        deployed = state_mod.collect(conn, settings).sections["deployed"]
        assert deployed["matches_source"].value is True
        assert deployed["stale_surfaces"].value == []

    def test_one_changed_byte_is_enough_to_report_stale(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The failure mode is a bundle that is *almost* current — a rebuild that missed
        one file reads as fine under any check coarser than this."""
        monkeypatch.setattr(state_mod, "INSTALLED_APP", self._bundle(tmp_path, False))
        deployed = state_mod.collect(conn, settings).sections["deployed"]
        assert deployed["matches_source"].value is False
        assert set(deployed["stale_surfaces"].value) == set(state_mod.FROZEN_SURFACES)


def _missing_path():  # type: ignore[no-untyped-def]
    from pathlib import Path

    return Path("/nonexistent/Backglass.app")
