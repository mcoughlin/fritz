"""Tests for the `circex` external service (GCN circulars -> GcnEvents).

Two layers:

* config-shape checks that the service is wired into the Fritz config the way
  baselayer's setup_services and the plugin's own load_plugin_config expect
  (repo + rev + params), mirroring how `osg` and `boom` are configured; and
* contract checks against a real GcnEvent for the SkyPortal endpoints the
  plugin resolves events with. Unlike `osg`, circex is not an AnalysisService:
  it has no callback_url round-trip to mock. What it does have is a resolver
  that leans on `partialdateobs` matching a substring of `GcnEvent.aliases`,
  and on the alias it writes back making the next lookup hit. Those are
  assumptions about SkyPortal's API, so they are what is worth pinning here.
"""

from datetime import timedelta

from baselayer.app.env import load_env
from skyportal.tests import api

_, cfg = load_env()


# --- config-shape ---------------------------------------------------------------


def test_circex_external_service_registered():
    external = cfg["services.external"]
    assert "circex" in external, "circex missing from services.external"
    circex = external["circex"]
    assert circex["repo"].endswith("circex-skyportal-plugin.git"), circex["repo"]
    assert circex.get("rev")


def test_circex_params_shape():
    # The plugin reads this exact dotted key; if it's missing it crashes on boot.
    params = cfg["services.external.circex.params"]

    for block in (
        "listener",
        "skyportal",
        "writes",
        "resolver",
        "extractor",
        "consumer",
        "auth",
    ):
        assert block in params, f"missing circex params block: {block}"

    for key in ("base_url", "api_token", "group_ids", "default_instrument_id"):
        assert key in params["skyportal"], f"missing skyportal.{key}"

    # The resolver tries these rungs in order; an empty list resolves nothing.
    assert params["resolver"]["order"]
    assert params["extractor"]["kind"] in ("regex", "llama", "hybrid")
    assert "incoming_bearer_token" in params["auth"]


def test_circex_ships_switched_off():
    """Both write paths default to off, so merging the config changes nothing."""
    params = cfg["services.external.circex.params"]
    assert params["writes"]["live"] is False, "circex would write on deploy"
    assert params["consumer"]["enabled"] is False, "circex would consume on deploy"


def test_circex_require_fields_matches_the_model():
    """Requiring the grammar's fields is model-specific and silently wrong if
    mispaired: Mistral pads the photometry array with fabricated rows when they
    are required, and Qwen returns an empty object when they are not."""
    extractor = cfg["services.external.circex.params.extractor"]
    model = (extractor.get("llama_model") or "").lower()
    if "mistral" in model:
        assert extractor["llama_require_fields"] is False, model
    elif "qwen" in model:
        assert extractor["llama_require_fields"] is True, model


# --- resolver contract against a real GcnEvent ----------------------------------


def _events(data):
    return data["data"]["events"]


def test_partialdateobs_matches_an_alias_substring(gcn_GW190814, view_only_token):
    """Resolver rung 1. A circular names the event `S190814bv`; SkyPortal holds
    it as `LVC#S190814bv`, and the lookup has to match on the substring."""
    status, data = api(
        "GET",
        "gcn_event",
        params={"partialdateobs": "S190814bv", "numPerPage": 50},
        token=view_only_token,
    )
    assert status == 200, data
    found = [e for e in _events(data) if e["dateobs"].startswith("2019-08-14")]
    assert found, f"S190814bv did not resolve to the event: {_events(data)}"


def test_alias_written_back_makes_the_next_lookup_hit(gcn_GW190814, super_admin_token):
    """The first circular of an event resolves by designation and writes its
    name as an alias; every later one is meant to resolve by that alias."""
    dateobs = gcn_GW190814.dateobs.isoformat()
    status, data = api(
        "POST",
        f"gcn_event/{dateobs}/alias",
        data={"alias": "GW190814"},
        token=super_admin_token,
    )
    assert status == 200, data

    status, data = api(
        "GET",
        "gcn_event",
        params={"partialdateobs": "GW190814", "numPerPage": 50},
        token=super_admin_token,
    )
    assert status == 200, data
    assert [e for e in _events(data) if e["dateobs"].startswith("2019-08-14")], data


def test_alias_match_ignores_case(gcn_GW190814, view_only_token):
    """Circulars write `S190814bv`; notices and TACH write it uppercased."""
    status, data = api(
        "GET",
        "gcn_event",
        params={"partialdateobs": "s190814BV", "numPerPage": 50},
        token=view_only_token,
    )
    assert status == 200, data
    assert [e for e in _events(data) if e["dateobs"].startswith("2019-08-14")], data


def test_date_window_finds_the_event(gcn_GW190814, view_only_token):
    """Resolver rung 2. A designation fixes the burst's UTC day but not its
    time, so the fallback searches a window around it."""
    dateobs = gcn_GW190814.dateobs
    status, data = api(
        "GET",
        "gcn_event",
        params={
            "startDate": (dateobs - timedelta(hours=12)).isoformat(),
            "endDate": (dateobs + timedelta(hours=12)).isoformat(),
            "numPerPage": 50,
        },
        token=view_only_token,
    )
    assert status == 200, data
    assert [e for e in _events(data) if e["dateobs"].startswith("2019-08-14")], data


def test_event_exposes_a_localization_name(gcn_GW190814, view_only_token):
    """`sources_in_gcn` confirms a source against a named localization, so the
    plugin skips events that have none. The GET must expose the name."""
    status, data = api(
        "GET", f"gcn_event/{gcn_GW190814.dateobs.isoformat()}", token=view_only_token
    )
    assert status == 200, data
    localizations = data["data"]["localizations"]
    assert localizations, "event has no localization"
    assert localizations[0].get("localization_name")
