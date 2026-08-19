"""Negative controls for the Phase 1 guarantee-ledger validator."""

from __future__ import annotations

import copy
import datetime
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "guarantee_ledger", ROOT / "scripts" / "guarantee_ledger.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _LedgerFixture(unittest.TestCase):
    """Shared fixture + assertion helpers. Holds no tests of its own, so a second
    suite can reuse it without re-running the first suite's cases."""

    @classmethod
    def setUpClass(cls):
        cls.ledger = MODULE.load_json_strict(ROOT / "docs" / "guarantee-ledger.json")
        cls.schema = MODULE.load_json_strict(
            ROOT / "docs" / "guarantee-ledger.schema.json")
        cls.tracked = {
            item.decode("utf-8")
            for item in subprocess.check_output(
                ["git", "ls-files", "-z"], cwd=ROOT
            ).split(b"\0")
            if item
        }

    def errors(self, ledger):
        return MODULE.validate_ledger(ledger, root=ROOT, tracked=self.tracked)

    def mutated(self):
        return copy.deepcopy(self.ledger)

    def assertRejectedWith(self, ledger, fragment):
        errors = self.errors(ledger)
        self.assertTrue(errors, "mutant unexpectedly passed validation")
        self.assertTrue(
            any(fragment in error for error in errors),
            f"expected {fragment!r} in {errors!r}",
        )

    # ---------------------------------------------------------------- helpers
    def cli_rc(self, ledger):
        """Run the real CLI path over *ledger* and return (rc, stderr)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mutant.json"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            stderr, stdout = StringIO(), StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                rc = MODULE.main([
                    "--ledger", str(path),
                    "--schema", str(ROOT / "docs" / "guarantee-ledger.schema.json"),
                ])
            return rc, stderr.getvalue()

    def assertRejectedThroughBothPaths(self, ledger, fragment):
        self.assertRejectedWith(ledger, fragment)
        rc, err = self.cli_rc(ledger)
        self.assertEqual(1, rc, f"CLI accepted the mutant: {err}")
        self.assertIn(fragment, err)

    def first_with_live_evidence(self, data):
        return next(e for e in data["guarantees"] if e["required_live_evidence"])

    def first_semantic(self, data):
        return next(e for e in data["guarantees"] if e["claim_kind"] == "semantic")

    def first_real_proof(self, data):
        return next(p for e in data["guarantees"] for p in e["proofs"]
                    if p["type"] != "missing")

    def first_violation(self, data):
        return next(e for e in data["guarantees"] if e["status"] == "known_violation")


class GuaranteeLedgerValidation(_LedgerFixture):
    def test_real_ledger_is_valid(self):
        self.assertEqual([], self.errors(self.ledger))

    def test_structurally_invalid_root_rejected(self):
        self.assertRejectedWith([], "root must be an object")

    def test_unsupported_structural_field_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["verdict_by_vibes"] = True
        self.assertRejectedWith(data, "contains unsupported field: verdict_by_vibes")

    def test_missing_required_field_rejected(self):
        data = self.mutated()
        del data["guarantees"][0]["owner"]
        self.assertRejectedWith(data, "missing required field: owner")

    def test_duplicate_id_rejected(self):
        data = self.mutated()
        data["guarantees"][1]["id"] = data["guarantees"][0]["id"]
        self.assertRejectedWith(data, "duplicate guarantee id")

    def test_invalid_controlled_enums_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["status"] = "looks-good"
        data["guarantees"][0]["proofs"][0]["type"] = "a test exists"
        self.assertRejectedWith(data, "invalid status")
        self.assertRejectedWith(data, "invalid proof type")

    def test_nonexistent_or_untracked_proof_path_rejected(self):
        data = self.mutated()
        proof = next(
            proof
            for entry in data["guarantees"]
            for proof in entry["proofs"]
            if proof["type"] != "missing"
        )
        proof["path"] = "tests/not-a-real-proof.py"
        self.assertRejectedWith(data, "evidence path is not a tracked file")

    def test_unsupported_platform_and_provider_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["applicable_platforms"] = ["plan9"]
        data["guarantees"][0]["applicable_providers"] = ["imaginary"]
        self.assertRejectedWith(data, "unsupported platform names")
        self.assertRejectedWith(data, "unsupported provider names")

    def test_high_mechanical_verified_without_integration_rejected(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["failure_consequence"] = "High"
        entry["claim_kind"] = "mechanical"
        entry["status"] = "verified_by_current_executable_evidence"
        entry["proofs"] = [
            {
                "type": "executable unit",
                "path": "tests/test_evidence_contract.py",
                "reference": "CloseDecision.test_clean_reversible_closes",
                "boundary": "unit",
                "negative_control": {
                    "path": "tests/test_evidence_contract.py",
                    "reference": "CloseDecision.test_force_without_reason_blocks",
                },
                "limitations": [],
            }
        ]
        self.assertRejectedWith(data, "lacks executable integration evidence")

    def test_high_mechanical_verified_without_negative_control_rejected(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["failure_consequence"] = "Critical"
        entry["claim_kind"] = "mechanical"
        entry["status"] = "verified_by_current_executable_evidence"
        entry["proofs"] = [
            {
                "type": "executable integration",
                "path": "tests/test_work_readopt.py",
                "reference": "TestReadoptFullyGated.test_readopt_then_work_done_closes_the_task",
                "boundary": "subprocess",
                "negative_control": None,
                "limitations": [],
            }
        ]
        self.assertRejectedWith(data, "lacks a targeted integration negative control")

    def test_required_live_evidence_must_be_phase_8(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["status"] = "partially_evidenced"
        entry["missing_evidence_or_limitation"] = ["live cell absent"]
        entry["follow_up_phases"] = [7]
        entry["required_live_evidence"] = [
            {
                "type": "live-platform test",
                "targets": ["macos"],
                "phase": 7,
                "reason": "runner unavailable",
            }
        ]
        self.assertRejectedWith(data, "must be scheduled for Phase 8")

    def test_non_green_status_requires_limitation_and_follow_up(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["status"] = "missing_evidence"
        entry["missing_evidence_or_limitation"] = []
        entry["follow_up_phases"] = []
        self.assertRejectedWith(data, "requires an explicit limitation")
        self.assertRejectedWith(data, "requires a follow-up phase")

    def test_semantic_claim_requires_controlled_repeated_protocol(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["claim_kind"] = "semantic"
        entry["semantic_protocol"] = None
        self.assertRejectedWith(data, "requires the controlled protocol object")

    def test_duplicate_members_rejected_by_strict_loader(self):
        for source, payload in (
            ("ledger", '{"status":"a","status":"b"}'),
            ("schema", '{"type":"object","type":"array"}'),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "duplicate object member"):
                    MODULE.loads_json_strict(payload, source=source)

    def test_cli_uses_the_same_duplicate_rejecting_loader(self):
        raw = (ROOT / "docs" / "guarantee-ledger.json").read_text(encoding="utf-8")
        version_line = f'"ledger_version": {json.dumps(self.ledger["ledger_version"])},'
        self.assertIn(version_line, raw)
        raw = raw.replace(version_line, f"{version_line}\n  {version_line}", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.json"
            path.write_text(raw, encoding="utf-8")
            stderr = StringIO()
            stdout = StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                rc = MODULE.main([
                    "--ledger", str(path),
                    "--schema", str(ROOT / "docs" / "guarantee-ledger.schema.json"),
                ])
        self.assertEqual(2, rc)
        self.assertIn("duplicate object member", stderr.getvalue())

    def test_cli_strictly_loads_the_schema_too(self):
        raw = (ROOT / "docs" / "guarantee-ledger.schema.json").read_text(encoding="utf-8")
        raw = raw.replace(
            '"title": "Playbook guarantee ledger",',
            '"title": "Playbook guarantee ledger",\n  "title": "duplicate",',
            1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate-schema.json"
            path.write_text(raw, encoding="utf-8")
            stderr = StringIO()
            stdout = StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                rc = MODULE.main([
                    "--ledger", str(ROOT / "docs" / "guarantee-ledger.json"),
                    "--schema", str(path),
                ])
        self.assertEqual(2, rc)
        self.assertIn("duplicate object member", stderr.getvalue())

    def test_cli_runs_schema_contract_and_ledger_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            schema = MODULE.load_json_strict(ROOT / "docs" / "guarantee-ledger.schema.json")
            schema["$defs"]["guarantee"]["properties"]["status"]["enum"].remove(
                "missing_evidence"
            )
            schema_path = tmp / "drifted-schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr), redirect_stdout(StringIO()):
                rc = MODULE.main([
                    "--ledger", str(ROOT / "docs" / "guarantee-ledger.json"),
                    "--schema", str(schema_path),
                ])
            self.assertEqual(2, rc)
            self.assertIn("Schema/validator contract invalid", stderr.getvalue())

            ledger = self.mutated()
            ledger["ledger_version"] = "not-a-date"
            ledger_path = tmp / "invalid-ledger.json"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr), redirect_stdout(StringIO()):
                rc = MODULE.main([
                    "--ledger", str(ledger_path),
                    "--schema", str(ROOT / "docs" / "guarantee-ledger.schema.json"),
                ])
            self.assertEqual(1, rc)
            self.assertIn("canonical YYYY-MM-DD date", stderr.getvalue())

    def test_ledger_version_must_be_a_real_canonical_date(self):
        for bad in ("soon", "2026-8-18", "2026-02-30", "2026-08-18T00:00:00"):
            with self.subTest(value=bad):
                data = self.mutated()
                data["ledger_version"] = bad
                self.assertRejectedWith(data, "canonical YYYY-MM-DD date")

    def test_vague_critical_python_reference_rejected(self):
        data = self.mutated()
        entry = next(item for item in data["guarantees"] if item["id"] == "PB-PAYLOAD-FUSED-FRAME")
        entry["proofs"][0]["reference"] = "test"
        self.assertRejectedWith(data, "exact Python test reference")

    def test_nonexistent_exact_python_reference_rejected(self):
        data = self.mutated()
        entry = next(item for item in data["guarantees"] if item["id"] == "PB-PAYLOAD-FUSED-FRAME")
        entry["proofs"][0]["reference"] = "EnforcingGateConsumesTheWholeFrame.test_not_real"
        self.assertRejectedWith(data, "exact Python test reference not found")

    def test_generic_shell_and_document_references_rejected(self):
        data = self.mutated()
        shell = next(item for item in data["guarantees"] if item["id"] == "PB-MERGE-CODE-IDENTITY")
        shell["proofs"][0]["reference"] = "PASS"
        self.assertRejectedWith(data, "exact shell scenario reference")

        data = self.mutated()
        manual = next(item for item in data["guarantees"] if item["id"] == "PB-MONITOR-DETECTS-DRIFT")
        manual["proofs"][0]["reference"] = "test"
        self.assertRejectedWith(data, "exact document section reference")

    def test_bare_negative_control_assertion_rejected(self):
        data = self.mutated()
        entry = next(item for item in data["guarantees"] if item["id"] == "PB-PAYLOAD-FUSED-FRAME")
        entry["proofs"][0]["negative_control"] = True
        self.assertRejectedWith(data, "negative_control must be null or an exact evidence reference")

    def test_vague_negative_control_reference_rejected(self):
        data = self.mutated()
        entry = next(item for item in data["guarantees"] if item["id"] == "PB-PAYLOAD-FUSED-FRAME")
        entry["proofs"][0]["negative_control"] = {
            "path": "tests/test_fused_payload_fields.py",
            "reference": "test",
        }
        self.assertRejectedWith(data, "exact Python test reference")

    def test_material_schema_validator_drift_rejected(self):
        schema = json.loads(
            (ROOT / "docs" / "guarantee-ledger.schema.json").read_text(encoding="utf-8")
        )
        schema["$defs"]["guarantee"]["properties"]["status"]["enum"].remove(
            "missing_evidence"
        )
        errors = MODULE.validate_schema_contract(schema)
        self.assertTrue(errors)
        self.assertTrue(
            any("status enum" in error for error in errors), errors
        )


    # ------------------------------------------- the four reproduced fail-opens
    def test_empty_guarantees_array_rejected_by_api_and_cli(self):
        data = self.mutated()
        data["guarantees"] = []
        self.assertRejectedThroughBothPaths(data, "must contain at least one entry")

    def test_duplicate_owner_binding_rejected_by_api_and_cli(self):
        data = self.mutated()
        owner = data["guarantees"][0]["owner"]
        owner.append(copy.deepcopy(owner[0]))
        self.assertRejectedThroughBothPaths(data, "duplicate owner binding")

    def test_duplicate_owner_reference_on_one_path_rejected(self):
        data = self.mutated()
        entry = next(e for e in data["guarantees"] if len(e["owner"]) > 1)
        entry["owner"][1] = copy.deepcopy(entry["owner"][0])
        self.assertRejectedThroughBothPaths(data, "duplicate owner binding")

    def test_duplicate_live_evidence_targets_rejected_by_api_and_cli(self):
        data = self.mutated()
        live = self.first_with_live_evidence(data)["required_live_evidence"][0]
        live["targets"].append(live["targets"][0])
        self.assertRejectedThroughBothPaths(data, "duplicate live targets")

    # ------------------------------------------------ adverse-control integrity
    def test_negative_control_repeating_its_own_proof_rejected(self):
        data = self.mutated()
        proof = next(p for e in data["guarantees"] for p in e["proofs"]
                     if p["negative_control"])
        proof["negative_control"] = {"path": proof["path"],
                                     "reference": proof["reference"]}
        self.assertRejectedThroughBothPaths(data, "negative_control repeats its own proof")

    def test_duplicate_proof_reference_within_one_guarantee_rejected(self):
        data = self.mutated()
        entry = next(e for e in data["guarantees"] if len(e["proofs"]) > 1)
        entry["proofs"][1]["path"] = entry["proofs"][0]["path"]
        entry["proofs"][1]["reference"] = entry["proofs"][0]["reference"]
        self.assertRejectedWith(data, "duplicate proof reference")

    def test_duplicate_statement_across_guarantees_rejected(self):
        data = self.mutated()
        data["guarantees"][1]["statement"] = data["guarantees"][0]["statement"]
        self.assertRejectedThroughBothPaths(data, "statement duplicates")

    # -------------------------------------------------------- owner resolution
    def test_untracked_owner_path_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"][0]["path"] = "plugins/playbook/tasks/ghost.py"
        self.assertRejectedThroughBothPaths(data, "owner path is not a tracked file")

    def test_nonexistent_owner_symbol_rejected(self):
        data = self.mutated()
        binding = next(b for e in data["guarantees"] for b in e["owner"]
                       if b["reference"].startswith("symbol:"))
        binding["reference"] = "symbol:no_such_production_symbol"
        self.assertRejectedThroughBothPaths(data, "owner symbol not found")

    def test_owner_symbol_on_a_non_python_file_rejected(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["owner"] = [{"path": "README.md", "reference": "symbol:cmd_work"}]
        self.assertRejectedThroughBothPaths(
            data, "symbol: owner references are only defined for Python files")

    def test_whole_file_owner_on_a_python_module_with_symbols_rejected(self):
        data = self.mutated()
        entry = data["guarantees"][0]
        entry["owner"] = [{"path": "plugins/playbook/tasks/lifecycle.py",
                           "reference": "whole-file"}]
        self.assertRejectedThroughBothPaths(
            data, "Python owners must use an AST-resolvable symbol")

    def test_nonexistent_owner_shell_function_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/scripts/gate-echo-lib.sh",
             "reference": "function:no_such_function"}]
        self.assertRejectedThroughBothPaths(data, "owner shell function not found")

    def test_nonexistent_owner_case_arm_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/scripts/session-end-hook",
             "reference": "case-arm:no-such-arm"}]
        self.assertRejectedThroughBothPaths(data, "owner shell case arm not found")

    def test_real_owner_case_arm_resolves(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/scripts/session-end-hook",
             "reference": "case-arm:logout"}]
        self.assertEqual([], self.errors(data))

    def test_nonexistent_owner_document_section_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "docs/cli.md", "reference": "section:No Such Heading"}]
        self.assertRejectedThroughBothPaths(data, "owner document section")

    def test_nonexistent_owner_json_pointer_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/.claude-plugin/plugin.json",
             "reference": "pointer:/not/a/key"}]
        self.assertRejectedThroughBothPaths(data, "owner JSON pointer not found")

    def test_owner_reference_scheme_must_be_known(self):
        data = self.mutated()
        data["guarantees"][0]["owner"][0]["reference"] = "cmd_work"
        self.assertRejectedThroughBothPaths(data, "owner reference must be")

    def test_whole_file_owner_on_a_markdown_file_with_headings_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "docs/cli.md", "reference": "whole-file"}]
        self.assertRejectedThroughBothPaths(
            data, "Markdown owners must name an exact unique section")

    def test_non_unique_owner_document_section_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "CHANGELOG.md", "reference": "section:Fixed"}]
        self.assertRejectedThroughBothPaths(data, "is not unique in")

    def test_pointer_owner_on_a_non_json_file_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "README.md", "reference": "pointer:/anything"}]
        self.assertRejectedThroughBothPaths(
            data, "pointer: owner references are only defined for JSON files")

    # ------------------------------------------- known_violation status contract
    def test_the_ledger_records_the_confirmed_lane_violation(self):
        entry = self.first_violation(self.ledger)
        self.assertEqual("PB-LANE-RESOLUTION", entry["id"])
        v = entry["violation_reproduction"]
        self.assertEqual("tests/wrapper-multiuser-fixture.sh", v["path"])
        self.assertEqual("scenario:S15", v["reference"])
        self.assertIn("plugins/playbook/scripts/bash-log.sh", v["artifacts"])
        self.assertEqual(["linux"], v["platforms"])
        self.assertEqual(2, v["phase"])
        self.assertIn(2, entry["follow_up_phases"])

    def test_known_violation_without_a_reproduction_rejected(self):
        data = self.mutated()
        self.first_violation(data)["violation_reproduction"] = None
        self.assertRejectedThroughBothPaths(
            data, "known_violation requires a violation_reproduction")

    def test_known_violation_reproduction_must_resolve(self):
        data = self.mutated()
        self.first_violation(data)["violation_reproduction"]["reference"] = "scenario:S999"
        self.assertRejectedThroughBothPaths(data, "shell scenario 'scenario:S999' not found")

    def test_known_violation_requires_a_limitation(self):
        data = self.mutated()
        self.first_violation(data)["missing_evidence_or_limitation"] = []
        self.assertRejectedThroughBothPaths(
            data, "non-green status requires an explicit limitation")

    def test_known_violation_requires_a_follow_up_phase(self):
        data = self.mutated()
        entry = self.first_violation(data)
        entry["follow_up_phases"] = []
        self.assertRejectedThroughBothPaths(
            data, "non-green status requires a follow-up phase")

    def test_critical_known_violation_must_schedule_phase_2(self):
        data = self.mutated()
        entry = self.first_violation(data)
        entry["follow_up_phases"] = [8]
        entry["violation_reproduction"]["phase"] = 8
        self.assertRejectedThroughBothPaths(
            data, "known violation must schedule the runtime correction in Phase 2")

    def test_violation_phase_must_be_listed_in_follow_up_phases(self):
        data = self.mutated()
        self.first_violation(data)["violation_reproduction"]["phase"] = 5
        self.assertRejectedThroughBothPaths(
            data, "is not listed in follow_up_phases")

    def test_untracked_implicated_artifact_rejected(self):
        data = self.mutated()
        self.first_violation(data)["violation_reproduction"]["artifacts"] = [
            "plugins/playbook/scripts/ghost-logger.sh"]
        self.assertRejectedThroughBothPaths(
            data, "implicated artifact is not tracked")

    def test_critical_known_violation_cannot_rest_on_manual_judgment(self):
        data = self.mutated()
        v = self.first_violation(data)["violation_reproduction"]
        v["type"] = "manual/human judgment"
        v["path"] = "docs/architecture.md"
        v["reference"] = "section:Per-user lanes"
        self.assertRejectedThroughBothPaths(
            data, "must be reproduced by executable or live-platform evidence")

    def test_reproduction_cannot_repeat_one_of_its_own_proofs(self):
        data = self.mutated()
        entry = self.first_violation(data)
        entry["violation_reproduction"]["path"] = entry["proofs"][1]["path"]
        entry["violation_reproduction"]["reference"] = entry["proofs"][1]["reference"]
        self.assertRejectedThroughBothPaths(
            data, "cannot both prove the guarantee and reproduce its violation")

    def test_non_violation_status_may_not_carry_a_reproduction(self):
        data = self.mutated()
        source = copy.deepcopy(self.first_violation(data)["violation_reproduction"])
        green = next(e for e in data["guarantees"]
                     if e["status"] == "verified_by_current_executable_evidence")
        green["violation_reproduction"] = source
        self.assertRejectedThroughBothPaths(
            data, "only a known_violation may carry a violation_reproduction")

    def test_known_violation_is_not_counted_as_verified(self):
        entries = self.ledger["guarantees"]
        violations = [e for e in entries if e["status"] == "known_violation"]
        self.assertTrue(violations)
        for entry in violations:
            self.assertNotEqual(
                "verified_by_current_executable_evidence", entry["status"])
        summary = MODULE.coverage_summary(self.ledger)
        self.assertIn(f"Known violations of a stated guarantee: {len(violations)}", summary)
        gaps = [e for e in entries
                if e["failure_consequence"] in {"Critical", "High"}
                and e["status"] != "verified_by_current_executable_evidence"]
        self.assertIn(f"Critical/High non-green gaps: {len(gaps)}", summary)
        for entry in violations:
            self.assertIn(entry["id"], summary)

    # ------------------------------- complete material schema-constraint audit
    def test_every_material_schema_constraint_is_enforced(self):
        """Each material value constraint in the published schema is rejected
        by the custom validator when violated in isolation."""
        for pointer, mutate, fragment in _SCHEMA_CONSTRAINT_MUTANTS:
            with self.subTest(constraint=pointer):
                data = self.mutated()
                mutate(self, data)
                self.assertRejectedWith(data, fragment)

    def test_schema_constraint_inventory_is_complete(self):
        """A new material schema constraint must come with an enforcing mutant."""
        schema = MODULE.load_json_strict(ROOT / "docs" / "guarantee-ledger.schema.json")
        found = set()

        def walk(node, ptr):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in _MATERIAL_KEYWORDS:
                        found.add(f"{ptr}#{key}")
                    else:
                        walk(value, f"{ptr}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{ptr}/{index}")

        walk(schema, "")
        covered = {pointer for pointer, _, _ in _SCHEMA_CONSTRAINT_MUTANTS}
        self.assertEqual(
            set(), found - covered,
            "schema constraints with no enforcing validator mutant",
        )
        self.assertEqual(
            set(), covered - found,
            "mutants for schema constraints that no longer exist",
        )


_MATERIAL_KEYWORDS = {
    "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
    "minimum", "maximum", "const", "pattern", "enum", "format",
}


def _entry(data, index=0):
    return data["guarantees"][index]


def _live(test, data):
    return test.first_with_live_evidence(data)["required_live_evidence"][0]


def _semantic(test, data):
    return test.first_semantic(data)["semantic_protocol"]


# (schema pointer#keyword, mutation, expected rejection fragment)
_SCHEMA_CONSTRAINT_MUTANTS = [
    ("/properties/$schema#const",
     lambda t, d: d.__setitem__("$schema", "something-else.json"),
     "$schema must equal guarantee-ledger.schema.json"),
    ("/properties/schema_version#const",
     lambda t, d: d.__setitem__("schema_version", 2),
     "schema_version must equal 1"),
    ("/properties/ledger_version#format",
     lambda t, d: d.__setitem__("ledger_version", "2026-02-30"),
     "canonical YYYY-MM-DD date"),
    ("/properties/ledger_version#pattern",
     lambda t, d: d.__setitem__("ledger_version", "18-08-2026"),
     "canonical YYYY-MM-DD date"),
    ("/properties/guarantees#minItems",
     lambda t, d: d.__setitem__("guarantees", []),
     "must contain at least one entry"),
    ("/$defs/guarantee/properties/id#pattern",
     lambda t, d: _entry(d).__setitem__("id", "pb lowercase id"),
     "must be a stable PB-* identifier"),
    ("/$defs/guarantee/properties/category#enum",
     lambda t, d: _entry(d).__setitem__("category", "misc"),
     "unsupported category"),
    ("/$defs/guarantee/properties/statement#minLength",
     lambda t, d: _entry(d).__setitem__("statement", ""),
     "statement must be a non-empty string"),
    ("/$defs/guarantee/properties/owner#minItems",
     lambda t, d: _entry(d).__setitem__("owner", []),
     "owner must be a non-empty array"),
    ("/$defs/guarantee/properties/owner#uniqueItems",
     lambda t, d: _entry(d)["owner"].append(copy.deepcopy(_entry(d)["owner"][0])),
     "duplicate owner binding"),
    ("/$defs/guarantee/properties/failure_consequence#enum",
     lambda t, d: _entry(d).__setitem__("failure_consequence", "Severe"),
     "invalid failure_consequence"),
    ("/$defs/guarantee/properties/claim_kind#enum",
     lambda t, d: _entry(d).__setitem__("claim_kind", "vibes"),
     "invalid claim_kind"),
    ("/$defs/guarantee/properties/proofs#minItems",
     lambda t, d: _entry(d).__setitem__("proofs", []),
     "proofs must be a non-empty array"),
    ("/$defs/guarantee/properties/applicable_platforms#minItems",
     lambda t, d: _entry(d).__setitem__("applicable_platforms", []),
     "applicable_platforms must be a non-empty string array"),
    ("/$defs/guarantee/properties/applicable_platforms#uniqueItems",
     lambda t, d: _entry(d).__setitem__("applicable_platforms", ["linux", "linux"]),
     "duplicate platform names"),
    ("/$defs/guarantee/properties/applicable_platforms/items#enum",
     lambda t, d: _entry(d).__setitem__("applicable_platforms", ["plan9"]),
     "unsupported platform names"),
    ("/$defs/guarantee/properties/applicable_providers#minItems",
     lambda t, d: _entry(d).__setitem__("applicable_providers", []),
     "applicable_providers must be a non-empty string array"),
    ("/$defs/guarantee/properties/applicable_providers#uniqueItems",
     lambda t, d: _entry(d).__setitem__("applicable_providers", ["claude", "claude"]),
     "duplicate provider names"),
    ("/$defs/guarantee/properties/applicable_providers/items#enum",
     lambda t, d: _entry(d).__setitem__("applicable_providers", ["imaginary"]),
     "unsupported provider names"),
    ("/$defs/guarantee/properties/status#enum",
     lambda t, d: _entry(d).__setitem__("status", "looks-good"),
     "invalid status"),
    ("/$defs/guarantee/properties/missing_evidence_or_limitation/items#minLength",
     lambda t, d: _entry(d).__setitem__("missing_evidence_or_limitation", ["   "]),
     "missing_evidence_or_limitation must be a string array"),
    ("/$defs/guarantee/properties/follow_up_phases#uniqueItems",
     lambda t, d: _entry(d).__setitem__("follow_up_phases", [8, 8]),
     "duplicate follow-up phases"),
    ("/$defs/guarantee/properties/follow_up_phases/items#minimum",
     lambda t, d: _entry(d).__setitem__("follow_up_phases", [1]),
     "follow_up_phases must contain phase integers 2..11"),
    ("/$defs/guarantee/properties/follow_up_phases/items#maximum",
     lambda t, d: _entry(d).__setitem__("follow_up_phases", [12]),
     "follow_up_phases must contain phase integers 2..11"),
    ("/$defs/proof/properties/type#enum",
     lambda t, d: _entry(d)["proofs"][0].__setitem__("type", "a test exists"),
     "invalid proof type"),
    ("/$defs/proof/properties/reference#minLength",
     lambda t, d: _entry(d)["proofs"][0].__setitem__("reference", ""),
     "reference must be a non-empty string"),
    ("/$defs/proof/properties/boundary#enum",
     lambda t, d: _entry(d)["proofs"][0].__setitem__("boundary", "vibes"),
     "invalid boundary"),
    ("/$defs/proof/properties/limitations/items#minLength",
     lambda t, d: _entry(d)["proofs"][0].__setitem__("limitations", [" "]),
     "limitations must be a string array"),
    ("/$defs/ownerBinding/properties/path#minLength",
     lambda t, d: _entry(d)["owner"][0].__setitem__("path", ""),
     "owner path is not a tracked file"),
    ("/$defs/ownerBinding/properties/reference#minLength",
     lambda t, d: _entry(d)["owner"][0].__setitem__("reference", ""),
     "owner reference must be"),
    ("/$defs/ownerBinding/properties/reference#pattern",
     lambda t, d: _entry(d)["owner"][0].__setitem__("reference", "cmd_work"),
     "owner reference must be"),
    ("/$defs/negativeControl/properties/path#minLength",
     lambda t, d: _control(t, d).__setitem__("path", ""),
     "evidence path is not a tracked file"),
    ("/$defs/negativeControl/properties/reference#minLength",
     lambda t, d: _control(t, d).__setitem__("reference", ""),
     "reference must be a non-empty string"),
    ("/$defs/liveEvidence/properties/type#enum",
     lambda t, d: _live(t, d).__setitem__("type", "vibes test"),
     "invalid live evidence type"),
    ("/$defs/liveEvidence/properties/targets#minItems",
     lambda t, d: _live(t, d).__setitem__("targets", []),
     "unsupported or empty live targets"),
    ("/$defs/liveEvidence/properties/targets#uniqueItems",
     lambda t, d: _live(t, d)["targets"].append(_live(t, d)["targets"][0]),
     "duplicate live targets"),
    ("/$defs/liveEvidence/properties/targets/items#enum",
     lambda t, d: _live(t, d).__setitem__("targets", ["plan9"]),
     "unsupported or empty live targets"),
    ("/$defs/liveEvidence/properties/phase#const",
     lambda t, d: _live(t, d).__setitem__("phase", 7),
     "must be scheduled for Phase 8"),
    ("/$defs/liveEvidence/properties/reason#minLength",
     lambda t, d: _live(t, d).__setitem__("reason", "  "),
     "reason must be a non-empty string"),
    ("/$defs/semanticProtocol/properties/protocol#minLength",
     lambda t, d: _semantic(t, d).__setitem__("protocol", ""),
     "semantic protocol must name a protocol"),
    ("/$defs/semanticProtocol/properties/repeated_samples#minimum",
     lambda t, d: _semantic(t, d).__setitem__("repeated_samples", 1),
     "semantic protocol must name a protocol"),
    ("/$defs/semanticProtocol/properties/bounded_conclusion#minLength",
     lambda t, d: _semantic(t, d).__setitem__("bounded_conclusion", ""),
     "semantic protocol must name a protocol"),
]


def _control(test, data):
    return next(p["negative_control"] for e in data["guarantees"] for p in e["proofs"]
                if p["negative_control"])


def _violation(test, data):
    return test.first_violation(data)["violation_reproduction"]


_SCHEMA_CONSTRAINT_MUTANTS += [
    ("/$defs/violationReproduction/properties/type#enum",
     lambda t, d: _violation(t, d).__setitem__("type", "vibes reproduction"),
     "invalid reproduction type"),
    ("/$defs/violationReproduction/properties/path#minLength",
     lambda t, d: _violation(t, d).__setitem__("path", ""),
     "evidence path is not a tracked file"),
    ("/$defs/violationReproduction/properties/reference#minLength",
     lambda t, d: _violation(t, d).__setitem__("reference", ""),
     "reference must be a non-empty string"),
    ("/$defs/violationReproduction/properties/invocation#minLength",
     lambda t, d: _violation(t, d).__setitem__("invocation", "  "),
     "invocation must be a non-empty string"),
    ("/$defs/violationReproduction/properties/observed#minLength",
     lambda t, d: _violation(t, d).__setitem__("observed", ""),
     "observed must be a non-empty string"),
    ("/$defs/violationReproduction/properties/platforms#minItems",
     lambda t, d: _violation(t, d).__setitem__("platforms", []),
     "unsupported or empty reproduced platforms"),
    ("/$defs/violationReproduction/properties/platforms#uniqueItems",
     lambda t, d: _violation(t, d).__setitem__("platforms", ["linux", "linux"]),
     "duplicate reproduced platforms"),
    ("/$defs/violationReproduction/properties/platforms/items#enum",
     lambda t, d: _violation(t, d).__setitem__("platforms", ["plan9"]),
     "unsupported or empty reproduced platforms"),
    ("/$defs/violationReproduction/properties/artifacts#minItems",
     lambda t, d: _violation(t, d).__setitem__("artifacts", []),
     "artifacts must be a non-empty string array"),
    ("/$defs/violationReproduction/properties/artifacts#uniqueItems",
     lambda t, d: _violation(t, d).__setitem__(
         "artifacts", ["plugins/playbook/scripts/bash-log.sh"] * 2),
     "duplicate implicated artifacts"),
    ("/$defs/violationReproduction/properties/artifacts/items#minLength",
     lambda t, d: _violation(t, d).__setitem__("artifacts", ["  "]),
     "artifacts must be a non-empty string array"),
    ("/$defs/violationReproduction/properties/phase#minimum",
     lambda t, d: _violation(t, d).__setitem__("phase", 1),
     "phase must be a follow-up phase integer 2..11"),
    ("/$defs/violationReproduction/properties/phase#maximum",
     lambda t, d: _violation(t, d).__setitem__("phase", 12),
     "phase must be a follow-up phase integer 2..11"),
]


class RuleMutationCoverage(_LedgerFixture):
    """Negative controls for validator rules that the original module left
    unexercised.

    Measured by neutralising each `errors.append(...)` / `return [...]` arm in a
    /tmp copy of scripts/guarantee_ledger.py (with its ROOT pinned to the real
    worktree, or every CLI-path test errors in `git ls-files` and masquerades as
    detection) and running this module against the copy. 77 of the validator's
    106 diagnostic arms were already detected; these close the remainder that a
    ledger mutation can reach.

    The four I/O arms - `cannot parse Python owner/evidence`, `cannot read owner
    file`, `cannot parse JSON owner` - cannot be reached by mutating the real
    ledger, because no tracked file is unparseable or non-UTF-8-readable in a
    position an owner may point at. They are reached here instead through a
    synthetic ``root``/``tracked`` pair under a temporary directory, so the
    repository never has to carry a deliberately corrupt fixture.
    """

    # ---------------------------------------------------------- synthetic root
    def synthetic(self, files, entry_patch):
        """Validate a one-entry ledger against a throwaway root/tracked pair."""
        base = copy.deepcopy(self.ledger)
        entry = copy.deepcopy(base["guarantees"][0])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(body, bytes):
                    target.write_bytes(body)
                else:
                    target.write_text(body, encoding="utf-8")
            entry_patch(entry)
            base["guarantees"] = [entry]
            return MODULE.validate_ledger(base, root=root, tracked=set(files))

    def assertSynthetic(self, files, entry_patch, fragment):
        errors = self.synthetic(files, entry_patch)
        self.assertTrue(errors, "synthetic mutant unexpectedly passed validation")
        self.assertTrue(any(fragment in e for e in errors),
                        f"expected {fragment!r} in {errors!r}")

    # -------------------------------------------------- unreadable/unparseable
    def test_unparseable_python_owner_is_reported_not_crashed(self):
        self.assertSynthetic(
            {"broken.py": "def (:\n"},
            lambda e: e.__setitem__("owner", [{"path": "broken.py",
                                               "reference": "symbol:anything"}]),
            "cannot parse Python owner")

    def test_unparseable_python_evidence_is_reported_not_crashed(self):
        def patch(entry):
            entry["proofs"] = [{"type": "executable unit",
                                "path": "tests/test_broken.py",
                                "reference": "Klass.test_x", "boundary": "unit",
                                "negative_control": None, "limitations": []}]
            entry["status"] = "partially_evidenced"
            entry["missing_evidence_or_limitation"] = ["x"]
            entry["follow_up_phases"] = [3]
            entry["required_live_evidence"] = []
        self.assertSynthetic({"tests/test_broken.py": "class (:\n",
                              "owner.py": "x = 1\n"},
                             lambda e: (patch(e), e.__setitem__(
                                 "owner", [{"path": "owner.py",
                                            "reference": "symbol:x"}]))[0],
            "cannot parse Python evidence")

    def test_unreadable_owner_file_is_reported_not_crashed(self):
        # A .md is classified markdown by extension, so a non-UTF-8 body reaches
        # _read_text rather than being downgraded to an opaque kind.
        self.assertSynthetic(
            {"broken.md": b"\xff\xfe not utf-8"},
            lambda e: e.__setitem__("owner", [{"path": "broken.md",
                                               "reference": "section:Whatever"}]),
            "cannot read owner file")

    def test_unparseable_json_owner_is_reported_not_crashed(self):
        self.assertSynthetic(
            {"broken.json": "{not json"},
            lambda e: e.__setitem__("owner", [{"path": "broken.json",
                                               "reference": "pointer:/a"}]),
            "cannot parse JSON owner")

    # ------------------------------------------------ owner scheme/kind guards
    def test_shell_scheme_owner_on_a_non_shell_file_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [{"path": "docs/cli.md",
                                           "reference": "function:cmd_work"}]
        self.assertRejectedWith(
            data, "owner references are only defined for shell files")

    def test_section_owner_on_a_non_markdown_file_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/scripts/bash-log.sh",
             "reference": "section:Whatever"}]
        self.assertRejectedWith(
            data, "owner references are only defined for Markdown files")

    def test_case_arm_owner_on_a_shell_file_without_a_case_block_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"] = [
            {"path": "plugins/playbook/scripts/monitor-lib/bootstrap.sh",
             "reference": "case-arm:Freehand*"}]
        self.assertRejectedWith(data, "contains no case block to own an arm in")

    # ------------------------------------------------ evidence path/kind rules
    def test_python_evidence_outside_tests_rejected(self):
        data = self.mutated()
        proof = self.first_real_proof(data)
        proof["path"] = "plugins/playbook/tasks/core.py"
        proof["reference"] = "test_whatever"
        self.assertRejectedWith(
            data, "Python executable evidence must be a tracked tests/test_*.py file")

    def test_shell_evidence_outside_tests_rejected(self):
        data = self.mutated()
        proof = self.first_real_proof(data)
        proof["path"] = "plugins/playbook/scripts/bash-log.sh"
        proof["reference"] = "scenario:S1"
        self.assertRejectedWith(
            data, "shell executable evidence must be a tracked tests/ scenario")

    def test_structurally_malformed_python_reference_rejected(self):
        data = self.mutated()
        proof = self.first_real_proof(data)
        proof["path"] = "tests/test_cli_dispatch.py"
        proof["reference"] = "Class.method.extra"
        self.assertRejectedWith(data, "exact Python test reference required")

    def test_document_section_evidence_must_exist(self):
        data = self.mutated()
        proof = self.first_real_proof(data)
        proof["path"] = "README.md"
        proof["reference"] = "section:No Such Heading Anywhere"
        self.assertRejectedWith(data, "not found in README.md")

    def test_document_section_evidence_must_be_unique(self):
        data = self.mutated()
        proof = self.first_real_proof(data)
        proof["path"] = "CHANGELOG.md"
        proof["reference"] = "section:Fixed"
        self.assertRejectedWith(data, "is not unique in CHANGELOG.md")

    # ------------------------------------------------------ structural shapes
    def test_missing_top_level_field_rejected(self):
        data = self.mutated()
        del data["ledger_version"]
        self.assertRejectedWith(data, "ledger missing required field: ledger_version")

    def test_unsupported_top_level_field_rejected(self):
        data = self.mutated()
        data["extra_top_level"] = 1
        self.assertRejectedWith(
            data, "ledger contains unsupported field: extra_top_level")

    def test_guarantees_must_be_an_array(self):
        data = self.mutated()
        data["guarantees"] = {"not": "an array"}
        self.assertRejectedWith(data, "guarantees must be an array")

    def test_non_object_guarantee_entry_rejected(self):
        data = self.mutated()
        data["guarantees"].append("not an object")
        self.assertRejectedWith(data, "must be an object")

    def test_owner_binding_with_extra_key_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["owner"][0]["note"] = "smuggled"
        self.assertRejectedWith(data, "must contain exactly path and reference")

    def test_non_object_proof_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["proofs"].append("not an object")
        self.assertRejectedWith(data, "must be an object")

    def test_proof_missing_required_field_rejected(self):
        data = self.mutated()
        del data["guarantees"][0]["proofs"][0]["boundary"]
        self.assertRejectedWith(data, "missing required field: boundary")

    def test_proof_with_unsupported_field_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["proofs"][0]["vibe"] = "good"
        self.assertRejectedWith(data, "contains unsupported field: vibe")

    def test_proof_with_blank_reference_rejected(self):
        data = self.mutated()
        data["guarantees"][0]["proofs"][0]["reference"] = "   "
        self.assertRejectedWith(data, "reference must be a non-empty string")

    # --------------------------------------------------- missing-evidence arms
    def first_missing_proof(self, data):
        return next(p for e in data["guarantees"] for p in e["proofs"]
                    if p["type"] == "missing")

    def test_missing_evidence_with_a_path_rejected(self):
        data = self.mutated()
        self.first_missing_proof(data)["path"] = "tests/test_cli_dispatch.py"
        self.assertRejectedWith(data, "missing evidence must have null path")

    def test_missing_evidence_with_a_real_boundary_rejected(self):
        data = self.mutated()
        self.first_missing_proof(data)["boundary"] = "subprocess"
        self.assertRejectedWith(data, "missing evidence must use boundary 'none'")

    def test_missing_evidence_with_a_negative_control_rejected(self):
        data = self.mutated()
        self.first_missing_proof(data)["negative_control"] = {
            "path": "tests/test_cli_dispatch.py",
            "reference": "DispatchLiveSmoke.test_rejection_branch_still_rejects"}
        self.assertRejectedWith(
            data, "missing evidence must have null negative_control")

    # ------------------------------------------------------- status/live rules
    def test_verified_status_on_non_executable_evidence_rejected(self):
        data = self.mutated()
        entry = next(e for e in data["guarantees"]
                     if e["status"] == "verified_by_current_executable_evidence"
                     and e["failure_consequence"] not in {"Critical", "High"})
        for proof in entry["proofs"]:
            proof["type"] = "manual/human judgment"
            proof["boundary"] = "human-review"
            proof["negative_control"] = None
        self.assertRejectedWith(data, "verified status requires executable evidence")

    def test_required_live_evidence_must_be_an_array(self):
        data = self.mutated()
        data["guarantees"][0]["required_live_evidence"] = {"type": "live-provider test"}
        self.assertRejectedWith(data, "required_live_evidence must be an array")

    def test_live_evidence_record_with_extra_key_rejected(self):
        data = self.mutated()
        _live(self, data)["note"] = "smuggled"
        self.assertRejectedWith(
            data, "must contain exactly type, targets, phase, reason")

    def test_live_evidence_not_scheduled_in_follow_up_phases_rejected(self):
        data = self.mutated()
        entry = self.first_with_live_evidence(data)
        entry["follow_up_phases"] = [p for p in entry["follow_up_phases"] if p != 8]
        if not entry["follow_up_phases"]:
            entry["follow_up_phases"] = [3]
        self.assertRejectedWith(
            data, "required live evidence is not scheduled in follow_up_phases Phase 8")

    def test_mechanical_claim_with_a_semantic_protocol_rejected(self):
        data = self.mutated()
        entry = next(e for e in data["guarantees"] if e["claim_kind"] == "mechanical")
        entry["semantic_protocol"] = {"protocol": "p", "repeated_samples": 5,
                                      "bounded_conclusion": "b"}
        self.assertRejectedWith(
            data, "mechanical claim must use null semantic_protocol")


    # ------------------------------- arms reached only through the schema seam
    #
    # `validate_schema_contract` runs before `validate_ledger` in `main`, so its
    # arms are unreachable through the ledger mutants above. Each needs its own
    # control, and the CLI assertion matters as much as the API one: a silent
    # schema-contract pass lets the real ledger validate against a schema that no
    # longer documents it.
    def test_non_object_schema_root_rejected(self):
        errors = MODULE.validate_schema_contract([])
        self.assertTrue(any("schema root must be an object" in e for e in errors),
                        errors)
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text("[]", encoding="utf-8")
            stderr, stdout = StringIO(), StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                rc = MODULE.main(["--ledger", str(ROOT / "docs" / "guarantee-ledger.json"),
                                  "--schema", str(schema)])
            self.assertEqual(2, rc, f"CLI accepted a non-object schema: {stderr.getvalue()}")
            self.assertIn("schema root must be an object", stderr.getvalue())

    def test_schema_missing_a_contract_key_rejected(self):
        # `additionalProperties` is not a material value constraint, so
        # test_schema_constraint_inventory_is_complete cannot see it go missing;
        # only the `schema missing …` arm can.
        schema = copy.deepcopy(self.schema)
        del schema["$defs"]["proof"]["additionalProperties"]
        errors = MODULE.validate_schema_contract(schema)
        self.assertTrue(
            any("schema missing proof additionalProperties" in e for e in errors),
            errors)

    def test_scalar_schema_value_drift_rejected(self):
        # The set-valued drift arm is covered by
        # test_material_schema_validator_drift_rejected; this is the scalar arm.
        schema = copy.deepcopy(self.schema)
        schema["properties"]["schema_version"]["const"] = 2
        errors = MODULE.validate_schema_contract(schema)
        self.assertTrue(
            any("schema version const drift: expected 1, got 2" in e for e in errors),
            errors)

    # --------------------------- whole-file owner I/O arms (synthetic root only)
    #
    # The `symbol:`/`section:` variants of these two failures are covered above.
    # These are the distinct `whole-file` arms: a Python module that cannot be
    # parsed while deciding whether it has a finer seam, and a Markdown owner that
    # cannot be decoded while deciding whether it has a heading.
    def test_unparseable_python_whole_file_owner_is_reported_not_crashed(self):
        self.assertSynthetic(
            {"broken.py": "def (:\n"},
            lambda e: e.__setitem__("owner", [{"path": "broken.py",
                                               "reference": "whole-file"}]),
            "cannot parse Python owner")

    def test_unreadable_markdown_whole_file_owner_is_reported_not_crashed(self):
        self.assertSynthetic(
            {"broken.md": b"\xff\xfe not utf-8"},
            lambda e: e.__setitem__("owner", [{"path": "broken.md",
                                               "reference": "whole-file"}]),
            "cannot read owner file")

    # ------------------------------- blank reference on a `missing`-type proof
    #
    # `_reference_errors` is SKIPPED for proofs whose type is "missing" (they
    # carry a null path), so the blank-reference check inside validate_ledger is
    # the SOLE enforcer for this case — not a redundant copy of the one in
    # `_reference_errors`. Removing it alone is a real fail-open, which is why
    # this control exists separately from
    # test_proof_with_blank_reference_rejected (that one blanks a NON-missing
    # proof and is caught by either enforcer).
    def test_missing_type_proof_with_a_blank_reference_rejected(self):
        data = self.mutated()
        self.first_missing_proof(data)["reference"] = "   "
        self.assertRejectedWith(data, "reference must be a non-empty string")

    # ------------------------- the one arm with no reachable input, pinned as such
    def test_ledger_version_round_trip_arm_is_unreachable_by_construction(self):
        """`parsed_version.isoformat() != ledger_version` cannot fire.

        Its two guards leave it nothing to catch: the `\\d{4}-\\d{2}-\\d{2}`
        fullmatch admits only a 4-2-2 digit layout, and `date.fromisoformat`
        rejects every such string it cannot round-trip (non-ASCII digits
        included, on the declared 3.10 floor). The arm is deliberate
        defence-in-depth against a future, laxer `fromisoformat`; this test pins
        the claim so the guide's coverage statement stays mechanically true.
        """
        reachable = []
        candidates = [f"{y:04d}-01-01" for y in range(10000)]
        for year in (1900, 2000, 2024, 2026, 9999):
            candidates += [f"{year:04d}-{mo:02d}-{da:02d}"
                           for mo in range(100) for da in range(100)]
        candidates += ["٢٠٢٦-٠٨-١٩",  # Arabic-Indic 2026-08-19
                       "２０２６-０８-１９"]  # fullwidth 2026-08-19
        for value in candidates:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                continue
            try:
                parsed = datetime.date.fromisoformat(value)
            except ValueError:
                continue
            if parsed.isoformat() != value:
                reachable.append(value)
        self.assertEqual([], reachable,
                         "the round-trip arm is reachable after all; give it a control")
        # The contract it guards IS enforced, by the two arms that can fire.
        for bad in ("2026-8-19", "2026-02-30", "not-a-date",
                    "٢٠٢٦-٠٨-١٩"):
            data = self.mutated()
            data["ledger_version"] = bad
            self.assertRejectedWith(data, "canonical YYYY-MM-DD date")

if __name__ == "__main__":
    unittest.main()
