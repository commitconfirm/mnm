"""Tests for the expression-mode DSL parser (E6).

The parser's correctness AND its security gate (E0 §8 R6) are
both contract-level for v1.0. If a future change to ``filter_dsl.py``
ever lets one of these adversarial cases through, the change is
wrong and must be reverted.

Coverage:
  - All operators round-trip with valid expressions.
  - AND / OR / parens precedence.
  - Quoted strings with embedded SQL-shaped content (parameterized).
  - Duration parsing both quoted and unquoted.
  - Empty / whitespace expressions return identity ``Q()``.
  - Adversarial inputs: SQL injection, attribute access, malformed
    syntax, unknown identifiers, sub-select. All return DslError;
    none reach the database; none raise.

These tests run inside Nautobot's test runner (deferred to G
integration validation), separate from the controller-side
``pytest tests/unit/``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.db.models import Q
from django.test import TestCase

from mnm_plugin.filter_dsl import DslError, parse_dsl


# Allowlist used by every test — superset of fields across all
# E1-E3 filtersets. The point is to exercise the parser, not to
# reflect any one model's exact filterset. Tests against narrower
# allowlists are explicit per case.
_ALLOW = {
    "mac",
    "mac_address",
    "ip",
    "vlan",
    "current_ip",
    "current_switch",
    "current_port",
    "current_vlan",
    "active",
    "interface",
    "vrf",
    "state",
    "node_name",
    "last_seen",
    "collected_at",
    "vendor",
    "hostname",
}


def _q_dict(q: Q) -> dict:
    """Flatten the children of a leaf Q to ``{lhs: rhs}`` for asserting."""
    assert isinstance(q, Q)
    return dict(q.children)


class SimpleComparisonTests(TestCase):
    def test_eq_string_uses_iexact(self):
        result = parse_dsl('mac = "aa:bb:cc"', _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertEqual(_q_dict(result), {"mac__iexact": "aa:bb:cc"})

    def test_eq_number_uses_exact(self):
        result = parse_dsl("vlan = 10", _ALLOW)
        self.assertEqual(_q_dict(result), {"vlan": 10})

    def test_eq_negative_number(self):
        result = parse_dsl("vlan = -5", _ALLOW)
        self.assertEqual(_q_dict(result), {"vlan": -5})

    def test_eq_boolean_true(self):
        result = parse_dsl("active = true", _ALLOW)
        self.assertEqual(_q_dict(result), {"active": True})

    def test_eq_boolean_false(self):
        result = parse_dsl("active = false", _ALLOW)
        self.assertEqual(_q_dict(result), {"active": False})

    def test_ne_string_negates_iexact(self):
        result = parse_dsl('vrf != "default"', _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertTrue(result.negated)

    def test_regex_uses_iregex(self):
        result = parse_dsl('interface ~ "ge-0/0/.*"', _ALLOW)
        self.assertEqual(_q_dict(result), {"interface__iregex": "ge-0/0/.*"})

    def test_contains_uses_icontains(self):
        result = parse_dsl('mac contains "aa:bb"', _ALLOW)
        self.assertEqual(_q_dict(result), {"mac__icontains": "aa:bb"})

    def test_gt_lt_ge_le(self):
        for op, suffix in [(">", "__gt"), ("<", "__lt"),
                           (">=", "__gte"), ("<=", "__lte")]:
            result = parse_dsl(f"vlan {op} 100", _ALLOW)
            self.assertEqual(_q_dict(result), {f"vlan{suffix}": 100})

    def test_in_list(self):
        result = parse_dsl("state in [Established, Up]", _ALLOW)
        self.assertEqual(
            _q_dict(result),
            {"state__in": ["Established", "Up"]},
        )

    def test_in_list_numbers(self):
        result = parse_dsl("vlan in [10, 20, 30]", _ALLOW)
        self.assertEqual(_q_dict(result), {"vlan__in": [10, 20, 30]})

    def test_not_in_list(self):
        result = parse_dsl("vlan not in [10, 20]", _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertTrue(result.negated)


class SentinelTests(TestCase):
    def test_is_sentinel(self):
        result = parse_dsl("interface is sentinel", _ALLOW)
        self.assertEqual(
            _q_dict(result),
            {"interface__regex": r"^ifindex:\d+$"},
        )

    def test_is_not_sentinel(self):
        result = parse_dsl("interface is not sentinel", _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertTrue(result.negated)


class BooleanCompositionTests(TestCase):
    def test_and(self):
        result = parse_dsl("vlan = 10 AND active = true", _ALLOW)
        self.assertIsInstance(result, Q)
        # AND root with two leaf children
        self.assertEqual(result.connector, "AND")
        self.assertEqual(len(result.children), 2)

    def test_or(self):
        result = parse_dsl("vlan = 10 OR vlan = 20", _ALLOW)
        self.assertEqual(result.connector, "OR")

    def test_nested_parens(self):
        result = parse_dsl(
            "(vlan = 10 OR vlan = 20) AND active = true",
            _ALLOW,
        )
        self.assertEqual(result.connector, "AND")

    def test_keyword_case_insensitive(self):
        upper = parse_dsl("vlan = 10 AND active = true", _ALLOW)
        lower = parse_dsl("vlan = 10 and active = true", _ALLOW)
        self.assertEqual(upper.connector, lower.connector)


class DurationTests(TestCase):
    def test_quoted_duration_ago(self):
        result = parse_dsl('last_seen > "7 days ago"', _ALLOW)
        self.assertIsInstance(result, Q)
        kwargs = _q_dict(result)
        self.assertIn("last_seen__gt", kwargs)
        ts = kwargs["last_seen__gt"]
        self.assertIsInstance(ts, datetime)
        # Should be roughly now - 7 days
        delta = datetime.now(timezone.utc) - ts
        self.assertLess(abs(delta.total_seconds() - 7 * 86400), 60)

    def test_unquoted_duration_ago(self):
        result = parse_dsl("collected_at >= 5 days ago", _ALLOW)
        kwargs = _q_dict(result)
        self.assertIn("collected_at__gte", kwargs)

    def test_duration_from_now(self):
        result = parse_dsl('last_seen < "1 hour from now"', _ALLOW)
        kwargs = _q_dict(result)
        ts = kwargs["last_seen__lt"]
        delta = ts - datetime.now(timezone.utc)
        self.assertLess(abs(delta.total_seconds() - 3600), 60)


class EmptyAndWhitespaceTests(TestCase):
    def test_empty_returns_identity(self):
        result = parse_dsl("", _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertEqual(len(result.children), 0)

    def test_whitespace_only_returns_identity(self):
        result = parse_dsl("   \t\n  ", _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertEqual(len(result.children), 0)

    def test_none_returns_identity(self):
        result = parse_dsl(None, _ALLOW)  # type: ignore[arg-type]
        self.assertIsInstance(result, Q)


# --------------------------------------------------------------------
# Adversarial cases (E0 §8 R6 security gate)
# --------------------------------------------------------------------


class AdversarialTests(TestCase):
    """If any of these regress, the security gate is broken."""

    def test_or_one_eq_one_rejects(self):
        """field = "1" OR 1=1 — closing-quote injection attempt.

        Either the LHS field isn't allowlisted (DslError) or the
        RHS expects an IDENT and gets a NUMBER (DslError). Both
        outcomes are correct: nothing reaches the database.
        """
        result = parse_dsl('field = "1" OR 1=1', _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_or_one_eq_one_with_allowlisted_lhs_rejects(self):
        """vlan = 1 OR 1=1 — LHS is allowed, RHS is bare ``1=1``.

        After the OR keyword, the parser expects another comparison
        starting with IDENT. ``1`` is a NUMBER token. DslError.
        """
        result = parse_dsl("vlan = 1 OR 1=1", _ALLOW)
        self.assertIsInstance(result, DslError)
        self.assertIn("IDENT", result.message)

    def test_sql_injection_in_string_value_is_parameterized(self):
        """mac = "x'; DROP TABLE foo; --" — escape-injection.

        Parameterized binding via Django ORM makes the embedded SQL
        harmless; the parser treats the string as a literal value.
        """
        result = parse_dsl(
            'mac = "x\'; DROP TABLE foo; --"',
            _ALLOW,
        )
        self.assertIsInstance(result, Q)
        self.assertEqual(
            _q_dict(result),
            {"mac__iexact": "x'; DROP TABLE foo; --"},
        )

    def test_sub_select_rejected(self):
        """vlan = (SELECT 1) — sub-select; ``(SELECT...)`` not a value."""
        result = parse_dsl("vlan = (SELECT 1)", _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_python_attribute_access_rejected(self):
        """__class__ = "X" — Python-attribute-access attempt.

        ``__class__`` matches the IDENT regex but isn't in any
        model's allowlist. Rejected.
        """
        result = parse_dsl('__class__ = "X"', _ALLOW)
        self.assertIsInstance(result, DslError)
        self.assertIn("__class__", result.message)

    def test_python_dunder_dict_rejected(self):
        result = parse_dsl('__dict__ = "X"', _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_unknown_field_rejected(self):
        result = parse_dsl('secret_field = "x"', _ALLOW)
        self.assertIsInstance(result, DslError)
        self.assertIn("secret_field", result.message)

    def test_lookup_suffix_in_field_rejected(self):
        """Operators MUST NOT bypass the allowlist via ``__gte`` etc.

        ``last_seen__gte`` is a single IDENT token; not in the
        allowlist (which has ``last_seen``).
        """
        result = parse_dsl('last_seen__gte = "x"', _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_nested_boolean_bypass(self):
        """field = "x" OR (1=1 AND 2=2) — the inner parens has bare numbers.

        The first half parses cleanly; the inner parens fails at the
        IDENT check. DslError.
        """
        result = parse_dsl('vlan = 10 OR (1=1 AND 2=2)', _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_malformed_incomplete_comparison(self):
        result = parse_dsl("mac =", _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_malformed_keyword_only(self):
        result = parse_dsl("AND OR", _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_malformed_dangling_and(self):
        result = parse_dsl("vlan = 10 AND", _ALLOW)
        self.assertIsInstance(result, DslError)

    def test_unknown_operator_rejected(self):
        # Backslash isn't a tokenizable character.
        result = parse_dsl("vlan ?? 10", _ALLOW)
        self.assertIsInstance(result, DslError)


# --------------------------------------------------------------------
# String escape behaviour
# --------------------------------------------------------------------


class StringEscapeTests(TestCase):
    def test_escaped_quote_in_double(self):
        result = parse_dsl(r'mac = "aa\"bb"', _ALLOW)
        self.assertIsInstance(result, Q)
        self.assertEqual(_q_dict(result), {"mac__iexact": 'aa"bb'})

    def test_single_quote_string(self):
        result = parse_dsl("mac = 'aa:bb'", _ALLOW)
        self.assertEqual(_q_dict(result), {"mac__iexact": "aa:bb"})

    def test_newline_escape(self):
        result = parse_dsl(r'mac = "x\ny"', _ALLOW)
        self.assertEqual(_q_dict(result), {"mac__iexact": "x\ny"})


# --------------------------------------------------------------------
# Allowlist enforcement when value is a bare identifier
# --------------------------------------------------------------------


class BareIdentValueTests(TestCase):
    def test_bare_ident_value_treated_as_string(self):
        """``vendor = juniper`` — bare ``juniper`` is a literal string."""
        result = parse_dsl("vendor = juniper", _ALLOW)
        self.assertEqual(
            _q_dict(result),
            {"vendor__iexact": "juniper"},
        )

    def test_in_list_with_bare_idents(self):
        result = parse_dsl(
            "state in [Established, Idle, Active]",
            _ALLOW,
        )
        self.assertEqual(
            _q_dict(result),
            {"state__in": ["Established", "Idle", "Active"]},
        )
