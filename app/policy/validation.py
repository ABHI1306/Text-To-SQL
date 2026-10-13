"""Save-time checks for row policies (plan section 6, D4-B3)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from app.core.types import Principal
from app.domain.mdl import Mdl
from app.domain.policy import PolicyScope, RowPolicy
from app.semantic.compiler import (
    CompiledQuery,
    CompileError,
    CompileErrorCode,
    compile_query,
)


@dataclass(frozen=True)
class PolicyValidation:
    errors: tuple[CompileError, ...]
    warnings: tuple[str, ...]
    test_compilation: CompiledQuery | None

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_policy(
    policy: RowPolicy,
    mdl: Mdl,
    test_principal: Principal,
    known_attributes: Collection[str],
) -> PolicyValidation:
    """The predicate must parse, use only the model's columns and known attributes, and compile.

    Compilation runs for ``test_principal`` with the policy forced to apply, so a role scope that
    excludes the test user cannot hide a broken predicate.
    """
    model_names = [model.name for model in mdl.models]
    if policy.model not in model_names:
        return _failed(
            CompileError(
                CompileErrorCode.UNKNOWN_MODEL,
                f"model {policy.model!r} is not in the semantic model",
                identifier=policy.model,
                suggestions=tuple(model_names[:5]),
            )
        )
    unknown = [name for name in policy.attributes() if name not in known_attributes]
    if unknown:
        return _failed(
            *(
                CompileError(
                    CompileErrorCode.POLICY_ERROR,
                    f"unknown user attribute {name!r}; set it on at least one account first",
                    identifier=name,
                    suggestions=tuple(sorted(known_attributes)[:5]),
                )
                for name in unknown
            )
        )
    forced = policy.model_copy(update={"applies_to": PolicyScope()})
    compilation = compile_query(f"SELECT * FROM {policy.model}", mdl, test_principal, [forced])
    if not compilation.ok:
        return PolicyValidation(compilation.errors, (), None)
    warnings = tuple(
        f"test user has no value for attribute {name!r}: the policy denies every row for them"
        for name in compilation.missing_attributes
    )
    return PolicyValidation((), warnings, compilation)


def _failed(*errors: CompileError) -> PolicyValidation:
    return PolicyValidation(errors, (), None)
