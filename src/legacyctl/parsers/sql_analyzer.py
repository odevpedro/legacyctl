"""Deterministic SQL analysis based on SQLGlot.

Language agnostic on purpose: it receives a SQL string plus its provenance and
returns tables, views, columns, operation and access mode.

The governing rule is *never invent dependencies*: when a statement cannot be
parsed safely the result keeps ``parse_status=UNKNOWN`` and reports no table at
all. A partially understood statement is reported as ``PARTIAL`` with the
objects it did prove.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TypedDict

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..domain.enums import DataAccessMode, ParseStatus, SqlOperation

DEFAULT_DIALECT = "postgres"

_CALL_LITERAL = re.compile(
    r"^\s*\{?\s*call\s+(?P<name>[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*|\.[A-Za-z_][\w$]*)"
    r"\s*\((?P<args>[^)]*)\)\s*\}?\s*$",
    re.IGNORECASE,
)
_EXCLUDE_KEYWORDS = {
    "AS",
    "ON",
    "IN",
    "AND",
    "OR",
    "NOT",
    "VALUES",
    "SET",
    "SELECT",
    "FROM",
    "WHERE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "FULL",
    "CROSS",
    "USING",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "DISTINCT",
    "UNION",
    "ALL",
    "BY",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
    "TOP",
    "INTO",
    "EXISTS",
    "BETWEEN",
    "LIKE",
    "IS",
    "NULL",
    "TABLE",
    "VIEW",
    "PROCEDURE",
    "FUNCTION",
    "TRIGGER",
    "SEQUENCE",
    "TEMP",
    "TEMPORARY",
    "ONLY",
    "RETURNING",
    "WITH",
    "INTERVAL",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "COALESCE",
    "NULLIF",
    "GREATEST",
    "LEAST",
}
_CTE_NAMES: set[str] = set()
_SUBQUERY_DEPTH_LIMIT = 12


class _SqlFacts(TypedDict):
    """Keyword arguments for :class:`SqlAnalysis` derived from a parsed tree."""

    normalized: str
    operation: SqlOperation
    tables: list[str]
    views: list[str]
    columns: list[str]
    access_mode: DataAccessMode


@dataclass(slots=True)
class SqlAnalysis:
    """Deterministic outcome of analysing one SQL string."""

    raw: str
    normalized: str | None = None
    operation: SqlOperation = SqlOperation.UNKNOWN
    tables: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    procedure_name: str | None = None
    procedure_args: list[str] = field(default_factory=list)
    db_schema: str | None = None
    access_mode: DataAccessMode = DataAccessMode.UNKNOWN
    parse_status: ParseStatus = ParseStatus.UNKNOWN
    dialect: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.parse_status in (ParseStatus.OK, ParseStatus.PARTIAL)

    @property
    def all_objects(self) -> list[str]:
        return [*self.tables, *self.views]


class SqlAnalyzer:
    """SQLGlot-backed analyzer. Dialect aware, never speculative."""

    def __init__(self, dialect: str = DEFAULT_DIALECT) -> None:
        self.dialect = dialect

    def analyze(self, raw: str) -> SqlAnalysis:
        text = raw.strip().rstrip(";").strip()
        if not text:
            return SqlAnalysis(raw=raw, diagnostics=["empty statement"])

        call = _CALL_LITERAL.match(text)
        if call:
            return self._analyze_call(raw, call, text)

        cleaned = _strip_ado_markers(text)
        try:
            statements = sqlglot.parse(cleaned, read=self.dialect)
        except ParseError as exc:
            return SqlAnalysis(
                raw=raw,
                parse_status=ParseStatus.UNKNOWN,
                diagnostics=[f"sqlglot ParseError: {exc}"],
                dialect=self.dialect,
            )
        except Exception as exc:
            return SqlAnalysis(
                raw=raw,
                parse_status=ParseStatus.UNKNOWN,
                diagnostics=[f"sqlglot failure: {type(exc).__name__}: {exc}"],
                dialect=self.dialect,
            )

        statements = [s for s in statements if s is not None]
        first = statements[0] if statements else None
        if first is None:
            return SqlAnalysis(
                raw=raw, parse_status=ParseStatus.UNKNOWN, diagnostics=["no statement parsed"]
            )
        if len(statements) > 1:
            return SqlAnalysis(
                raw=raw,
                parse_status=ParseStatus.PARTIAL,
                diagnostics=[f"{len(statements)} statements in one literal; first analysed"],
                dialect=self.dialect,
                **self._facts(first),
            )
        return SqlAnalysis(
            raw=raw, parse_status=ParseStatus.OK, dialect=self.dialect, **self._facts(first)
        )

    # -- stored procedure calls -------------------------------------------

    def _analyze_call(self, raw: str, match: re.Match[str], text: str) -> SqlAnalysis:
        name = match.group("name")
        args = [a.strip() for a in match.group("args").split(",") if a.strip()]
        schema, _, simple = name.rpartition(".")
        diagnostics: list[str] = []
        if name.startswith("."):
            # The schema was interpolated from a variable: the *object* is
            # proven, the schema is not. Report UNKNOWN instead of inventing it.
            schema, simple = "", name.lstrip(".")
            diagnostics.append("schema unknown: interpolated expression, not a literal")
        try:
            normalized = sqlglot.parse_one(text, read=self.dialect).sql(dialect=self.dialect)
            status = ParseStatus.PARTIAL if diagnostics else ParseStatus.OK
        except Exception as exc:
            normalized = None
            status = ParseStatus.PARTIAL
            diagnostics.append(f"call not normalised: {type(exc).__name__}")
        return SqlAnalysis(
            raw=raw,
            normalized=normalized,
            operation=SqlOperation.CALL,
            procedure_name=f"{schema}.{simple}" if schema else simple,
            procedure_args=args,
            db_schema=schema or None,
            access_mode=DataAccessMode.UNKNOWN,
            parse_status=status,
            dialect=self.dialect,
            diagnostics=diagnostics,
        )

    # -- DML/DDL facts -----------------------------------------------------

    def _facts(self, statement: exp.Expr) -> _SqlFacts:
        root = statement
        if isinstance(root, exp.Subquery):
            root = root.this
        operation = _operation_of(root)
        cte_names = {cte.alias_or_name.upper() for cte in root.find_all(exp.CTE)}

        tables: list[str] = []
        views: list[str] = []
        columns: list[str] = []
        for scope, name in _iter_qualified_names(root):
            upper = name.upper()
            if upper in cte_names or upper in _EXCLUDE_KEYWORDS:
                continue
            if not _looks_like_identifier(name):
                continue
            if scope == "table":
                tables.append(name)
            elif scope == "column":
                columns.append(name)
        for view in _iter_views(root, cte_names):
            if _looks_like_identifier(view) and view.upper() not in cte_names:
                views.append(view)
        for function_name in _iter_table_functions(root):
            if _looks_like_identifier(function_name):
                views.append(function_name)

        access = _access_mode(operation)
        return {
            "normalized": root.sql(dialect=self.dialect),
            "operation": operation,
            "tables": _dedupe(tables),
            "views": _dedupe(views),
            "columns": _dedupe(columns),
            "access_mode": access,
        }


def _iter_qualified_names(root: exp.Expr) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for table in root.find_all(exp.Table):
        found.append(("table", table.name))
    for column in root.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        found.append(("column", column.name))
    return found


def _iter_views(root: exp.Expr, cte_names: set[str]) -> list[str]:
    views: list[str] = []
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        if isinstance(table.this, exp.Identifier) and table.name in cte_names:
            continue
    for select in root.find_all(exp.Select):
        from_clause = select.args.get("from")
        if from_clause is None:
            continue
        for table in from_clause.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            if _is_view_hint(name, root):
                views.append(name)
    return views


def _iter_table_functions(root: exp.Expr) -> list[str]:
    names: list[str] = []
    for function in root.find_all(exp.Anonymous):
        if function.this.upper() in _EXCLUDE_KEYWORDS:
            continue
        names.append(function.this)
    return names


def _is_view_hint(name: str, root: exp.Expr) -> bool:
    for table in root.find_all(exp.Table):
        if table.name != name:
            continue
        text = str(table.this).upper()
        if "V_" in name.upper() or "VW" in name.upper() or text.startswith("V_"):
            return True
    return False


def _operation_of(root: exp.Expr) -> SqlOperation:
    mapping: list[tuple[type[exp.Expression], SqlOperation]] = [
        (exp.Select, SqlOperation.SELECT),
        (exp.Insert, SqlOperation.INSERT),
        (exp.Update, SqlOperation.UPDATE),
        (exp.Delete, SqlOperation.DELETE),
        (exp.Merge, SqlOperation.MERGE),
        (exp.Create, SqlOperation.CREATE),
        (exp.Alter, SqlOperation.ALTER),
        (exp.Drop, SqlOperation.DROP),
        (exp.Command, SqlOperation.UNKNOWN),
    ]
    for node_type, operation in mapping:
        if isinstance(root, node_type):
            return operation
    if isinstance(root, exp.Expression) and "TRUNCATE" in type(root).__name__.upper():
        return SqlOperation.TRUNCATE
    return SqlOperation.UNKNOWN


def _access_mode(operation: SqlOperation) -> DataAccessMode:
    if operation in (SqlOperation.SELECT,):
        return DataAccessMode.READ
    if operation in (
        SqlOperation.INSERT,
        SqlOperation.UPDATE,
        SqlOperation.DELETE,
        SqlOperation.MERGE,
    ):
        return DataAccessMode.WRITE
    if operation in (
        SqlOperation.CREATE,
        SqlOperation.ALTER,
        SqlOperation.DROP,
        SqlOperation.TRUNCATE,
    ):
        return DataAccessMode.WRITE
    if operation is SqlOperation.CALL:
        return DataAccessMode.UNKNOWN
    return DataAccessMode.UNKNOWN


def _strip_ado_markers(text: str) -> str:
    """Remove ADO/DAO escape markers and `{call ...}` wrappers from literals."""
    cleaned = text.strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def _looks_like_identifier(name: str) -> bool:
    if not name:
        return False
    if len(name) > 128:
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", name))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out
