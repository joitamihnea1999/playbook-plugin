#!/usr/bin/env python3
"""Validate and summarize Playbook's machine-readable guarantee ledger."""

from __future__ import annotations

import argparse
import ast
import collections
import datetime
import functools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "guarantee-ledger.json"
SCHEMA = ROOT / "docs" / "guarantee-ledger.schema.json"

EVIDENCE_TYPES = {
    "executable unit",
    "executable integration",
    "packaged-install test",
    "live-provider test",
    "live-platform test",
    "manual/human judgment",
    "missing",
}
STATUSES = {
    "verified_by_current_executable_evidence",
    "partially_evidenced",
    "unverified",
    "missing_evidence",
    "known_violation",
    "not_applicable",
}
NON_GREEN_STATUSES = {
    "partially_evidenced",
    "unverified",
    "missing_evidence",
    "known_violation",
}
CONSEQUENCES = {"Critical", "High", "Medium", "Low"}
CLAIM_KINDS = {"mechanical", "semantic"}
PLATFORMS = {"linux", "macos", "windows-git-bash"}
PROVIDERS = {"claude", "codex", "antigravity", "grok", "pi"}
BOUNDARIES = {
    "unit",
    "subprocess",
    "real-os",
    "packaged-install",
    "live-provider",
    "live-platform",
    "human-review",
    "none",
}
CATEGORIES = {
    "task-lifecycle",
    "risk-and-close",
    "hook-payload-and-paths",
    "command-safety",
    "sessions-and-attribution",
    "monitor",
    "judges-and-reviews",
    "providers-and-hooks",
    "installation-and-upgrade",
    "sandbox",
    "mind-map",
    "merge",
    "configuration",
    "doctor",
    "compatibility-and-package",
    "cli-contracts",
    "performance",
}
REQUIRED_ENTRY_FIELDS = {
    "id",
    "category",
    "statement",
    "owner",
    "failure_consequence",
    "claim_kind",
    "proofs",
    "applicable_platforms",
    "applicable_providers",
    "status",
    "missing_evidence_or_limitation",
    "follow_up_phases",
    "required_live_evidence",
    "semantic_protocol",
    "violation_reproduction",
}
REQUIRED_PROOF_FIELDS = {
    "type",
    "path",
    "reference",
    "boundary",
    "negative_control",
    "limitations",
}
TOP_LEVEL_FIELDS = {"$schema", "schema_version", "ledger_version", "guarantees"}
NEGATIVE_CONTROL_FIELDS = {"path", "reference"}
OWNER_BINDING_FIELDS = {"path", "reference"}
VIOLATION_FIELDS = {
    "type",
    "path",
    "reference",
    "invocation",
    "observed",
    "platforms",
    "artifacts",
    "phase",
}
VIOLATION_TYPES = {
    "executable integration",
    "executable unit",
    "live-platform test",
    "manual/human judgment",
}
EXECUTABLE_VIOLATION_TYPES = {
    "executable integration",
    "executable unit",
    "live-platform test",
}
OWNER_REFERENCE_PATTERN = (
    r"^(?:symbol:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?"
    r"|function:[A-Za-z_][\w:.-]*"
    r"|case-arm:[A-Za-z0-9_*?.@%+=:,/\[\]{}-]+"
    r"|section:.+"
    r"|pointer:/.*"
    r"|whole-file)$"
)


def loads_json_strict(text: str, *, source: str) -> Any:
    """Parse JSON while rejecting duplicate members at every object depth."""
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{source}: duplicate object member {key!r}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicates)


def load_json_strict(path: Path) -> Any:
    return loads_json_strict(path.read_text(encoding="utf-8"), source=str(path))


def _schema_value(schema: dict[str, Any], dotted: str) -> Any:
    value: Any = schema
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def validate_schema_contract(schema: Any) -> list[str]:
    """Detect material drift between the published schema and custom checks."""
    if not isinstance(schema, dict):
        return ["schema root must be an object"]
    errors: list[str] = []

    def expect(path: str, expected: Any, label: str) -> None:
        try:
            actual = _schema_value(schema, path)
        except KeyError:
            errors.append(f"schema missing {label}: {path}")
            return
        if isinstance(expected, set):
            if isinstance(actual, dict):
                comparable = set(actual)
                duplicate_free = True
            elif isinstance(actual, list):
                comparable = set(actual)
                duplicate_free = len(actual) == len(comparable)
            else:
                comparable = None
                duplicate_free = False
            if comparable != expected or not duplicate_free:
                errors.append(f"schema {label} drift: expected {sorted(expected)!r}, got {actual!r}")
        elif actual != expected:
            errors.append(f"schema {label} drift: expected {expected!r}, got {actual!r}")

    expect("$schema", "https://json-schema.org/draft/2020-12/schema", "dialect")
    expect("type", "object", "top-level type")
    expect("additionalProperties", False, "top-level additionalProperties")
    expect("required", TOP_LEVEL_FIELDS, "top-level required fields")
    expect("properties", TOP_LEVEL_FIELDS, "top-level property set")
    expect("properties.$schema.const", "guarantee-ledger.schema.json", "ledger schema const")
    expect("properties.schema_version.const", 1, "schema version const")
    expect("properties.ledger_version.type", "string", "ledger_version type")
    expect("properties.ledger_version.format", "date", "ledger_version format")
    expect("properties.ledger_version.pattern", r"^\d{4}-\d{2}-\d{2}$", "ledger_version canonical pattern")
    expect("properties.guarantees.type", "array", "guarantees type")
    expect("properties.guarantees.minItems", 1, "guarantees minimum")
    expect("properties.guarantees.items.$ref", "#/$defs/guarantee", "guarantee item reference")
    for definition, fields in (
        ("guarantee", REQUIRED_ENTRY_FIELDS),
        ("proof", REQUIRED_PROOF_FIELDS),
        ("liveEvidence", {"type", "targets", "phase", "reason"}),
        ("semanticProtocol", {"protocol", "repeated_samples", "bounded_conclusion"}),
        ("negativeControl", NEGATIVE_CONTROL_FIELDS),
        ("ownerBinding", OWNER_BINDING_FIELDS),
        ("violationReproduction", VIOLATION_FIELDS),
    ):
        expect(f"$defs.{definition}.additionalProperties", False, f"{definition} additionalProperties")
        expect(f"$defs.{definition}.required", fields, f"{definition} required fields")
        expect(f"$defs.{definition}.properties", set(fields), f"{definition} property set")
        expect(f"$defs.{definition}.type", "object", f"{definition} type")
    expect("$defs.guarantee.properties.id.pattern", r"^PB-[A-Z0-9]+(?:-[A-Z0-9]+)*$", "id pattern")
    expect("$defs.guarantee.properties.id.type", "string", "id type")
    expect("$defs.guarantee.properties.category.enum", CATEGORIES, "category enum")
    expect("$defs.guarantee.properties.statement.type", "string", "statement type")
    expect("$defs.guarantee.properties.statement.minLength", 1, "statement minimum")
    expect("$defs.guarantee.properties.owner.type", "array", "owner type")
    expect("$defs.guarantee.properties.owner.minItems", 1, "owner minimum")
    expect("$defs.guarantee.properties.owner.uniqueItems", True, "owner uniqueness")
    expect("$defs.guarantee.properties.owner.items.$ref", "#/$defs/ownerBinding", "owner item reference")
    expect("$defs.ownerBinding.properties.path.type", "string", "owner path type")
    expect("$defs.ownerBinding.properties.path.minLength", 1, "owner path minimum")
    expect("$defs.ownerBinding.properties.reference.type", "string", "owner reference type")
    expect("$defs.ownerBinding.properties.reference.pattern", OWNER_REFERENCE_PATTERN, "owner reference pattern")
    expect("$defs.guarantee.properties.failure_consequence.enum", CONSEQUENCES, "failure consequence enum")
    expect("$defs.guarantee.properties.claim_kind.enum", CLAIM_KINDS, "claim kind enum")
    expect("$defs.guarantee.properties.proofs.type", "array", "proofs type")
    expect("$defs.guarantee.properties.proofs.minItems", 1, "proofs minimum")
    expect("$defs.guarantee.properties.proofs.items.$ref", "#/$defs/proof", "proof item reference")
    expect("$defs.guarantee.properties.applicable_platforms.items.enum", PLATFORMS, "platform enum")
    expect("$defs.guarantee.properties.applicable_providers.items.enum", PROVIDERS, "provider enum")
    for array_field in ("applicable_platforms", "applicable_providers"):
        array_base = f"$defs.guarantee.properties.{array_field}"
        expect(f"{array_base}.type", "array", f"{array_field} type")
        expect(f"{array_base}.minItems", 1, f"{array_field} minimum")
        expect(f"{array_base}.uniqueItems", True, f"{array_field} uniqueness")
    expect("$defs.guarantee.properties.status.enum", STATUSES, "status enum")
    expect("$defs.guarantee.properties.missing_evidence_or_limitation.type", "array", "limitations type")
    expect("$defs.guarantee.properties.missing_evidence_or_limitation.items.type", "string", "limitation item type")
    expect("$defs.guarantee.properties.missing_evidence_or_limitation.items.minLength", 1, "limitation item minimum")
    expect("$defs.guarantee.properties.follow_up_phases.type", "array", "follow-up type")
    expect("$defs.guarantee.properties.follow_up_phases.uniqueItems", True, "follow-up uniqueness")
    expect("$defs.guarantee.properties.follow_up_phases.items.type", "integer", "follow-up item type")
    expect("$defs.guarantee.properties.follow_up_phases.items.minimum", 2, "follow-up minimum")
    expect("$defs.guarantee.properties.follow_up_phases.items.maximum", 11, "follow-up maximum")
    expect("$defs.guarantee.properties.required_live_evidence.type", "array", "live evidence type")
    expect("$defs.guarantee.properties.required_live_evidence.items.$ref", "#/$defs/liveEvidence", "live evidence item reference")
    expect("$defs.guarantee.properties.semantic_protocol.anyOf", [
        {"type": "null"}, {"$ref": "#/$defs/semanticProtocol"}
    ], "semantic protocol shape")
    expect("$defs.guarantee.properties.violation_reproduction.anyOf", [
        {"type": "null"}, {"$ref": "#/$defs/violationReproduction"}
    ], "violation reproduction shape")
    expect("$defs.violationReproduction.properties.type.enum", VIOLATION_TYPES, "violation type enum")
    for violation_text in ("path", "reference", "invocation", "observed"):
        base = f"$defs.violationReproduction.properties.{violation_text}"
        expect(f"{base}.type", "string", f"violation {violation_text} type")
        expect(f"{base}.minLength", 1, f"violation {violation_text} minimum")
    expect("$defs.violationReproduction.properties.platforms.type", "array", "violation platforms type")
    expect("$defs.violationReproduction.properties.platforms.minItems", 1, "violation platforms minimum")
    expect("$defs.violationReproduction.properties.platforms.uniqueItems", True, "violation platforms uniqueness")
    expect("$defs.violationReproduction.properties.platforms.items.enum", PLATFORMS, "violation platform enum")
    expect("$defs.violationReproduction.properties.artifacts.type", "array", "violation artifacts type")
    expect("$defs.violationReproduction.properties.artifacts.minItems", 1, "violation artifacts minimum")
    expect("$defs.violationReproduction.properties.artifacts.uniqueItems", True, "violation artifacts uniqueness")
    expect("$defs.violationReproduction.properties.artifacts.items.type", "string", "violation artifact item type")
    expect("$defs.violationReproduction.properties.artifacts.items.minLength", 1, "violation artifact item minimum")
    expect("$defs.violationReproduction.properties.phase.type", "integer", "violation phase type")
    expect("$defs.violationReproduction.properties.phase.minimum", 2, "violation phase minimum")
    expect("$defs.violationReproduction.properties.phase.maximum", 11, "violation phase maximum")
    expect("$defs.proof.properties.type.enum", EVIDENCE_TYPES, "evidence type enum")
    expect("$defs.proof.properties.path.type", {"string", "null"}, "proof path type")
    expect("$defs.proof.properties.reference.type", "string", "proof reference type")
    expect("$defs.proof.properties.reference.minLength", 1, "proof reference minimum")
    expect("$defs.proof.properties.boundary.enum", BOUNDARIES, "boundary enum")
    expect("$defs.proof.properties.negative_control.anyOf", [
        {"type": "null"}, {"$ref": "#/$defs/negativeControl"}
    ], "negative-control shape")
    expect("$defs.proof.properties.limitations.type", "array", "proof limitations type")
    expect("$defs.proof.properties.limitations.items.type", "string", "proof limitation item type")
    expect("$defs.proof.properties.limitations.items.minLength", 1, "proof limitation item minimum")
    for nc_field in NEGATIVE_CONTROL_FIELDS:
        expect(f"$defs.negativeControl.properties.{nc_field}.type", "string", f"negative-control {nc_field} type")
        expect(f"$defs.negativeControl.properties.{nc_field}.minLength", 1, f"negative-control {nc_field} minimum")
    expect("$defs.liveEvidence.properties.type.enum", {"live-provider test", "live-platform test"}, "live evidence enum")
    expect("$defs.liveEvidence.properties.targets.type", "array", "live targets type")
    expect("$defs.liveEvidence.properties.targets.minItems", 1, "live targets minimum")
    expect("$defs.liveEvidence.properties.targets.uniqueItems", True, "live targets uniqueness")
    expect("$defs.liveEvidence.properties.targets.items.enum", PLATFORMS | PROVIDERS, "live target enum")
    expect("$defs.liveEvidence.properties.phase.const", 8, "live evidence phase")
    expect("$defs.liveEvidence.properties.reason.type", "string", "live reason type")
    expect("$defs.liveEvidence.properties.reason.minLength", 1, "live reason minimum")
    for semantic_field in ("protocol", "bounded_conclusion"):
        expect(f"$defs.semanticProtocol.properties.{semantic_field}.type", "string", f"semantic {semantic_field} type")
        expect(f"$defs.semanticProtocol.properties.{semantic_field}.minLength", 1, f"semantic {semantic_field} minimum")
    expect("$defs.semanticProtocol.properties.repeated_samples.type", "integer", "semantic sample type")
    expect("$defs.semanticProtocol.properties.repeated_samples.minimum", 2, "semantic sample minimum")
    return errors


@functools.lru_cache(maxsize=None)
def _tracked_paths(root: Path) -> frozenset[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, stderr=subprocess.DEVNULL
    )
    return frozenset(item.decode("utf-8") for item in output.split(b"\0") if item)


def _is_string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


@functools.lru_cache(maxsize=None)
def _python_targets(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    methods: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(f"{node.name}.{child.name}")
    return functions, methods


@functools.lru_cache(maxsize=None)
def _python_symbols(path: Path) -> set[str]:
    """Every module-level function, class, method, and constant name in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    def record_assign(node: ast.AST, prefix: str = "") -> None:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            return
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(f"{prefix}{target.id}")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{child.name}")
                else:
                    record_assign(child, f"{node.name}.")
        else:
            record_assign(node)
    return names


@functools.lru_cache(maxsize=None)
def _owner_file_kind(path: str, source_path: Path) -> str:
    """Classify an owner file so the right exact resolver applies."""
    if path.endswith(".py"):
        return "python"
    if path.endswith(".json"):
        return "json"
    if path.endswith((".md", ".template")):
        return "markdown"
    if path.endswith((".sh", ".bash", ".zsh")):
        return "shell"
    try:
        with source_path.open("r", encoding="utf-8", errors="strict") as handle:
            first = handle.readline()
    except (OSError, UnicodeDecodeError):
        return "opaque"
    if first.startswith("#!") and re.search(r"\b(?:ba|z|k|da)?sh\b", first):
        return "shell"
    return "opaque"


def _resolve_json_pointer(document: Any, pointer: str) -> bool:
    """Resolve an RFC 6901 JSON pointer against *document*."""
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(current):
                return False
            current = current[int(token)]
        else:
            return False
    return True


@functools.lru_cache(maxsize=None)
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


def _owner_errors(
    *, path: Any, reference: Any, root: Path, tracked: set[str], label: str
) -> list[str]:
    """Resolve one owner binding to an exact production seam."""
    if not isinstance(path, str) or path not in tracked:
        return [f"{label}: owner path is not a tracked file: {path!r}"]
    if not isinstance(reference, str) or not re.fullmatch(OWNER_REFERENCE_PATTERN, reference):
        return [
            f"{label}: owner reference must be symbol:/function:/case-arm:/section:/"
            f"pointer:/whole-file, got {reference!r}"
        ]
    source_path = root / path
    kind = _owner_file_kind(path, source_path)
    scheme = reference.split(":", 1)[0] if ":" in reference else reference

    if kind == "python" and scheme != "symbol":
        # A Python module that defines no module-level name has no finer seam to
        # bind; anything else must resolve through the AST.
        try:
            defines_something = bool(_python_symbols(source_path))
        except (OSError, SyntaxError) as exc:
            return [f"{label}: cannot parse Python owner {path}: {exc}"]
        if scheme != "whole-file" or defines_something:
            return [f"{label}: Python owners must use an AST-resolvable symbol: reference, got {reference!r}"]
        return []
    if scheme == "symbol" and kind != "python":
        return [f"{label}: symbol: owner references are only defined for Python files, got {path}"]
    if scheme in {"function", "case-arm"} and kind != "shell":
        return [f"{label}: {scheme}: owner references are only defined for shell files, got {path}"]
    if scheme == "section" and kind != "markdown":
        return [f"{label}: section: owner references are only defined for Markdown files, got {path}"]
    if scheme == "pointer" and kind != "json":
        return [f"{label}: pointer: owner references are only defined for JSON files, got {path}"]

    if scheme == "whole-file":
        if kind == "markdown":
            # A Markdown owner with headings has an exact seam; use it.
            try:
                source = _read_text(source_path)
            except (OSError, UnicodeDecodeError) as exc:
                return [f"{label}: cannot read owner file {path}: {exc}"]
            if re.search(r"^#+\s+\S", source, re.MULTILINE):
                return [
                    f"{label}: Markdown owners must name an exact unique section: heading, "
                    f"got {reference!r}"
                ]
        return []

    target = reference.split(":", 1)[1]
    if scheme == "symbol":
        try:
            symbols = _python_symbols(source_path)
        except (OSError, SyntaxError) as exc:
            return [f"{label}: cannot parse Python owner {path}: {exc}"]
        if target not in symbols:
            return [f"{label}: owner symbol not found: {path}::{target}"]
        return []

    try:
        source = _read_text(source_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{label}: cannot read owner file {path}: {exc}"]

    if scheme == "function":
        pattern = (
            rf"^\s*(?:function\s+)?{re.escape(target)}\s*\(\s*\)\s*\{{"
            rf"|^\s*function\s+{re.escape(target)}\s*\{{"
        )
        if not re.search(pattern, source, re.MULTILINE):
            return [f"{label}: owner shell function not found: {path}::{target}()"]
        return []
    if scheme == "case-arm":
        if not re.search(r"^\s*case\s.*\sin\s*$", source, re.MULTILINE):
            return [f"{label}: {path} contains no case block to own an arm in"]
        arm = re.escape(target)
        alt = r"[A-Za-z0-9_*?.@%+=:,/\[\]{}-]+"
        pattern = rf"^[ \t]*\(?(?:{alt}\|)*\"?{arm}\"?(?:\|{alt})*\)"
        if not re.search(pattern, source, re.MULTILINE):
            return [f"{label}: owner shell case arm not found: {path}::{target})"]
        return []
    if scheme == "section":
        matches = re.findall(rf"^#+\s+{re.escape(target)}\s*$", source, re.MULTILINE)
        if not matches:
            return [f"{label}: owner document section {target!r} not found in {path}"]
        if len(matches) != 1:
            return [f"{label}: owner document section {target!r} is not unique in {path}"]
        return []
    try:
        document = loads_json_strict(source, source=path)
    except ValueError as exc:
        return [f"{label}: cannot parse JSON owner {path}: {exc}"]
    if not _resolve_json_pointer(document, target):
        return [f"{label}: owner JSON pointer not found: {path}#{target}"]
    return []


def _reference_errors(
    *, path: Any, reference: Any, root: Path, tracked: set[str], label: str
) -> list[str]:
    errors: list[str] = []
    if not isinstance(path, str) or path not in tracked:
        return [f"{label}: evidence path is not a tracked file: {path!r}"]
    if not isinstance(reference, str) or not reference.strip():
        return [f"{label}: reference must be a non-empty string"]
    source_path = root / path
    if path.endswith(".py"):
        if not path.startswith("tests/test_"):
            return [f"{label}: Python executable evidence must be a tracked tests/test_*.py file"]
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", reference):
            return [f"{label}: exact Python test reference required (Class.method or function), got {reference!r}"]
        test_name = reference.rsplit(".", 1)[-1]
        if not test_name.startswith("test_"):
            return [f"{label}: exact Python test reference must name a test_* method or function"]
        try:
            functions, methods = _python_targets(source_path)
        except (OSError, SyntaxError) as exc:
            return [f"{label}: cannot parse Python evidence {path}: {exc}"]
        targets = methods if "." in reference else functions
        if reference not in targets:
            errors.append(f"{label}: exact Python test reference not found: {path}::{reference}")
    elif path.endswith((".sh", ".bash")):
        if not path.startswith("tests/"):
            return [f"{label}: shell executable evidence must be a tracked tests/ scenario"]
        match = re.fullmatch(r"scenario:([A-Z][A-Z0-9-]*)", reference)
        if not match:
            return [f"{label}: exact shell scenario reference required as scenario:ID"]
        source = _read_text(source_path)
        if not re.search(
            rf"^\s*(?:echo|printf)\s+[\"']=== {re.escape(match.group(1))}(?::|\s)",
            source,
            re.MULTILINE,
        ):
            errors.append(f"{label}: shell scenario {reference!r} not found in {path}")
    else:
        if not reference.startswith("section:") or not reference.removeprefix("section:").strip():
            return [f"{label}: exact document section reference required as section:Heading"]
        heading = reference.removeprefix("section:")
        source = _read_text(source_path)
        matches = re.findall(rf"^#+\s+{re.escape(heading)}\s*$", source, re.MULTILINE)
        if not matches:
            errors.append(f"{label}: document section {heading!r} not found in {path}")
        elif len(matches) != 1:
            errors.append(f"{label}: document section {heading!r} is not unique in {path}")
    return errors


def validate_ledger(
    data: Any, *, root: Path = ROOT, tracked: set[str] | None = None
) -> list[str]:
    """Return every structural and evidence-policy error in *data*."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["ledger root must be an object"]
    for key in ("$schema", "schema_version", "ledger_version", "guarantees"):
        if key not in data:
            errors.append(f"ledger missing required field: {key}")
    for key in sorted(data.keys() - TOP_LEVEL_FIELDS):
        errors.append(f"ledger contains unsupported field: {key}")
    if data.get("$schema") != "guarantee-ledger.schema.json":
        errors.append("$schema must equal guarantee-ledger.schema.json")
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    ledger_version = data.get("ledger_version")
    if not isinstance(ledger_version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ledger_version):
        errors.append("ledger_version must be a real canonical YYYY-MM-DD date")
    else:
        try:
            parsed_version = datetime.date.fromisoformat(ledger_version)
        except ValueError:
            errors.append("ledger_version must be a real canonical YYYY-MM-DD date")
        else:
            if parsed_version.isoformat() != ledger_version:
                errors.append("ledger_version must be a real canonical YYYY-MM-DD date")
    entries = data.get("guarantees")
    if not isinstance(entries, list):
        errors.append("guarantees must be an array")
        return errors
    if not entries:
        errors.append("guarantees must contain at least one entry")
        return errors

    tracked = _tracked_paths(root) if tracked is None else tracked
    seen: set[str] = set()
    seen_statements: dict[str, str] = {}
    for index, entry in enumerate(entries):
        prefix = f"guarantees[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = REQUIRED_ENTRY_FIELDS - entry.keys()
        for field in sorted(missing):
            errors.append(f"{prefix} missing required field: {field}")
        for field in sorted(entry.keys() - REQUIRED_ENTRY_FIELDS):
            errors.append(f"{prefix} contains unsupported field: {field}")
        gid = entry.get("id")
        label = gid if isinstance(gid, str) else prefix
        if not isinstance(gid, str) or not re.fullmatch(r"PB-[A-Z0-9]+(?:-[A-Z0-9]+)*", gid):
            errors.append(f"{prefix}.id must be a stable PB-* identifier")
        elif gid in seen:
            errors.append(f"duplicate guarantee id: {gid}")
        else:
            seen.add(gid)

        if entry.get("category") not in CATEGORIES:
            errors.append(f"{label}: unsupported category {entry.get('category')!r}")
        statement = entry.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            errors.append(f"{label}: statement must be a non-empty string")
        else:
            normalized = " ".join(statement.split())
            if normalized in seen_statements:
                errors.append(
                    f"{label}: statement duplicates {seen_statements[normalized]}; "
                    "consolidate or narrow one of them"
                )
            else:
                seen_statements[normalized] = label if isinstance(gid, str) else prefix
        if entry.get("failure_consequence") not in CONSEQUENCES:
            errors.append(f"{label}: invalid failure_consequence")
        if entry.get("claim_kind") not in CLAIM_KINDS:
            errors.append(f"{label}: invalid claim_kind")
        if entry.get("status") not in STATUSES:
            errors.append(f"{label}: invalid status")

        owner = entry.get("owner")
        if not isinstance(owner, list) or not owner:
            errors.append(f"{label}: owner must be a non-empty array of {{path, reference}} bindings")
        else:
            seen_bindings: set[tuple[str, str]] = set()
            for owner_index, binding in enumerate(owner):
                op = f"{label}.owner[{owner_index}]"
                if not isinstance(binding, dict) or set(binding) != OWNER_BINDING_FIELDS:
                    errors.append(f"{op}: must contain exactly path and reference")
                    continue
                errors.extend(_owner_errors(
                    path=binding.get("path"),
                    reference=binding.get("reference"),
                    root=root,
                    tracked=tracked,
                    label=op,
                ))
                key = (str(binding.get("path")), str(binding.get("reference")))
                if key in seen_bindings:
                    errors.append(f"{op}: duplicate owner binding {key[0]} :: {key[1]}")
                seen_bindings.add(key)

        platforms = entry.get("applicable_platforms")
        if not _is_string_list(platforms, nonempty=True):
            errors.append(f"{label}: applicable_platforms must be a non-empty string array")
        else:
            unknown = set(platforms) - PLATFORMS
            if unknown:
                errors.append(f"{label}: unsupported platform names: {sorted(unknown)}")
            if len(platforms) != len(set(platforms)):
                errors.append(f"{label}: duplicate platform names")
        providers = entry.get("applicable_providers")
        if not _is_string_list(providers, nonempty=True):
            errors.append(f"{label}: applicable_providers must be a non-empty string array")
        else:
            unknown = set(providers) - PROVIDERS
            if unknown:
                errors.append(f"{label}: unsupported provider names: {sorted(unknown)}")
            if len(providers) != len(set(providers)):
                errors.append(f"{label}: duplicate provider names")

        proofs = entry.get("proofs")
        if not isinstance(proofs, list) or not proofs:
            errors.append(f"{label}: proofs must be a non-empty array")
            proofs = []
        seen_proofs: set[tuple[Any, Any]] = set()
        for proof_index, proof in enumerate(proofs):
            pp = f"{label}.proofs[{proof_index}]"
            if not isinstance(proof, dict):
                errors.append(f"{pp} must be an object")
                continue
            for field in sorted(REQUIRED_PROOF_FIELDS - proof.keys()):
                errors.append(f"{pp} missing required field: {field}")
            for field in sorted(proof.keys() - REQUIRED_PROOF_FIELDS):
                errors.append(f"{pp} contains unsupported field: {field}")
            proof_type = proof.get("type")
            if proof_type not in EVIDENCE_TYPES:
                errors.append(f"{pp}: invalid proof type {proof_type!r}")
            if proof.get("boundary") not in BOUNDARIES:
                errors.append(f"{pp}: invalid boundary {proof.get('boundary')!r}")
            if not _is_string_list(proof.get("limitations")):
                errors.append(f"{pp}: limitations must be a string array")
            path = proof.get("path")
            reference = proof.get("reference")
            if proof_type == "missing":
                if path is not None:
                    errors.append(f"{pp}: missing evidence must have null path")
                if proof.get("boundary") != "none":
                    errors.append(f"{pp}: missing evidence must use boundary 'none'")
                if proof.get("negative_control") is not None:
                    errors.append(f"{pp}: missing evidence must have null negative_control")
            else:
                errors.extend(_reference_errors(
                    path=path, reference=reference, root=root, tracked=tracked, label=pp
                ))
            if not isinstance(reference, str) or not reference.strip():
                errors.append(f"{pp}: reference must be a non-empty string")
            if isinstance(path, (str, type(None))) and isinstance(reference, str):
                proof_key = (path, reference)
                if proof_key in seen_proofs:
                    errors.append(f"{pp}: duplicate proof reference {path} :: {reference}")
                seen_proofs.add(proof_key)
            negative_control = proof.get("negative_control")
            if negative_control is not None:
                if not isinstance(negative_control, dict) or set(negative_control) != NEGATIVE_CONTROL_FIELDS:
                    errors.append(f"{pp}: negative_control must be null or an exact evidence reference")
                else:
                    errors.extend(_reference_errors(
                        path=negative_control.get("path"),
                        reference=negative_control.get("reference"),
                        root=root,
                        tracked=tracked,
                        label=f"{pp}.negative_control",
                    ))
                    if (
                        negative_control.get("path") == path
                        and negative_control.get("reference") == reference
                    ):
                        errors.append(
                            f"{pp}: negative_control repeats its own proof "
                            f"({reference}); an adverse control must be a distinct target"
                        )

        limitations = entry.get("missing_evidence_or_limitation")
        if not _is_string_list(limitations):
            errors.append(f"{label}: missing_evidence_or_limitation must be a string array")
        phases = entry.get("follow_up_phases")
        if not isinstance(phases, list) or any(
            not isinstance(phase, int) or isinstance(phase, bool) or phase < 2 or phase > 11
            for phase in phases
        ):
            errors.append(f"{label}: follow_up_phases must contain phase integers 2..11")
            phases = []
        elif len(phases) != len(set(phases)):
            errors.append(f"{label}: duplicate follow-up phases")
        status = entry.get("status")
        if status in NON_GREEN_STATUSES:
            if not limitations:
                errors.append(f"{label}: non-green status requires an explicit limitation")
            if not phases:
                errors.append(f"{label}: non-green status requires a follow-up phase")
        if status == "verified_by_current_executable_evidence" and not any(
            proof.get("type", "").startswith("executable")
            or proof.get("type") in {"packaged-install test", "live-provider test", "live-platform test"}
            for proof in proofs
            if isinstance(proof, dict)
        ):
            errors.append(f"{label}: verified status requires executable evidence")

        if (
            entry.get("claim_kind") == "mechanical"
            and entry.get("failure_consequence") in {"Critical", "High"}
            and status == "verified_by_current_executable_evidence"
        ):
            integrations = [
                proof
                for proof in proofs
                if isinstance(proof, dict) and proof.get("type") == "executable integration"
            ]
            if not integrations:
                errors.append(
                    f"{label}: Critical/High mechanical verified claim lacks executable integration evidence"
                )
            if not any(isinstance(proof.get("negative_control"), dict) for proof in integrations):
                errors.append(
                    f"{label}: Critical/High mechanical verified claim lacks a targeted integration negative control"
                )

        live = entry.get("required_live_evidence")
        if not isinstance(live, list):
            errors.append(f"{label}: required_live_evidence must be an array")
            live = []
        for live_index, item in enumerate(live):
            lp = f"{label}.required_live_evidence[{live_index}]"
            if not isinstance(item, dict) or set(item) != {"type", "targets", "phase", "reason"}:
                errors.append(f"{lp}: must contain exactly type, targets, phase, reason")
                continue
            if item.get("type") not in {"live-provider test", "live-platform test"}:
                errors.append(f"{lp}: invalid live evidence type")
            allowed = PROVIDERS if item.get("type") == "live-provider test" else PLATFORMS
            targets = item.get("targets")
            if not _is_string_list(targets, nonempty=True) or set(targets or []) - allowed:
                errors.append(f"{lp}: unsupported or empty live targets")
            elif isinstance(targets, list) and len(targets) != len(set(targets)):
                errors.append(f"{lp}: duplicate live targets")
            if item.get("phase") != 8:
                errors.append(f"{lp}: required live evidence must be scheduled for Phase 8")
            if not isinstance(item.get("reason"), str) or not item.get("reason", "").strip():
                errors.append(f"{lp}: reason must be a non-empty string")
        if live and 8 not in phases:
            errors.append(f"{label}: required live evidence is not scheduled in follow_up_phases Phase 8")

        violation = entry.get("violation_reproduction")
        if status == "known_violation":
            if 2 not in phases and entry.get("failure_consequence") in {"Critical", "High"}:
                errors.append(
                    f"{label}: Critical/High known violation must schedule the runtime "
                    "correction in Phase 2"
                )
            if not isinstance(violation, dict) or set(violation) != VIOLATION_FIELDS:
                errors.append(
                    f"{label}: known_violation requires a violation_reproduction with exactly "
                    + ", ".join(sorted(VIOLATION_FIELDS))
                )
            else:
                vp = f"{label}.violation_reproduction"
                vtype = violation.get("type")
                if vtype not in VIOLATION_TYPES:
                    errors.append(f"{vp}: invalid reproduction type {vtype!r}")
                errors.extend(_reference_errors(
                    path=violation.get("path"),
                    reference=violation.get("reference"),
                    root=root,
                    tracked=tracked,
                    label=vp,
                ))
                for field in ("invocation", "observed"):
                    value = violation.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(f"{vp}: {field} must be a non-empty string")
                platforms = violation.get("platforms")
                if not _is_string_list(platforms, nonempty=True) or set(platforms or []) - PLATFORMS:
                    errors.append(f"{vp}: unsupported or empty reproduced platforms")
                elif isinstance(platforms, list) and len(platforms) != len(set(platforms)):
                    errors.append(f"{vp}: duplicate reproduced platforms")
                artifacts = violation.get("artifacts")
                if not _is_string_list(artifacts, nonempty=True):
                    errors.append(f"{vp}: artifacts must be a non-empty string array")
                else:
                    if len(artifacts) != len(set(artifacts)):
                        errors.append(f"{vp}: duplicate implicated artifacts")
                    for artifact in artifacts:
                        if artifact not in tracked:
                            errors.append(f"{vp}: implicated artifact is not tracked: {artifact}")
                vphase = violation.get("phase")
                if not isinstance(vphase, int) or isinstance(vphase, bool) or vphase < 2 or vphase > 11:
                    errors.append(f"{vp}: phase must be a follow-up phase integer 2..11")
                elif vphase not in phases:
                    errors.append(f"{vp}: phase {vphase} is not listed in follow_up_phases")
                if (
                    entry.get("failure_consequence") in {"Critical", "High"}
                    and vtype in VIOLATION_TYPES
                    and vtype not in EXECUTABLE_VIOLATION_TYPES
                ):
                    errors.append(
                        f"{vp}: a Critical/High known violation must be reproduced by executable "
                        "or live-platform evidence, not manual judgment alone"
                    )
                for proof in proofs:
                    if not isinstance(proof, dict):
                        continue
                    if (
                        proof.get("path") == violation.get("path")
                        and proof.get("reference") == violation.get("reference")
                    ):
                        errors.append(
                            f"{vp}: reproduction repeats proof {violation.get('reference')}; the same "
                            "target cannot both prove the guarantee and reproduce its violation"
                        )
        elif violation is not None:
            errors.append(
                f"{label}: only a known_violation may carry a violation_reproduction"
            )

        semantic = entry.get("semantic_protocol")
        if entry.get("claim_kind") == "semantic":
            if not isinstance(semantic, dict) or set(semantic) != {
                "protocol",
                "repeated_samples",
                "bounded_conclusion",
            }:
                errors.append(f"{label}: semantic claim requires the controlled protocol object")
            elif (
                not isinstance(semantic.get("protocol"), str)
                or not semantic["protocol"].strip()
                or not isinstance(semantic.get("repeated_samples"), int)
                or isinstance(semantic.get("repeated_samples"), bool)
                or semantic["repeated_samples"] < 2
                or not isinstance(semantic.get("bounded_conclusion"), str)
                or not semantic["bounded_conclusion"].strip()
            ):
                errors.append(f"{label}: semantic protocol must name a protocol, >=2 samples, and bounded conclusion")
        elif semantic is not None:
            errors.append(f"{label}: mechanical claim must use null semantic_protocol")
    return errors


def coverage_summary(data: dict[str, Any]) -> str:
    entries = data["guarantees"]
    lines = [f"Guarantees: {len(entries)}"]
    for label, key in (
        ("Failure consequence", "failure_consequence"),
        ("Status", "status"),
    ):
        counts = collections.Counter(entry[key] for entry in entries)
        lines.append(f"{label}: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    proof_counts = collections.Counter(
        proof["type"] for entry in entries for proof in entry["proofs"]
    )
    lines.append("Proof type: " + ", ".join(f"{k}={proof_counts[k]}" for k in sorted(proof_counts)))
    platform_counts = collections.Counter(p for entry in entries for p in entry["applicable_platforms"])
    provider_counts = collections.Counter(p for entry in entries for p in entry["applicable_providers"])
    lines.append("Platform coverage: " + ", ".join(f"{k}={platform_counts[k]}" for k in sorted(platform_counts)))
    lines.append("Provider coverage: " + ", ".join(f"{k}={provider_counts[k]}" for k in sorted(provider_counts)))
    violations = [e for e in entries if e["status"] == "known_violation"]
    lines.append(f"Known violations of a stated guarantee: {len(violations)}")
    for entry in violations:
        v = entry["violation_reproduction"]
        lines.append(
            f"- {entry['id']} [{entry['failure_consequence']}]: {v['path']} :: {v['reference']}"
            f" on {', '.join(v['platforms'])} — {v['observed']}"
        )
        lines.append(f"    invocation: {v['invocation']}")
        lines.append(f"    implicated artifacts: {', '.join(v['artifacts'])}")
        lines.append(f"    correction scheduled: Phase {v['phase']}")
    lines.append("Missing or partial evidence:")
    for entry in entries:
        if entry["status"] in {"partially_evidenced", "unverified", "missing_evidence"}:
            lines.append(
                f"- {entry['id']} [{entry['status']}]: "
                + "; ".join(entry["missing_evidence_or_limitation"])
                + f" (follow-up: {', '.join('Phase '+str(p) for p in entry['follow_up_phases'])})"
            )
    lines.append("Phase 8 live-evidence schedule:")
    for entry in entries:
        for item in entry["required_live_evidence"]:
            lines.append(
                f"- {entry['id']} {item['type']}: {', '.join(item['targets'])} — {item['reason']}"
            )
    gaps = [
        entry for entry in entries
        if entry["failure_consequence"] in {"Critical", "High"}
        and entry["status"] != "verified_by_current_executable_evidence"
    ]
    lines.append(f"Critical/High non-green gaps: {len(gaps)}")
    for entry in gaps:
        lines.append(f"- {entry['id']} [{entry['failure_consequence']}/{entry['status']}]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Portability (task 039): the ledger legitimately contains non-ASCII (em-dashes
    # and, going forward, any Unicode a limitation needs). On Windows the default
    # stdout codec is cp1252, which raises UnicodeEncodeError on a character it
    # cannot map (e.g. U+2192 '->') and fails the whole verify lane — a break a
    # Linux/macOS run never surfaces. Force UTF-8 so `--summary` prints the same on
    # every platform; guarded because a redirected/replaced stream may not be
    # reconfigurable. (The project's hard constraint is Linux/macOS/Windows parity.)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        schema = load_json_strict(args.schema)
        data = load_json_strict(args.ledger)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    schema_errors = validate_schema_contract(schema)
    if schema_errors:
        for error in schema_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Schema/validator contract invalid: {len(schema_errors)} error(s)", file=sys.stderr)
        return 2
    errors = validate_ledger(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Ledger invalid: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print(f"Ledger valid: {len(data['guarantees'])} guarantees")
    if args.summary:
        print(coverage_summary(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
