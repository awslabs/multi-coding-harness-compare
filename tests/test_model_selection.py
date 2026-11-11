# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Catalogue-validated model and planning selection for comparisons and judge."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from arena.api import create_app
from arena.auth import access_token
from arena.config import Config, HarnessConfig
from arena.judge_models import catalogue_fingerprint
from arena.service import Service, harness_catalogue_fingerprint


def _cat(config, **harnesses):
    """Build a fingerprint-matched harness model catalogue for the given config."""
    return {
        "id": "model-catalogue",
        "fingerprint": harness_catalogue_fingerprint(config),
        "checked_at": 1.0,
        "harnesses": harnesses,
    }


def _judge_cat(config, model_ids):
    """Build a fingerprint-matched judge catalogue containing the given model IDs."""
    return {
        "id": "judge-model-catalogue",
        "fingerprint": catalogue_fingerprint(config),
        "checked_at": 1.0,
        "models": [{"id": mid, "selectable": True, "streaming": True} for mid in model_ids],
    }


class HarnessModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"))
        self.service = Service(self.cfg)

    # 1. Explicit valid pick -> stored in resolved_models
    def test_explicit_valid_pick_is_stored(self):
        self.service.store.put("system", _cat(self.cfg,
            claude={"models": [{"id": "us.anthropic.claude-sonnet-4-6"}]},
            codex={"models": []}, kiro={"models": []}))
        c = self.service.create("Build a CLI", models={"claude": "us.anthropic.claude-sonnet-4-6"})
        self.assertEqual(c["models"]["claude"], "us.anthropic.claude-sonnet-4-6")

    # 2. Explicit unknown model -> error
    def test_explicit_unknown_pick_is_rejected(self):
        self.service.store.put("system", _cat(self.cfg,
            claude={"models": [{"id": "known-model"}]},
            codex={"models": []}, kiro={"models": []}))
        with self.assertRaisesRegex(ValueError, "unknown-model"):
            self.service.create("Build a CLI", models={"claude": "unknown-model"})

    # 3. Untouched picker with configured default present in catalogue -> stored
    def test_configured_default_present_in_catalogue_is_stored(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data2"),
                     harnesses={"claude": HarnessConfig(model="us.anthropic.claude-sonnet-4-6"),
                                "codex": HarnessConfig(), "kiro": HarnessConfig()})
        service = Service(cfg)
        service.store.put("system", _cat(cfg,
            claude={"models": [{"id": "us.anthropic.claude-sonnet-4-6"}]},
            codex={"models": []}, kiro={"models": []}))
        c = service.create("Build a CLI")
        self.assertEqual(c["models"]["claude"], "us.anthropic.claude-sonnet-4-6")
        run = next(r for r in c["runs"] if r["harness"] == "claude")
        self.assertEqual(run["requested_model"], "us.anthropic.claude-sonnet-4-6")

    # 4. Configured default absent from catalogue -> error naming the property key
    def test_configured_default_absent_from_catalogue_errors_with_property_name(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data3"),
                     harnesses={"claude": HarnessConfig(model="us.anthropic.claude-opus-5"),
                                "codex": HarnessConfig(), "kiro": HarnessConfig()})
        service = Service(cfg)
        service.store.put("system", _cat(cfg,
            claude={"models": [{"id": "us.anthropic.claude-sonnet-4-6"}]},
            codex={"models": []}, kiro={"models": []}))
        with self.assertRaisesRegex(ValueError, "CLAUDE_MODEL"):
            service.create("Build a CLI")

    # 5. Stale/missing catalogue fingerprint -> error for any non-None model
    def test_stale_fingerprint_rejects_any_non_none_model(self):
        self.service.store.put("system", {
            "id": "model-catalogue", "fingerprint": "stale", "checked_at": 1.0,
            "harnesses": {"claude": {"models": [{"id": "some-model"}]},
                          "codex": {"models": []}, "kiro": {"models": []}}})
        with self.assertRaisesRegex(ValueError, "stale or missing"):
            self.service.create("Build a CLI", models={"claude": "some-model"})

    def test_missing_catalogue_rejects_any_non_none_model(self):
        # No catalogue in store at all
        cfg = Config(data_dir=str(Path(self.temp.name) / "data4"),
                     harnesses={"claude": HarnessConfig(model="us.anthropic.claude-sonnet-4-6"),
                                "codex": HarnessConfig(), "kiro": HarnessConfig()})
        with self.assertRaisesRegex(ValueError, "stale or missing"):
            Service(cfg).create("Build a CLI")

    # 6. None resolved -> allowed without a catalogue
    def test_none_resolved_needs_no_catalogue(self):
        # Config with no defaults, no explicit picks -> all None -> ok
        c = self.service.create("Build a CLI")
        self.assertIsNone(c["models"]["claude"])
        self.assertIsNone(c["models"]["codex"])
        self.assertIsNone(c["models"]["kiro"])


class JudgeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _service(self, judge_model=None):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     judge_model=judge_model)
        return Service(cfg)

    def _seed_judge(self, service, model_ids):
        service.store.put("system", _judge_cat(service.config, model_ids))

    # 7a. Judge default validated (absent from catalogue -> error)
    def test_configured_judge_absent_from_catalogue_is_rejected(self):
        svc = self._service("us.xai.grok-4")
        self._seed_judge(svc, ["us.other.model"])  # grok-4 not included
        with self.assertRaisesRegex(ValueError, "JUDGE_MODEL"):
            svc.judge_selection()

    # 7b. No bypass: configured default is now validated
    def test_configured_judge_present_in_catalogue_is_accepted(self):
        svc = self._service("us.xai.grok-4")
        self._seed_judge(svc, ["us.xai.grok-4"])
        result = svc.judge_selection()
        self.assertEqual(result["model"], "us.xai.grok-4")

    # 7c. Stale catalogue fails even for configured default
    def test_stale_judge_catalogue_rejects_configured_default(self):
        svc = self._service("us.xai.grok-4")
        svc.store.put("system", {"id": "judge-model-catalogue", "fingerprint": "stale",
                                 "checked_at": 1.0, "models": [{"id": "us.xai.grok-4", "selectable": True}]})
        with self.assertRaisesRegex(ValueError, "stale or missing"):
            svc.judge_selection()

    # 7d. No judge_model configured and no explicit pick -> None model ok
    def test_no_judge_configured_returns_none_model(self):
        svc = self._service()
        result = svc.judge_selection()
        self.assertIsNone(result["model"])

    # 8. reevaluate with None model_id keeps stored judge
    def test_none_model_id_with_stored_judge_returns_stored_judge(self):
        svc = self._service("us.xai.grok-4")
        stored_comparison = {"judge": {"model": "us.other.model", "profile": None,
                                       "region": "us-east-1", "streaming": True}}
        result = svc.judge_selection(model_id=None, comparison=stored_comparison)
        self.assertEqual(result["model"], "us.other.model")

    # Explicit pick validated against catalogue
    def test_explicit_judge_pick_validated_against_catalogue(self):
        svc = self._service()
        self._seed_judge(svc, ["us.xai.grok-4"])
        result = svc.judge_selection("us.xai.grok-4")
        self.assertEqual(result["model"], "us.xai.grok-4")

    def test_explicit_judge_pick_not_in_catalogue_is_rejected(self):
        svc = self._service()
        self._seed_judge(svc, ["us.other.model"])
        with self.assertRaisesRegex(ValueError, "not in the catalog"):
            svc.judge_selection("us.xai.grok-4")

    def test_non_selectable_judge_is_rejected(self):
        svc = self._service()
        fp = catalogue_fingerprint(svc.config)
        svc.store.put("system", {"id": "judge-model-catalogue", "fingerprint": fp,
                                 "checked_at": 1.0,
                                 "models": [{"id": "us.xai.grok-4", "selectable": False,
                                             "reason": "Not active", "streaming": False}]})
        with self.assertRaisesRegex(ValueError, "Not active"):
            svc.judge_selection("us.xai.grok-4")


class PlanningSelectionTests(unittest.TestCase):
    CLAUDE_MODEL = "us.anthropic.claude-sonnet-4-6"
    OTHER_MODEL = "us.xai.grok-4"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _service(self, planning_model=None, planning_effort=None, judge_model=None):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     planning_model=planning_model, planning_effort=planning_effort,
                     judge_model=judge_model)
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [self.CLAUDE_MODEL, self.OTHER_MODEL]))
        return svc

    # 9. Explicit planning pick is stored as comparison["planning"]
    def test_planning_pick_is_stored_on_comparison(self):
        svc = self._service()
        svc.store.put("system", _cat(svc.config))  # empty harness catalogue (no models)
        judge = {"model": None}
        result = svc.planning_selection(self.CLAUDE_MODEL, judge)
        self.assertEqual(result["model"], self.CLAUDE_MODEL)
        self.assertIsNone(result["effort"])

    # 10. Default resolution order: pick > planning_model > judge
    def test_planning_default_resolution_uses_pick_first(self):
        svc = self._service(planning_model=self.OTHER_MODEL)
        judge = {"model": self.OTHER_MODEL}
        result = svc.planning_selection(self.CLAUDE_MODEL, judge)
        self.assertEqual(result["model"], self.CLAUDE_MODEL)

    def test_planning_default_resolution_falls_back_to_config_planning_model(self):
        svc = self._service(planning_model=self.CLAUDE_MODEL)
        judge = {"model": self.OTHER_MODEL}
        result = svc.planning_selection(None, judge)
        self.assertEqual(result["model"], self.CLAUDE_MODEL)

    def test_planning_default_resolution_falls_back_to_judge_model(self):
        svc = self._service()
        result = svc.planning_selection(None, {"model": self.CLAUDE_MODEL})
        self.assertEqual(result["model"], self.CLAUDE_MODEL)

    def test_an_unlisted_planning_model_names_where_it_came_from(self):
        for planning_model, model_id, judge, source in (
                (None, "us.anthropic.claude-gone", None, "your selection"),
                ("us.anthropic.claude-gone", None, None, "the configured default PLANNING_MODEL"),
                (None, None, {"model": "us.anthropic.claude-gone"}, "the judge model, used while PLANNING_MODEL is unset")):
            with self.subTest(source=source):
                svc = self._service(planning_model=planning_model)
                with self.assertRaisesRegex(ValueError, f"from {source}"):
                    svc.planning_selection(model_id, judge)

    def test_planning_none_when_no_model_resolved(self):
        svc = self._service()
        result = svc.planning_selection(None, None)
        self.assertIsNone(result)

    # 11. Non-Claude planning model with planning_effort set -> error (explicit pick overrides Claude default)
    def test_non_claude_planning_model_with_planning_effort_fails(self):
        # Config requires Claude when planning_effort is set; the model explicitly supplied
        # at call time can still be non-Claude, and planning_selection must catch that.
        cfg = Config(data_dir=str(Path(self.temp.name) / "data2"),
                     planning_model=self.CLAUDE_MODEL, planning_effort="max")
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [self.CLAUDE_MODEL, self.OTHER_MODEL]))
        # Explicitly pass a non-Claude model despite planning_effort requiring Claude.
        with self.assertRaisesRegex(ValueError, "anthropic.claude"):
            svc.planning_selection(self.OTHER_MODEL, None)

    def test_claude_planning_model_with_planning_effort_is_accepted(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data3"),
                     planning_model=self.CLAUDE_MODEL, planning_effort="max")
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [self.CLAUDE_MODEL]))
        result = svc.planning_selection(None, None)
        self.assertEqual(result, {"model": self.CLAUDE_MODEL, "effort": "max",
                                  "effort_basis": "planning_effort in config.toml"})

    # 12. planning_for_comparison() override
    def test_planning_for_comparison_returns_self_when_no_planning_key(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data4"),
                     planning_model=self.CLAUDE_MODEL)
        result = cfg.planning_for_comparison({})
        self.assertIs(result, cfg)

    def test_planning_for_comparison_overrides_planning_model(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data5"),
                     planning_model=self.CLAUDE_MODEL)
        result = cfg.planning_for_comparison({"planning": {"model": self.OTHER_MODEL}})
        self.assertEqual(result.planning_model, self.OTHER_MODEL)
        self.assertEqual(cfg.planning_model, self.CLAUDE_MODEL)  # original unchanged

    def test_planning_stored_on_comparison_via_create(self):
        svc = self._service()
        svc.store.put("system", _cat(svc.config))  # empty harness catalogue
        c = svc.create("Build a CLI", planning_model=self.CLAUDE_MODEL)
        self.assertIsNotNone(c.get("planning"))
        self.assertEqual(c["planning"]["model"], self.CLAUDE_MODEL)

    def test_planning_none_stored_when_no_model_resolved(self):
        svc = self._service()
        svc.store.put("system", _cat(svc.config))
        c = svc.create("Build a CLI")
        self.assertIsNone(c.get("planning"))


class SystemEndpointTests(unittest.TestCase):
    def _client(self, planning_effort=None):
        cfg = Config(data_dir=tempfile.mkdtemp(), planning_effort=planning_effort,
                     planning_model="us.anthropic.claude-sonnet-4-6" if planning_effort else None)
        with unittest.mock.patch("arena.api.load_config", return_value=cfg):
            app = create_app()
        token = access_token(cfg.root)
        client = TestClient(app, base_url="http://127.0.0.1",
                            headers={"Authorization": "Bearer " + token})
        return client

    # 13. /api/system exposes planning_requires_claude
    def test_system_planning_requires_claude_false_without_effort(self):
        import unittest.mock
        client = self._client(planning_effort=None)
        data = client.get("/api/system").json()
        self.assertFalse(data["planning_requires_claude"])

    def test_system_planning_requires_claude_true_with_effort(self):
        import unittest.mock
        client = self._client(planning_effort="max")
        data = client.get("/api/system").json()
        self.assertTrue(data["planning_requires_claude"])

    def test_a_submission_with_the_configured_default_judge_fetches_the_catalogue_first(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cfg = Config(data_dir=temp.name, judge_model="us.xai.grok-4")
        with patch("arena.api.load_config", return_value=cfg):
            app = create_app()
        fetched = []

        def discover(catalogue, cached):
            fetched.append(cached["checked_at"])
            return catalogue.store.put("system", _judge_cat(cfg, ["us.xai.grok-4"]))

        with patch("arena.judge_models.JudgeModelCatalogue.discover", discover), \
                TestClient(app, base_url="http://127.0.0.1",
                           headers={"Authorization": "Bearer " + access_token(cfg.root)}) as client:
            response = client.post("/api/comparisons", json={"prompt": "Build a CLI"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(fetched, [0])
        self.assertEqual(response.json()["judge"]["model"], "us.xai.grok-4")


class HighestEffortTests(unittest.TestCase):
    def test_returns_max_for_full_list(self):
        from arena.config import highest_effort
        self.assertEqual(highest_effort(["low", "medium", "high", "xhigh", "max"]), "max")

    def test_returns_highest_when_max_absent(self):
        from arena.config import highest_effort
        self.assertEqual(highest_effort(["low", "medium", "high"]), "high")

    def test_returns_xhigh_when_only_xhigh_present(self):
        from arena.config import highest_effort
        self.assertEqual(highest_effort(["xhigh"]), "xhigh")

    def test_returns_none_for_empty_list(self):
        from arena.config import highest_effort
        self.assertIsNone(highest_effort([]))

    def test_returns_none_when_no_efforts_match(self):
        from arena.config import highest_effort
        self.assertIsNone(highest_effort(["ultracode", "unknown"]))

    def test_ignores_unknown_levels_picks_highest_known(self):
        from arena.config import highest_effort
        self.assertEqual(highest_effort(["low", "ultracode", "max"]), "max")


def _cat_with_efforts(config, model_id, efforts):
    """Harness catalogue with a claude entry carrying an efforts list."""
    return _cat(config,
                claude={"models": [{"id": model_id, "efforts": efforts}]},
                codex={"models": []}, kiro={"models": []})


class PlanningEffortResolutionTests(unittest.TestCase):
    CLAUDE_OPUS = "us.anthropic.claude-opus-5"
    CLAUDE_SONNET = "us.anthropic.claude-sonnet-4-6"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _svc(self, model_id, efforts, planning_effort="highest"):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     planning_model=model_id, planning_effort=planning_effort,
                     judge_model=model_id)
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [model_id]))
        svc.store.put("system", _cat_with_efforts(cfg, model_id, efforts))
        return svc

    # highest + model with xhigh and max -> max
    def test_highest_opus5_with_full_levels_resolves_to_max(self):
        svc = self._svc(self.CLAUDE_OPUS, ["low", "medium", "high", "xhigh", "max"])
        result = svc.planning_selection(None, None)
        self.assertEqual(result["effort"], "max")
        self.assertEqual(result["effort_basis"], "highest the model supports")

    # highest + model whose levels top out at high -> high
    def test_highest_model_capped_at_high(self):
        svc = self._svc(self.CLAUDE_SONNET, ["low", "medium", "high"])
        result = svc.planning_selection(None, None)
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["effort_basis"], "highest the model supports")

    # region prefix and [1m] suffix stripping
    def test_highest_strips_region_prefix_and_suffix_for_matching(self):
        # judge catalogue uses global...[1m], harness catalogue uses us... without suffix
        model_id = "global.anthropic.claude-opus-5[1m]"
        cfg = Config(data_dir=str(Path(self.temp.name) / "data2"),
                     planning_model=model_id, planning_effort="highest",
                     judge_model=model_id)
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [model_id]))
        # harness catalogue stores the base id without region prefix or [1m]
        svc.store.put("system", _cat(cfg,
            claude={"models": [{"id": "us.anthropic.claude-opus-5",
                                "efforts": ["low", "medium", "high", "xhigh", "max"]}]},
            codex={"models": []}, kiro={"models": []}))
        result = svc.planning_selection(None, None)
        self.assertEqual(result["effort"], "max")
        self.assertEqual(result["effort_basis"], "highest the model supports")

    # levels unknown (no harness catalogue) -> max
    def test_highest_unknown_levels_defaults_to_max(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data3"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="highest",
                     judge_model=self.CLAUDE_OPUS)
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [self.CLAUDE_OPUS]))
        # no harness catalogue stored
        result = svc.planning_selection(None, None)
        self.assertEqual(result["effort"], "max")
        self.assertEqual(result["effort_basis"], "highest (model levels unknown)")

    # levels [] -> error
    def test_highest_empty_levels_raises_error(self):
        svc = self._svc(self.CLAUDE_OPUS, [])
        with self.assertRaisesRegex(ValueError, "supports no known effort level"):
            svc.planning_selection(None, None)

    def test_highest_with_only_unknown_levels_raises_error(self):
        svc = self._svc(self.CLAUDE_OPUS, ["ultra"])
        with self.assertRaisesRegex(ValueError, r"supports no known effort level \(ultra\)"):
            svc.planning_selection(None, None)

    # pinned level in supported list -> accepted
    def test_pinned_level_in_supported_list_accepted(self):
        svc = self._svc(self.CLAUDE_OPUS, ["low", "medium", "high", "xhigh", "max"],
                        planning_effort="high")
        result = svc.planning_selection(None, None)
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["effort_basis"], "planning_effort in config.toml")

    # pinned level not in supported list -> error
    def test_pinned_level_not_in_supported_list_raises_error(self):
        svc = self._svc(self.CLAUDE_SONNET, ["low", "medium", "high"], planning_effort="xhigh")
        with self.assertRaisesRegex(ValueError, "does not support effort"):
            svc.planning_selection(None, None)

    # planning_effort None -> effort None
    def test_planning_effort_none_returns_none_effort_and_basis(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data4"),
                     planning_model=self.CLAUDE_OPUS, judge_model=self.CLAUDE_OPUS)
        svc = Service(cfg)
        svc.store.put("system", _judge_cat(cfg, [self.CLAUDE_OPUS]))
        result = svc.planning_selection(None, None)
        self.assertIsNone(result["effort"])
        self.assertIsNone(result["effort_basis"])

    # planning_for_comparison overrides both model and effort
    def test_planning_for_comparison_overrides_model_and_effort(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data5"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="highest",
                     judge_model=self.CLAUDE_OPUS)
        result = cfg.planning_for_comparison(
            {"planning": {"model": self.CLAUDE_SONNET, "effort": "max"}})
        self.assertEqual(result.planning_model, self.CLAUDE_SONNET)
        self.assertEqual(result.planning_effort, "max")
        # original unchanged
        self.assertEqual(cfg.planning_model, self.CLAUDE_OPUS)
        self.assertEqual(cfg.planning_effort, "highest")

    # stored effort None disables adaptive thinking
    def test_planning_for_comparison_stored_none_effort_disables_thinking(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data6"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="highest",
                     judge_model=self.CLAUDE_OPUS)
        result = cfg.planning_for_comparison(
            {"planning": {"model": self.CLAUDE_OPUS, "effort": None}})
        self.assertIsNone(result.planning_effort)

    # legacy comparison (no planning key) with "highest" -> falls back to "max"
    def test_planning_for_comparison_highest_legacy_falls_back_to_max(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data7"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="highest",
                     judge_model=self.CLAUDE_OPUS)
        result = cfg.planning_for_comparison({})
        self.assertEqual(result.planning_effort, "max")
        self.assertIsNot(result, cfg)

    # legacy comparison with non-"highest" effort -> returns self
    def test_planning_for_comparison_non_highest_legacy_returns_self(self):
        cfg = Config(data_dir=str(Path(self.temp.name) / "data8"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="max",
                     judge_model=self.CLAUDE_OPUS)
        result = cfg.planning_for_comparison({})
        self.assertIs(result, cfg)

    # planner's Bedrock request fields use concrete level, not "highest"
    def test_planner_bedrock_request_uses_concrete_effort(self):
        import unittest.mock
        from arena.evaluation import judge_model
        cfg = Config(data_dir=str(Path(self.temp.name) / "data9"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="max",
                     judge_model=self.CLAUDE_OPUS)
        with unittest.mock.patch("boto3.Session") as mock_session:
            mock_session.return_value = unittest.mock.MagicMock()
            m = judge_model(cfg, max_tokens=1000)
        arf = m.get_config().get("additional_request_fields", {})
        self.assertEqual(arf.get("output_config", {}).get("effort"), "max")

    # defensive: judge_model never sends "highest" to Bedrock
    def test_planner_defensive_guard_converts_highest_to_max(self):
        import unittest.mock
        from arena.evaluation import judge_model
        cfg = Config(data_dir=str(Path(self.temp.name) / "data10"),
                     planning_model=self.CLAUDE_OPUS, planning_effort="highest",
                     judge_model=self.CLAUDE_OPUS)
        with unittest.mock.patch("boto3.Session") as mock_session:
            mock_session.return_value = unittest.mock.MagicMock()
            m = judge_model(cfg, max_tokens=1000)
        arf = m.get_config().get("additional_request_fields", {})
        self.assertNotEqual(arf.get("output_config", {}).get("effort"), "highest")
        self.assertEqual(arf.get("output_config", {}).get("effort"), "max")

    # config.example.toml ships planning_effort = "highest"
    def test_config_example_toml_has_highest_planning_effort(self):
        import tomllib
        repo = Path(__file__).parent.parent
        raw = tomllib.loads((repo / "config.example.toml").read_text())
        self.assertEqual(raw.get("planning_effort"), "highest")


if __name__ == "__main__":
    unittest.main()
