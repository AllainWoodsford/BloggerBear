"""The inline policies a root attaches to its shared lambda_exec role, rendered as IAM measures them.

IAM refuses a PutRolePolicy that would take one role's inline policies past 10,240 characters in
all (whitespace is not counted). Every pipeline Lambda runs as lambda_exec, so every grant one of
them needs lands on it: as a statement in a root's own documents, or as a policy a module attaches
through a role input. Neither `terraform validate` nor a plan adds the documents up; the first sign
is an apply that stops at the policy, and in production that apply is a release.
test_terraform_wiring.py holds the total under a budget; this renders the documents from the
Terraform text so it can.

It is not Terraform. It reads the HCL the policies are written in (string templates, lists,
objects, for expressions, conditionals, a few functions, and references to variables, locals,
module outputs, data sources and resources) and puts a realistic value where only an apply knows
one: the default name prefix (terraform_text.py), a 12-digit account, the home region, and a
generated id the length AWS makes it. A name is the one the Terraform gives the resource, so the
environment's name and the app-data table count come out as they deploy. Anything it cannot value
raises `Unresolved`, naming the expression, rather than guessing a length.

How each document renders follows the AWS provider's aws_iam_policy_document: a list of one is a
bare string, actions and resources are sets, Effect defaults to Allow, and Version is 2012-10-17.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from terraform_text import DEFAULT_PREFIX, INFRA

ACCOUNT = "123456789012"
HOME_REGION = "ap-southeast-2"
# A root's variables that have to be these, whatever the variable's own default says.
ROOT_VALUES = {"unique_name_prefix": DEFAULT_PREFIX, "aws_region": HOME_REGION}

# A resource's ARN, from its name. A type the policies name that is missing here raises Unresolved.
_ARNS = {
    "aws_cloudwatch_log_group": "arn:aws:logs:{region}:{account}:log-group:{name}",
    "aws_dynamodb_table": "arn:aws:dynamodb:{region}:{account}:table/{name}",
    "aws_iam_role": "arn:aws:iam::{account}:role/{name}",
    "aws_lambda_function": "arn:aws:lambda:{region}:{account}:function:{name}",
    "aws_s3_bucket": "arn:aws:s3:::{name}",
    "aws_sfn_state_machine": "arn:aws:states:{region}:{account}:stateMachine:{name}",
    "aws_sns_topic": "arn:aws:sns:{region}:{account}:{name}",
    "aws_sqs_queue": "arn:aws:sqs:{region}:{account}:{name}",
}
# The argument that holds a resource's name, where it is not `name`.
_NAME_ARGUMENTS = {"aws_s3_bucket": "bucket", "aws_lambda_function": "function_name"}
# What a resource's `id` is, where it is not its name.
_IDS = {"aws_sns_topic": "arn", "aws_sfn_state_machine": "arn", "aws_sqs_queue": "url"}
# Attributes AWS makes at apply, as long as AWS makes them.
_GENERATED = {
    ("aws_bedrockagentcore_gateway", "gateway_arn"): (
        "arn:aws:bedrock-agentcore:{region}:{account}:gateway/{name}-abcdefghij"
    ),
    ("aws_cloudfront_distribution", "id"): "E1ABCDEFGHIJKL",
    ("aws_cloudfront_distribution", "arn"): "arn:aws:cloudfront::{account}:distribution/E1ABCDEFGHIJKL",
    ("aws_sqs_queue", "url"): "https://sqs.{region}.amazonaws.com/{account}/{name}",
}


class Unresolved(Exception):
    """An expression this cannot value. Teach it (a table above, or a function in _FUNCTIONS) when
    a new policy needs it, rather than leaving the policy out of the count."""


def size(document: dict) -> int:
    """The document's length as IAM counts it: minified JSON, whitespace not counted."""
    return len(re.sub(r"\s", "", json.dumps(document, separators=(",", ":"), ensure_ascii=False)))


def lambda_exec_policies(env: str, infra: Path = INFRA) -> dict[str, dict]:
    """Every inline policy on the root's lambda_exec role, by address, rendered: the root's own, and
    any module's, at any depth.

    A policy is lambda_exec's when its `role` values to the role's name, however that is spelled:
    `.id` or `.name`, a local, a module input, a template, another policy's `role`. A `role` this
    cannot value raises Unresolved rather than being left out of the count, so a function it has
    not learned (`split()`) or an input given the whole role object fails loudly. The one `role`
    not valued is a reference to an aws_iam_role the same root or module declares, other than the
    root's lambda_exec (`aws_iam_role.ops_mcp.id`): it cannot be lambda_exec, and its name may be
    one this cannot make.

    A policy behind a count or for_each counts once, as if its switch (`count = var.x ? 1 : 0`)
    were on. One that comes to more than one instance with the variables' defaults raises
    Unresolved: each instance is a policy of its own."""
    root = _Scope(infra / "environments" / env)
    found: dict[str, dict] = {}
    _collect(root, "", root.value(_tokens("aws_iam_role.lambda_exec.name")), found)
    return found


def statements(env: str, address: str = "aws_iam_role_policy.lambda_exec", infra: Path = INFRA) -> dict:
    """One of those policies' statements, by Sid."""
    document = lambda_exec_policies(env, infra)[address]
    return {statement.get("Sid", ""): statement for statement in document["Statement"]}


def evaluate(env: str, expression: str, module: str | None = None, infra: Path = INFRA):
    """An expression's value in a root, or in one of the modules it calls."""
    scope = _Scope(infra / "environments" / env)
    if module:
        scope = scope.module(module)
    return scope.value(_tokens(expression))


def _collect(scope: _Scope, prefix: str, role_name: str, found: dict[str, dict]) -> None:
    for (kind, name), block in scope.resources.items():
        if kind != "aws_iam_role_policy" or scope.names_another_role(block.attributes["role"]):
            continue
        if scope.value(block.attributes["role"]) != role_name:
            continue
        address = f"{prefix}aws_iam_role_policy.{name}"
        if (instances := scope.instances(block)) > 1:
            raise Unresolved(f"{address}, which comes to {instances} policies: count each one")
        found[address] = json.loads(scope.value(block.attributes["policy"]))
    for name in scope.modules:
        child = scope.module(name)
        if child is not None:
            _collect(child, f"{prefix}module.{name}.", role_name, found)


# --- reading HCL ------------------------------------------------------------------------------------

_PUNCTUATION = ("...", "=>", "==", "!=", ">=", "<=", "&&", "||", *"{}[](),.:?=!><+-*/%")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\n")


@dataclass(frozen=True)
class _Token:
    kind: str  # identifier, number, string or heredoc (a template's source), punctuation or newline
    text: str


@dataclass
class _Block:
    type: str
    labels: tuple[str, ...]
    attributes: dict[str, list[_Token]] = field(default_factory=dict)  # unparsed until needed
    blocks: list[_Block] = field(default_factory=list)


def _string_end(text: str, i: int) -> int:
    """Where the quoted string whose contents start at i closes."""
    while True:
        if text.startswith(("$${", "%%{"), i):
            i += 3
        elif text.startswith(("${", "%{"), i):
            i = _interpolation_end(text, i + 2) + 1
        elif text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i
        elif text[i] == "\n":
            raise ValueError(f"a string runs past the end of its line: {text[i - 40 : i]!r}")
        else:
            i += 1


def _interpolation_end(text: str, i: int) -> int:
    depth = 0
    while True:
        if text[i] == '"':
            i = _string_end(text, i + 1)
        elif text[i] == "{":
            depth += 1
        elif text[i] == "}":
            if not depth:
                return i
            depth -= 1
        i += 1


def _tokens(text: str) -> list[_Token]:
    tokens, i = [], 0
    while i < len(text):
        char = text[i]
        if char == "\n":
            tokens.append(_Token("newline", char))
            i += 1
        elif char in " \t\r":
            i += 1
        elif char == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        elif char == '"':
            end = _string_end(text, i + 1)
            tokens.append(_Token("string", text[i + 1 : end]))
            i = end + 1
        elif heredoc := _HEREDOC.match(text, i):
            closing = re.compile(rf"^[ \t]*{heredoc.group(1)}[ \t]*$", re.M).search(text, heredoc.end())
            tokens.append(_Token("heredoc", text[heredoc.end() : closing.start()]))
            i = closing.end()
        elif number := _NUMBER.match(text, i):
            tokens.append(_Token("number", number.group()))
            i = number.end()
        elif identifier := _IDENTIFIER.match(text, i):
            tokens.append(_Token("identifier", identifier.group()))
            i = identifier.end()
        else:
            punctuation = next((p for p in _PUNCTUATION if text.startswith(p, i)), None)
            if punctuation is None:
                raise ValueError(f"unexpected {char!r} in {text[max(0, i - 40) : i + 40]!r}")
            tokens.append(_Token("punctuation", punctuation))
            i += len(punctuation)
    return tokens


def _is(token: _Token | None, *texts: str) -> bool:
    return token is not None and token.kind == "punctuation" and token.text in texts


def _body(tokens: list[_Token], i: int, nested: bool) -> tuple[_Block, int]:
    """A body's attributes and blocks, from i to its closing brace (or the end of a file)."""
    body = _Block("body", ())
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "newline":
            i += 1
            continue
        if _is(token, "}") and nested:
            return body, i + 1
        if token.kind != "identifier":
            raise ValueError(f"expected an attribute or a block, found {token.text!r}")
        if _is(tokens[i + 1], "="):
            start = i = i + 2
            depth = 0
            # An attribute's expression runs to the end of its line, or to the brace that closes
            # its block, unless a bracket it opened is still open.
            while i < len(tokens):
                if depth == 0 and (tokens[i].kind == "newline" or _is(tokens[i], "}")):
                    break
                depth += _is(tokens[i], "(", "[", "{") - _is(tokens[i], ")", "]", "}")
                i += 1
            body.attributes[token.text] = tokens[start:i]
            continue
        labels, i = [], i + 1
        while tokens[i].kind in ("string", "identifier"):
            labels.append(tokens[i].text)
            i += 1
        if not _is(tokens[i], "{"):
            raise ValueError(f"expected {{ after {token.text} {labels}")
        block, i = _body(tokens, i + 1, nested=True)
        block.type, block.labels = token.text, tuple(labels)
        body.blocks.append(block)
    if nested:
        raise ValueError("a block is never closed")
    return body, i


# --- expressions ----------------------------------------------------------------------------------
# An expression is a tuple: ("literal", value), ("template", parts), ("reference", name, steps),
# ("call", name, args), ("list", items), ("object", pairs), ("for", ...), ("conditional", c, a, b),
# ("operator", op, left, right), ("not", e), ("negative", e) or ("step", e, steps). A step is
# ("attribute", name), ("index", expression) or ("splat",).

_BINARY = (("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%"))


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = [token for token in tokens if token.kind != "newline"]
        self.i = 0

    def peek(self, ahead: int = 0) -> _Token | None:
        return self.tokens[self.i + ahead] if self.i + ahead < len(self.tokens) else None

    def take(self, *texts: str) -> _Token:
        token = self.peek()
        if token is None or (texts and not _is(token, *texts)):
            raise ValueError(f"expected {texts or 'more'} at {token}")
        self.i += 1
        return token

    def whole(self):
        expression = self.expression()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek().text!r} after an expression")
        return expression

    def expression(self):
        condition = self.binary(0)
        if _is(self.peek(), "?"):
            self.take("?")
            yes = self.expression()
            self.take(":")
            return ("conditional", condition, yes, self.expression())
        return condition

    def binary(self, level: int):
        if level == len(_BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while _is(self.peek(), *_BINARY[level]):
            operator = self.take().text
            left = ("operator", operator, left, self.binary(level + 1))
        return left

    def unary(self):
        if _is(self.peek(), "!"):
            self.take()
            return ("not", self.unary())
        if _is(self.peek(), "-"):
            self.take()
            return ("negative", self.unary())
        return self.postfix(self.primary())

    def postfix(self, expression):
        steps = []
        while True:
            if _is(self.peek(), ".") and _is(self.peek(1), "*"):
                self.i += 2
                steps.append(("splat",))
            elif _is(self.peek(), "."):
                self.take()
                steps.append(("attribute", self.take().text))
            elif _is(self.peek(), "[") and _is(self.peek(1), "*"):
                self.i += 2
                self.take("]")
                steps.append(("splat",))
            elif _is(self.peek(), "["):
                self.take()
                steps.append(("index", self.expression()))
                self.take("]")
            else:
                break
        if not steps:
            return expression
        if expression[0] == "reference":
            return ("reference", expression[1], expression[2] + steps)
        return ("step", expression, steps)

    def primary(self):
        token = self.take()
        if token.kind == "number":
            return ("literal", float(token.text) if "." in token.text else int(token.text))
        if token.kind in ("string", "heredoc"):
            return ("template", _template(token.text, escapes=token.kind == "string"))
        if token.kind == "identifier":
            if token.text in ("true", "false", "null"):
                return ("literal", {"true": True, "false": False, "null": None}[token.text])
            if _is(self.peek(), "("):
                self.take()
                arguments = []
                while not _is(self.peek(), ")"):
                    arguments.append(self.expression())
                    if _is(self.peek(), "..."):
                        raise ValueError("argument expansion (...) is not read here")
                    if not _is(self.peek(), ")"):
                        self.take(",")
                self.take(")")
                return ("call", token.text, arguments)
            return ("reference", token.text, [])
        if _is(token, "("):
            inner = self.expression()
            self.take(")")
            return inner
        if _is(token, "["):
            if self.peek() is not None and self.peek().text == "for":
                return self.for_expression("]")
            items = []
            while not _is(self.peek(), "]"):
                items.append(self.expression())
                if not _is(self.peek(), "]"):
                    self.take(",")
            self.take("]")
            return ("list", items)
        if _is(token, "{"):
            if self.peek() is not None and self.peek().text == "for":
                return self.for_expression("}")
            pairs = []
            while not _is(self.peek(), "}"):
                key = self.take()
                key = ("literal", key.text) if key.kind == "identifier" else ("template", _template(key.text))
                self.take("=", ":")
                pairs.append((key, self.expression()))
                if _is(self.peek(), ","):
                    self.take()
            self.take("}")
            return ("object", pairs)
        raise ValueError(f"unexpected {token.text!r}")

    def for_expression(self, closing: str):
        self.take()  # for
        names = [self.take().text]
        if _is(self.peek(), ","):
            self.take()
            names.append(self.take().text)
        if self.take().text != "in":
            raise ValueError("expected `in` in a for expression")
        collection = self.expression()
        self.take(":")
        key = None
        value = self.expression()
        if closing == "}":
            self.take("=>")
            key, value = value, self.expression()
            if _is(self.peek(), "..."):
                raise ValueError("grouping (...) is not read here")
        condition = None
        if self.peek() is not None and self.peek().text == "if":
            self.take()
            condition = self.expression()
        self.take(closing)
        return ("for", names, collection, key, value, condition)


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _template(source: str, escapes: bool = True) -> list:
    """A string's parts: literal text, and the expressions of its ${...} interpolations. A heredoc
    has no backslash escapes."""
    parts, text, i = [], [], 0
    while i < len(source):
        if source.startswith(("$${", "%%{"), i):
            text.append(source[i + 1 : i + 3])
            i += 3
        elif source.startswith("%{", i):
            raise ValueError(f"template directives are not read here: {source!r}")
        elif source.startswith("${", i):
            end = _interpolation_end(source, i + 2)
            parts.append("".join(text))
            text = []
            parts.append(_Parser(_tokens(source[i + 2 : end].strip("~ "))).whole())
            i = end + 1
        elif source[i] == "\\" and escapes:
            text.append(_ESCAPES.get(source[i + 1], source[i + 1]))
            i += 2
        else:
            text.append(source[i])
            i += 1
    parts.append("".join(text))
    return parts


def _replace(text: str, search: str, replacement: str) -> str:
    if len(search) > 1 and search.startswith("/") and search.endswith("/"):
        raise Unresolved(f"replace() with a regular expression: {search}")
    return text.replace(search, replacement)


def _flatten(items) -> list:
    return [leaf for item in items for leaf in (_flatten(item) if isinstance(item, list) else [item])]


_FUNCTIONS = {
    "coalesce": lambda *values: next(value for value in values if value not in (None, "")),
    "compact": lambda items: [item for item in items if item],
    "concat": lambda *lists: [item for items in lists for item in items],
    "distinct": lambda items: list(dict.fromkeys(items)),
    "flatten": _flatten,
    "join": lambda separator, items: separator.join(items),
    "jsonencode": lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True),
    "length": len,
    "lower": str.lower,
    "replace": _replace,
    "tolist": list,
    "toset": lambda items: sorted(set(items)),
    "trimprefix": lambda text, prefix: text.removeprefix(prefix),
    "trimsuffix": lambda text, suffix: text.removesuffix(suffix),
    "upper": str.upper,
}

_OPERATORS = {
    "||": lambda a, b: a or b,
    "&&": lambda a, b: a and b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "%": lambda a, b: a % b,
}


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | dict) or value is None:
        raise Unresolved(f"a {type(value).__name__} interpolated into a string")
    return str(value)


def _apply(value, steps: list, scope: _Scope, names: dict):
    for step in steps:
        if step[0] == "splat":
            raise Unresolved("a splat ([*]) on a value")
        key = step[1] if step[0] == "attribute" else scope.evaluate(step[1], names)
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError) as error:
            raise Unresolved(f"{key!r} of {value!r}") from error
    return value


# --- evaluating -----------------------------------------------------------------------------------


class _Scope:
    """A root, or one module call: its files, and the arguments it was called with."""

    def __init__(self, directory: Path, arguments: dict | None = None, caller: _Scope | None = None):
        self.directory = directory
        self.arguments = arguments or {}  # input -> its tokens, valued in the caller's scope
        self.caller = caller
        self.blocks: list[_Block] = []
        for path in sorted(directory.glob("*.tf")):
            self.blocks.extend(_body(_tokens(path.read_text(encoding="utf-8")), 0, nested=False)[0].blocks)
        self.region = caller.region if caller else HOME_REGION
        self._values: dict[tuple, object] = {}
        self._modules: dict[str, _Scope | None] = {}

    def _labelled(self, kind: str) -> dict[tuple[str, ...], _Block]:
        return {block.labels: block for block in self.blocks if block.type == kind}

    @cached_property
    def resources(self) -> dict[tuple[str, ...], _Block]:
        return self._labelled("resource")

    @cached_property
    def data(self) -> dict[tuple[str, ...], _Block]:
        return self._labelled("data")

    @cached_property
    def modules(self) -> dict[str, _Block]:
        return {labels[0]: block for labels, block in self._labelled("module").items()}

    @cached_property
    def locals(self) -> dict[str, list[_Token]]:
        return {
            name: tokens
            for block in self.blocks
            if block.type == "locals"
            for name, tokens in block.attributes.items()
        }

    def module(self, name: str) -> _Scope | None:
        """The scope of a module this one calls, or None for one not in this repository."""
        if name not in self._modules:
            block = self.modules[name]
            source = self.evaluate(_Parser(block.attributes["source"]).whole(), {})
            self._modules[name] = (
                _Scope((self.directory / source).resolve(), block.attributes, self)
                if source.startswith(".")
                else None
            )
        return self._modules[name]

    def names_another_role(self, tokens: list[_Token]) -> bool:
        """Whether a `role` is `aws_iam_role.<name>.id` or `.name` (`[0]` for one of a count) for a
        role this scope declares that is not the root's lambda_exec. A module's own roles are never
        the root's, whatever they are called."""
        expression = _Parser(tokens).whole()
        if expression[0] != "reference" or expression[1] != "aws_iam_role":
            return False
        steps = [step for i, step in enumerate(expression[2]) if not (i == 1 and step[0] == "index")]
        if len(steps) != 2 or any(step[0] != "attribute" for step in steps):
            return False
        role, attribute = steps[0][1], steps[1][1]
        if attribute not in ("id", "name"):
            return False
        return ("aws_iam_role", role) in self.resources and (self.caller is not None or role != "lambda_exec")

    def instances(self, block: _Block) -> int:
        """How many instances a resource comes to with the variables' defaults: its count, or the
        length of its for_each, or one."""
        if "count" in block.attributes:
            return self.value(block.attributes["count"])
        if "for_each" in block.attributes:
            return len(self.value(block.attributes["for_each"]))
        return 1

    def value(self, tokens: list[_Token], names: dict | None = None):
        return self.evaluate(_Parser(tokens).whole(), names or {})

    def evaluate(self, expression, names: dict):
        kind = expression[0]
        if kind == "literal":
            return expression[1]
        if kind == "template":
            parts = [part if isinstance(part, str) else self.evaluate(part, names) for part in expression[1]]
            if len(parts) == 3 and parts[0] == parts[2] == "":
                return parts[1]  # "${x}" is x itself, whatever its type
            return "".join(_text(part) for part in parts)
        if kind == "reference":
            return self.reference(expression[1], expression[2], names)
        if kind == "step":
            return _apply(self.evaluate(expression[1], names), expression[2], self, names)
        if kind == "call":
            if expression[1] not in _FUNCTIONS:
                raise Unresolved(f"the function {expression[1]}()")
            return _FUNCTIONS[expression[1]](*(self.evaluate(a, names) for a in expression[2]))
        if kind == "list":
            return [self.evaluate(item, names) for item in expression[1]]
        if kind == "object":
            return {_text(self.evaluate(k, names)): self.evaluate(v, names) for k, v in expression[1]}
        if kind == "for":
            return self.for_expression(*expression[1:], names=names)
        if kind == "conditional":
            branch = expression[2] if self.evaluate(expression[1], names) else expression[3]
            return self.evaluate(branch, names)
        if kind == "operator":
            left, right = self.evaluate(expression[2], names), self.evaluate(expression[3], names)
            return _OPERATORS[expression[1]](left, right)
        if kind == "not":
            return not self.evaluate(expression[1], names)
        if kind == "negative":
            return -self.evaluate(expression[1], names)
        raise Unresolved(kind)

    def for_expression(self, variables, collection, key, value, condition, names):
        items = self.evaluate(collection, names)
        pairs = items.items() if isinstance(items, dict) else enumerate(items)
        results = []
        for index, item in pairs:
            inner = {**names, **dict(zip(variables[::-1], (item, index)))}
            if condition is None or self.evaluate(condition, inner):
                results.append((key and self.evaluate(key, inner), self.evaluate(value, inner)))
        return dict(results) if key else [result for _, result in results]

    def reference(self, root: str, steps: list, names: dict):
        if root in names:
            return _apply(names[root], steps, self, names)
        if not steps or steps[0][0] != "attribute":
            raise Unresolved(f"{root} on its own")
        name, rest = steps[0][1], steps[1:]
        if root == "var":
            return _apply(self.variable(name), rest, self, names)
        if root == "local":
            if name not in self.locals:
                raise Unresolved(f"local.{name} in {self.directory}")
            local = self.cached(("local", name), lambda: self.value(self.locals[name]))
            return _apply(local, rest, self, names)
        rest = rest[1:] if rest and rest[0][0] == "index" else rest  # one instance of a count
        if root == "module":
            if not rest or rest[0][0] != "attribute":
                raise Unresolved(f"module.{name} on its own")
            return _apply(self.output(name, rest[0][1]), rest[1:], self, names)
        if root == "data":
            if not rest or rest[0][0] != "attribute":
                raise Unresolved(f"data.{name} on its own")
            data_name = rest[0][1]
            rest = rest[1:]
            rest = rest[1:] if rest and rest[0][0] == "index" else rest
            if not rest or rest[0][0] != "attribute":
                raise Unresolved(f"data.{name}.{data_name} on its own")
            return _apply(self.data_attribute(name, data_name, rest[0][1]), rest[1:], self, names)
        if root in ("count", "each", "path", "self", "terraform"):
            raise Unresolved(f"{root}.{name}")
        if not rest or rest[0][0] != "attribute":
            raise Unresolved(f"{root}.{name} on its own")
        return _apply(self.attribute(root, name, rest[0][1]), rest[1:], self, names)

    def cached(self, key: tuple, compute):
        if key not in self._values:
            self._values[key] = compute()
        return self._values[key]

    def variable(self, name: str):
        if name in self.arguments:
            return self.cached(("argument", name), lambda: self.caller.value(self.arguments[name]))
        if self.caller is None and name in ROOT_VALUES:
            return ROOT_VALUES[name]
        block = self._labelled("variable").get((name,))
        if block is None or "default" not in block.attributes:
            raise Unresolved(f"var.{name} (no default) in {self.directory}")
        return self.value(block.attributes["default"])

    def output(self, module: str, output: str):
        scope = self.module(module)
        if scope is None:
            raise Unresolved(f"module.{module}, which is not in this repository")
        block = scope._labelled("output").get((output,))
        if block is None:
            raise Unresolved(f"module.{module}.{output}, which the module does not output")
        return scope.cached(("output", output), lambda: scope.value(block.attributes["value"]))

    def data_attribute(self, kind: str, name: str, attribute: str):
        if kind == "aws_caller_identity" and attribute == "account_id":
            return ACCOUNT
        if kind == "aws_region" and attribute in ("name", "region", "id"):
            return self.region
        if kind == "aws_partition" and attribute == "partition":
            return "aws"
        if kind == "aws_iam_policy_document" and attribute == "json":
            return json.dumps(self.cached(("document", name), lambda: self.document(name)))
        raise Unresolved(f"data.{kind}.{name}.{attribute}")

    def attribute(self, kind: str, name: str, attribute: str):
        block = self.resources.get((kind, name))
        if block is None:
            raise Unresolved(f"{kind}.{name}, which {self.directory} does not declare")
        if attribute in block.attributes:
            return self.value(block.attributes[attribute])
        region = self.value(block.attributes["region"]) if "region" in block.attributes else self.region
        argument = _NAME_ARGUMENTS.get(kind, "name")
        resource_name = self.value(block.attributes[argument]) if argument in block.attributes else None
        if attribute == "id" and (kind, "id") not in _GENERATED:
            attribute = _IDS.get(kind, argument)
        template = _GENERATED.get((kind, attribute)) or (_ARNS.get(kind) if attribute == "arn" else None)
        if attribute == argument and resource_name is not None:
            return resource_name
        if template and (resource_name is not None or "{name}" not in template):
            return template.format(region=region, account=ACCOUNT, name=resource_name)
        raise Unresolved(f"{kind}.{name}.{attribute}")

    def expanded(self, blocks: list[_Block], kind: str, names: dict):
        """The blocks of one kind, a dynamic block's once for each of its for_each."""
        for block in blocks:
            if block.type == kind:
                yield block, names
            elif block.type == "dynamic" and block.labels == (kind,):
                iterator = block.attributes["iterator"][0].text if "iterator" in block.attributes else kind
                items = self.value(block.attributes["for_each"], names)
                content = next(inner for inner in block.blocks if inner.type == "content")
                pairs = items.items() if isinstance(items, dict) else enumerate(items)
                for key, item in pairs:
                    yield content, {**names, iterator: {"key": key, "value": item}}

    def document(self, name: str) -> dict:
        """An aws_iam_policy_document, as the AWS provider renders it."""
        block = self.data.get(("aws_iam_policy_document", name))
        if block is None:
            raise Unresolved(f"data.aws_iam_policy_document.{name} in {self.directory}")
        merged = {"source_policy_documents", "override_policy_documents", "source_json", "override_json"}
        if merged & block.attributes.keys():
            raise Unresolved(f"data.aws_iam_policy_document.{name} merges other documents")
        document = {"Version": "2012-10-17"}
        if "version" in block.attributes:
            document["Version"] = self.value(block.attributes["version"])
        if "policy_id" in block.attributes:
            document["Id"] = self.value(block.attributes["policy_id"])
        document["Statement"] = [
            self.statement(statement, names)
            for statement, names in self.expanded(block.blocks, "statement", {})
        ]
        return document

    def statement(self, block: _Block, names: dict) -> dict:
        def strings(key: str) -> list[str]:
            values = self.value(block.attributes[key], names) if key in block.attributes else []
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise Unresolved(f"{key} that is not a list of strings: {values!r}")
            return sorted(set(values))

        def one_or_list(values: list[str]):
            return values[0] if len(values) == 1 else values

        rendered = {}
        if "sid" in block.attributes and self.value(block.attributes["sid"], names):
            rendered["Sid"] = self.value(block.attributes["sid"], names)
        effect = block.attributes.get("effect")
        rendered["Effect"] = self.value(effect, names) if effect else "Allow"
        for key, field_name in (
            ("actions", "Action"),
            ("not_actions", "NotAction"),
            ("resources", "Resource"),
            ("not_resources", "NotResource"),
        ):
            if values := strings(key):
                rendered[field_name] = one_or_list(values)
        for kind, field_name in (("principals", "Principal"), ("not_principals", "NotPrincipal")):
            principals: dict[str, list[str]] = {}
            for principal, inner in self.expanded(block.blocks, kind, names):
                identifiers = self.value(principal.attributes["identifiers"], inner)
                principals.setdefault(self.value(principal.attributes["type"], inner), []).extend(identifiers)
            if principals == {"*": ["*"]}:
                rendered[field_name] = "*"
            elif principals:
                rendered[field_name] = {key: one_or_list(sorted(set(ids))) for key, ids in principals.items()}
        conditions: dict[str, dict] = {}
        for condition, inner in self.expanded(block.blocks, "condition", names):
            test, variable, values = (
                self.value(condition.attributes[key], inner) for key in ("test", "variable", "values")
            )
            conditions.setdefault(test, {})[variable] = one_or_list(values)
        if conditions:
            rendered["Condition"] = conditions
        return rendered
