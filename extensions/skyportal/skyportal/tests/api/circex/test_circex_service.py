"""Tests for the `circex` external service (GCN circulars -> GcnEvents).

Two layers: that the service is wired into the Fritz config the way
baselayer's setup_services and the plugin's own load_plugin_config expect, and
that the SkyPortal behaviour the plugin's resolver depends on actually holds.

The plugin writes through SkyPortal's models rather than its REST API, so what
is worth pinning here is the database behaviour it leans on: alias matching that
ignores case and spacing, and the dateobs window it falls back to.
"""

import datetime

import pytest
import sqlalchemy as sa

from baselayer.app.env import load_env
from skyportal.models import DBSession, GcnEvent, Photometry

_, cfg = load_env()


# --- config-shape ---------------------------------------------------------------


def test_circex_external_service_registered():
    external = cfg["services.external"]
    assert "circex" in external, "circex missing from services.external"
    circex = external["circex"]
    assert circex["repo"].endswith("circex-skyportal-plugin.git"), circex["repo"]
    assert circex.get("rev")


def test_circex_params_shape():
    params = cfg["services.external.circex.params"]
    for block in ("skyportal", "writes", "resolver", "extractor", "consumer"):
        assert block in params, f"missing circex params block: {block}"
    assert params["skyportal"]["user_id"] >= 1
    assert params["resolver"]["order"]
    assert params["extractor"]["kind"] in ("regex", "llama", "hybrid")


def test_circex_needs_no_rest_credentials():
    """It writes through the models; an api_token here would mean it does not."""
    params = cfg["services.external.circex.params"]
    assert "api_token" not in params["skyportal"]
    assert "base_url" not in params["skyportal"]
    assert "auth" not in params


def test_circex_ships_switched_off():
    params = cfg["services.external.circex.params"]
    assert params["writes"]["live"] is False, "circex would write on deploy"
    assert params["consumer"]["enabled"] is False, "circex would consume on deploy"


def test_circex_require_fields_matches_the_model():
    """Mispairing silently yields fabricated photometry or nothing at all."""
    extractor = cfg["services.external.circex.params.extractor"]
    model = (extractor.get("llama_model") or "").lower()
    if "mistral" in model:
        assert extractor["llama_require_fields"] is False, model
    elif "qwen" in model:
        assert extractor["llama_require_fields"] is True, model


# --- the SkyPortal behaviour the resolver depends on ----------------------------


def _alias_query(needle):
    """The resolver's rung 1, as the plugin issues it."""
    pattern = f"%{needle.replace(' ', '').lower()}%"
    return sa.select(GcnEvent).where(
        sa.func.replace(
            sa.func.lower(sa.cast(GcnEvent.aliases, sa.String)), " ", ""
        ).like(pattern)
    )


@pytest.mark.parametrize("needle", ["S190814bv", "s190814BV", "S190814 bv"])
def test_alias_match_ignores_case_and_spacing(gcn_GW190814, needle):
    """A circular names the event `S190814bv`; SkyPortal holds `LVC#S190814bv`."""
    found = DBSession().scalars(_alias_query(needle)).unique().all()
    assert gcn_GW190814.dateobs in [e.dateobs for e in found], needle


def test_alias_written_back_makes_the_next_lookup_hit(gcn_GW190814):
    """The first circular resolves by designation and writes its name as an
    alias; every later one is meant to resolve by that alias."""
    assert not DBSession().scalars(_alias_query("GW190814")).unique().all()

    event = DBSession().scalar(
        sa.select(GcnEvent).where(GcnEvent.dateobs == gcn_GW190814.dateobs)
    )
    event.aliases = list(event.aliases or []) + ["GW190814"]
    DBSession().commit()

    found = DBSession().scalars(_alias_query("GW190814")).unique().all()
    assert gcn_GW190814.dateobs in [e.dateobs for e in found]


def test_dateobs_window_finds_the_event(gcn_GW190814):
    """Rung 2. A designation fixes the UTC day but not the time."""
    centre = gcn_GW190814.dateobs
    found = (
        DBSession()
        .scalars(
            sa.select(GcnEvent).where(
                GcnEvent.dateobs >= centre - datetime.timedelta(hours=12),
                GcnEvent.dateobs <= centre + datetime.timedelta(hours=12),
            )
        )
        .unique()
        .all()
    )
    assert centre in [e.dateobs for e in found]


def test_event_exposes_a_localization(gcn_GW190814):
    """sources_in_gcn confirms against a named localization, so the plugin
    skips events that have none."""
    assert gcn_GW190814.localizations
    assert gcn_GW190814.localizations[0].localization_name


def test_photometry_dedup_index_covers_origin():
    """The plugin relies on this index for idempotency instead of tracking what
    it has already written, and tags its rows with origin 'circex'."""
    assert "origin" in Photometry.DEDUP_COLUMNS
    assert "obj_id" in Photometry.DEDUP_COLUMNS
    assert "mjd" in Photometry.DEDUP_COLUMNS
