"""Sandboxed Jinja2 environments: the only way Armature renders a template.

Every template a spec carries (skip_if, condition, partition_source, loop
until, gate messages, tool_call args, role descriptions, adapter cmd, and the
spec file itself at load time) renders through one of these, so attribute
access such as ``''.__class__`` and calls to mutating methods raise
``jinja2.sandbox.SecurityError`` instead of reaching Python internals.

They also bound each render, raising ``TemplateLimitError``:

- A width or ``*`` result past ``MAX_ITEMS``, a ``**`` result past
  ``MAX_ITEMS`` bits, and any single built string past ``MAX_OUTPUT_CHARS``
  are refused before they are built.
- Every operator, filter, and str method that builds a string adds its
  estimated size to a per-render budget of ``MAX_ALLOCATED_CHARS``.
- The rendered output stops at ``MAX_OUTPUT_CHARS``.
- Loop iterations and calls count toward ``MAX_STEPS`` (a macro call counts
  twenty), and a render that loops or calls stops after ``RENDER_DEADLINE_S``
  seconds.

Ranges stop at ``MAX_ITEMS`` items, as in Jinja's own sandbox.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache, wraps
from itertools import chain
from string import Formatter
from typing import Any

from jinja2 import BaseLoader, ChainableUndefined, Undefined, nodes
from jinja2.compiler import CodeGenerator, Frame
from jinja2.environment import Template
from jinja2.nativetypes import NativeCodeGenerator, NativeEnvironment, NativeTemplate, native_concat
from jinja2.runtime import Context, Macro
from jinja2.utils import _PassArg
from jinja2.sandbox import MAX_RANGE, ImmutableSandboxedEnvironment, SecurityError

MAX_ITEMS = MAX_RANGE
MAX_OUTPUT_CHARS = 1 << 20
MAX_ALLOCATED_CHARS = 16 << 20
MAX_STEPS = 100_000
RENDER_DEADLINE_S = float(os.environ.get("ARMATURE_TEMPLATE_DEADLINE_S", "1"))
_MACRO_STEPS = 20
_CLOCK_EVERY = 256
_MAX_WRAPPED_LINES = 20_000

_PERCENT_SPEC = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(\*|\d+)?(?:\.(\*|\d+))?")
_SEQUENCES = (str, list, tuple)
_SIZED_FILTERS = frozenset({"center", "wordwrap", "truncate", "indent", "replace", "string", "tojson", "pprint",
                            "format", "join", "sum"})
_END = object()


class TemplateLimitError(SecurityError):
    """A template would build a value or output past the sandbox's limits, or ran too long."""


@dataclass
class _Budget:
    steps: int = 0
    started: float | None = None
    allocated: int = 0


_BUDGET: ContextVar[_Budget | None] = ContextVar("armature_template_budget", default=None)


@contextmanager
def _render_scope() -> Iterator[None]:
    token = _BUDGET.set(_Budget())
    try:
        yield
    finally:
        _BUDGET.reset(token)


def _budget() -> _Budget:
    return _BUDGET.get() or _Budget()


def _tick(cost: int = 1) -> None:
    budget = _budget()
    if budget.started is None:
        budget.started = time.monotonic()
    before, budget.steps = budget.steps, budget.steps + cost
    if budget.steps > MAX_STEPS:
        raise TemplateLimitError(f"template ran more than {MAX_STEPS} steps of loops and calls")
    if before // _CLOCK_EVERY != budget.steps // _CLOCK_EVERY \
            and time.monotonic() - budget.started > RENDER_DEADLINE_S:
        raise TemplateLimitError(f"template ran longer than {RENDER_DEADLINE_S} s")


def _charge(size: int, what: str) -> None:
    """Refuse a string past MAX_OUTPUT_CHARS before it is built, and count it toward the render's budget."""
    if size > MAX_OUTPUT_CHARS:
        raise TemplateLimitError(f"{what} would build more than {MAX_OUTPUT_CHARS} characters")
    budget = _budget()
    budget.allocated += max(size, 0)
    if budget.allocated > MAX_ALLOCATED_CHARS:
        raise TemplateLimitError(f"template built more than {MAX_ALLOCATED_CHARS} characters in all")


def _str_size(value: Any, limit: int = MAX_OUTPUT_CHARS) -> int:
    """About how long str(value) is, counted without building it, and stopping once past limit."""
    if isinstance(value, str):
        return len(value)
    total, stack = 0, [iter((value,))]
    while stack and total <= limit:
        item = next(stack[-1], _END)
        if item is _END:
            stack.pop()
        elif isinstance(item, str):
            total += len(item) + 2
        elif isinstance(item, dict):
            total += 2 + 4 * len(item)
            stack.append(iter(item.items()))
        elif isinstance(item, (list, tuple, set, frozenset)):
            total += 2 + 2 * len(item)
            stack.append(iter(item))
        else:
            total += 24
    return total


def _capped(parts: Iterable[Any]) -> Iterator[Any]:
    size = 0
    for part in parts:
        if isinstance(part, str):
            size += len(part)
            if size > MAX_OUTPUT_CHARS:
                raise TemplateLimitError(f"template output exceeds {MAX_OUTPUT_CHARS} characters")
        yield part


def _check_width(width: Any, what: str) -> None:
    n = len(width) if isinstance(width, str) else width
    if isinstance(n, int) and n > MAX_ITEMS:
        raise TemplateLimitError(f"{what} width {n} exceeds {MAX_ITEMS}")


def _replaced_size(s: str, old: str, new: str, count: Any) -> int:
    n = s.count(old) if old else len(s) + 1
    if isinstance(count, int) and count >= 0:
        n = min(n, count)
    return len(s) + n * (len(new) - len(old))


def _joined_size(sep: str, items: list) -> int:
    return len(sep) * max(len(items) - 1, 0) + sum(_str_size(i) for i in items)


def _check_percent(fmt: str, values: Any) -> None:
    specs = _PERCENT_SPEC.findall(fmt)
    for width, precision in specs:
        for n in (width, precision):
            if n == "*":
                raise TemplateLimitError("'%' format widths must be literal numbers")
            if n:
                _check_width(int(n), "'%' format")
    if isinstance(values, dict):
        values = list(values.values())
    elif not isinstance(values, tuple):
        values = [values]
    _charge(len(fmt) + len(specs) * max((_str_size(v) for v in values), default=0), "'%' format")


def _checked_format(fmt: str, wrapped: Any) -> Any:
    fields = 0
    for _, field, spec, _ in Formatter().parse(fmt):
        if field is None:
            continue
        fields += 1
        if spec and "{" in spec:
            raise TemplateLimitError("format specs may not nest a replacement field")
        for n in re.findall(r"\d+", spec or ""):
            _check_width(int(n), "format")

    def checked(*args: Any, **kwargs: Any) -> str:
        values = [*args, *kwargs.values(), *(v for a in args if isinstance(a, dict) for v in a.values())]
        _charge(len(fmt) + fields * max((_str_size(v) for v in values), default=0), "str.format")
        return wrapped(*args, **kwargs)
    return checked


def _check_binop(operator: str, left: Any, right: Any) -> None:
    if operator == "*":
        for seq, n in ((left, right), (right, left)):
            if isinstance(seq, _SEQUENCES) and isinstance(n, int):
                if len(seq) * n > MAX_ITEMS:
                    raise TemplateLimitError(f"'*' would build more than {MAX_ITEMS} items or characters")
                _charge(n * _str_size(seq), "'*'")
    elif operator == "**":
        if isinstance(left, int) and isinstance(right, int) and abs(left) > 1 \
                and right * abs(left).bit_length() > MAX_ITEMS:
            raise TemplateLimitError(f"'**' would build a number of more than {MAX_ITEMS} bits")
    elif operator == "+":
        if isinstance(left, _SEQUENCES) and isinstance(right, _SEQUENCES):
            if not isinstance(left, str) and len(left) + len(right) > MAX_ITEMS:
                raise TemplateLimitError(f"'+' would build more than {MAX_ITEMS} items")
            _charge(_str_size(left) + _str_size(right), "'+'")
    elif operator == "%" and isinstance(left, str):
        _check_percent(left, right)


def _check_str_method(method: Any, args: tuple, kwargs: dict) -> tuple:
    """Refuse a str method call that would build too much and charge what it builds; a join's iterable is listed."""
    s, name = method.__self__, method.__name__
    size = len(s)
    if name == "translate":
        raise TemplateLimitError("str.translate is not allowed in a template")
    if name in ("center", "ljust", "rjust", "zfill") and args:
        _check_width(args[0], f"str.{name}")
        size = max(size, args[0]) if isinstance(args[0], int) else size
    elif name == "expandtabs":
        tabsize = args[0] if args else kwargs.get("tabsize", 8)
        if isinstance(tabsize, int):
            size = len(s) + s.count("\t") * tabsize
    elif name == "replace" and len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
        size = _replaced_size(s, args[0], args[1], args[2] if len(args) > 2 else kwargs.get("count", -1))
    elif name == "join" and args:
        items = list(args[0])
        size = _joined_size(s, items)
        args = (items, *args[1:])
    _charge(size, f"str.{name}")
    return args


def _capped_join(items: Iterable[Any]) -> str:
    items = list(items)
    _charge(sum(_str_size(i) for i in items), "'~'")
    return "".join(str(i) for i in items)


def _ticking(iterable: Iterable[Any]) -> Iterator[Any]:
    for item in iterable:
        _tick()
        yield item


def _checked_output(value: Any) -> Any:
    if not isinstance(value, (str, Undefined)):
        _charge(_str_size(value), "output")
    return value


def _sized_filter(fn, size, what: str):
    """Wrap a filter so the size it would build is checked and charged first; size takes the filter's arguments."""
    @wraps(fn)
    def checked(*args, **kwargs):
        _charge(size(*args, **kwargs), what)
        return fn(*args, **kwargs)
    return checked


def _value_charged(fn, name: str):
    """Wrap a filter so a non-str value is charged its str size before the filter runs."""
    position = 0 if _PassArg.from_obj(fn) is None else 1

    @wraps(fn)
    def checked(*args, **kwargs):
        if len(args) > position and not isinstance(args[position], (str, Undefined)):
            _charge(_str_size(args[position]), name)
        return fn(*args, **kwargs)
    return checked


def _width(width: Any, what: str, s: Any) -> int:
    _check_width(width, what)
    return max(width if isinstance(width, int) else 0, len(s) if isinstance(s, str) else _str_size(s))


def _wordwrap_size(env: Any, s: Any, width: Any = 79, *args: Any, **kwargs: Any) -> int:
    size = _width(width, "wordwrap", s)
    if isinstance(width, int) and isinstance(s, str) and len(s) // max(width, 1) > _MAX_WRAPPED_LINES:
        raise TemplateLimitError(f"wordwrap would build more than {_MAX_WRAPPED_LINES} lines")
    return 2 * size


def _indent_size(s: Any, width: Any = 4, *args: Any, **kwargs: Any) -> int:
    _check_width(width, "indent")
    pad = len(width) if isinstance(width, str) else width if isinstance(width, int) else 0
    text = s if isinstance(s, str) else ""
    return len(text) + (text.count("\n") + 1) * pad


def _sum_filter(fn):
    @wraps(fn)
    def checked(environment, iterable, attribute=None, start=0):
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise TemplateLimitError("sum takes only a numeric start")
        return fn(environment, iterable, attribute, start)
    return checked


def _format_filter(fn):
    @wraps(fn)
    def checked(value, *args, **kwargs):
        if isinstance(value, str):
            _check_percent(value, kwargs or args)
        return fn(value, *args, **kwargs)
    return checked


def _join_filter(fn):
    @wraps(fn)
    def checked(eval_ctx, value, d="", attribute=None):
        items = list(value)
        if attribute is None:
            _charge(_joined_size(d, items), "join")
        return fn(eval_ctx, items, d, attribute)
    return checked


class _LimitedCodeGenerator(CodeGenerator):
    """Counts each loop iteration toward the render's budget and joins ``~`` under the size cap."""

    def visit_For(self, node: nodes.For, frame: Frame) -> None:
        node.iter = nodes.Call(nodes.EnvironmentAttribute("ticking"), [node.iter], [], None, None)
        super().visit_For(node, frame)

    def visit_Concat(self, node: nodes.Concat, frame: Frame) -> None:
        self.write("environment.capped_join((")
        for arg in node.nodes:
            self.visit(arg, frame)
            self.write(", ")
        self.write("))")


class _LimitedNativeCodeGenerator(_LimitedCodeGenerator, NativeCodeGenerator):
    pass


class _LimitedTemplate(Template):
    def render(self, *args: Any, **kwargs: Any) -> Any:
        with _render_scope():
            return super().render(*args, **kwargs)


class LimitedSandboxedEnvironment(ImmutableSandboxedEnvironment):
    """The immutable sandbox with limits on how much a template builds and how long it runs."""

    intercepted_binops = frozenset({"*", "**", "+", "%"})
    code_generator_class = _LimitedCodeGenerator
    template_class = _LimitedTemplate
    ticking = staticmethod(_ticking)
    capped_join = staticmethod(_capped_join)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        f = self.filters
        for name in set(f) - _SIZED_FILTERS:
            f[name] = _value_charged(f[name], name)
        f["center"] = _sized_filter(f["center"], lambda s, width=80: _width(width, "center", s), "center")
        f["wordwrap"] = _sized_filter(f["wordwrap"], _wordwrap_size, "wordwrap")
        f["truncate"] = _sized_filter(
            f["truncate"], lambda env, s, length=255, *a, **k: _width(length, "truncate", s), "truncate")
        f["indent"] = _sized_filter(f["indent"], _indent_size, "indent")
        f["replace"] = _sized_filter(
            f["replace"], lambda ctx, s, old, new, count=None: _replaced_size(str(s), str(old), str(new), count),
            "replace")
        f["string"] = _sized_filter(f["string"], _str_size, "string")
        f["tojson"] = _sized_filter(f["tojson"], lambda ctx, value, indent=None: 2 * _str_size(value), "tojson")
        f["pprint"] = _sized_filter(f["pprint"], lambda value: 2 * _str_size(value), "pprint")
        f["format"] = _format_filter(f["format"])
        f["join"] = _join_filter(f["join"])
        f["sum"] = _sum_filter(f["sum"])

    @staticmethod
    def concat(parts: Iterable[str]) -> str:
        return "".join(_capped(parts))

    def compile_expression(self, source: str, undefined_to_none: bool = True) -> Any:
        expression = super().compile_expression(source, undefined_to_none)

        def run(*args: Any, **kwargs: Any) -> Any:
            with _render_scope():
                return expression(*args, **kwargs)
        return run

    def getattr(self, obj: Any, attribute: str) -> Any:
        _tick()
        return super().getattr(obj, attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        _tick()
        return super().getitem(obj, argument)

    def call_filter(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        _tick()
        return super().call_filter(name, value, *args, **kwargs)

    def call_test(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        _tick()
        return super().call_test(name, value, *args, **kwargs)

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        _check_binop(operator, left, right)
        return super().call_binop(context, operator, left, right)

    def call(__self, __context: Context, __obj: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N805
        _tick(_MACRO_STEPS if isinstance(__obj, Macro) else 1)
        if isinstance(getattr(__obj, "__self__", None), str):
            args = _check_str_method(__obj, args, kwargs)
        return super().call(__context, __obj, *args, **kwargs)

    def wrap_str_format(self, value: Any) -> Any:
        wrapped = super().wrap_str_format(value)
        return None if wrapped is None else _checked_format(value.__self__, wrapped)


class SandboxedNativeEnvironment(NativeEnvironment, LimitedSandboxedEnvironment):
    """Returns native Python values like NativeEnvironment, with the limited sandbox's checks."""

    code_generator_class = _LimitedNativeCodeGenerator

    @staticmethod
    def concat(parts: Iterable[Any]) -> Any:
        parts = iter(parts)
        head = [p for _, p in zip(range(2), parts)]
        if len(head) < 2:
            return native_concat(head)
        return native_concat(_capped(_charged_parts(head, parts)))


def _charged_parts(head: list, rest: Iterator[Any]) -> Iterator[Any]:
    for part in chain(head, rest):
        if not isinstance(part, str):
            _charge(_str_size(part), "output")
        yield part


class _SandboxedNativeTemplate(NativeTemplate):
    environment_class = SandboxedNativeEnvironment

    def render(self, *args: Any, **kwargs: Any) -> Any:
        with _render_scope():
            return super().render(*args, **kwargs)


SandboxedNativeEnvironment.template_class = _SandboxedNativeTemplate


@lru_cache(maxsize=None)
def text_env(undefined: type[Undefined] = Undefined) -> LimitedSandboxedEnvironment:
    """A sandboxed environment that renders to a string."""
    return LimitedSandboxedEnvironment(loader=BaseLoader(), undefined=undefined, finalize=_checked_output)


@lru_cache(maxsize=None)
def native_env(undefined: type[Undefined] = ChainableUndefined) -> SandboxedNativeEnvironment:
    """A sandboxed environment that renders a lone expression to its Python value."""
    return SandboxedNativeEnvironment(undefined=undefined)
