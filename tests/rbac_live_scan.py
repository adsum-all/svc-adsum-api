"""Scan the LIVE backend code and compute the roles each endpoint admits.

Resolves both the legacy ``require_roles(...)`` gates and the new
``require_permission("...")`` gates (directly, via *tuple unpacking, via a
module-level alias, or via an alias imported from another module) so a conformity
test can assert, at any point of the switchover, that every endpoint still admits
exactly its Phase A role-set. This is the anti-silent-bug guarantee: a wrong
permission on a switched endpoint or a lost gate is caught immediately.
"""
# ruff: noqa: E501
from __future__ import annotations

import ast
import glob
import os
from typing import Any

from app.permissions_data import ROLE_PERMISSIONS

APP = os.path.join(os.path.dirname(__file__), "..", "app")
ROLES = ["membre", "controleur", "gestionnaire", "direction", "admin", "super_admin"]
MEMBER_DEPS = {"_membre", "_membre_ctx", "require_membre", "_require_membre"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _const_strs(seq: list[ast.expr]) -> list[str]:
    return [a.value for a in seq if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _roles_holding(permission: str) -> set[str]:
    return {role for role in ROLES if permission in ROLE_PERMISSIONS.get(role, frozenset())}


def _alias_from_call(val: ast.Call, tuples: dict[str, list[str]]) -> tuple[str, Any] | None:
    fn = val.func
    nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
    if nm == "require_roles":
        r = _const_strs(val.args)
        for a in val.args:
            if isinstance(a, ast.Starred) and isinstance(a.value, ast.Name) and a.value.id in tuples:
                r += tuples[a.value.id]
        return ("roles", sorted(set(r)))
    if nm == "require_permission" and val.args and isinstance(val.args[0], ast.Constant):
        return ("perm", val.args[0].value)
    return None


def _parse_module(path: str) -> tuple[ast.Module, dict[str, list[str]], dict[str, tuple[str, Any]], dict[str, str], dict[str, tuple[str, str]]]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    tuples: dict[str, list[str]] = {}
    aliases: dict[str, tuple[str, Any]] = {}
    routers: dict[str, str] = {}
    imports: dict[str, tuple[str, str]] = {}  # local name -> (source module, source name)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = node.value
            if isinstance(val, (ast.Tuple, ast.List)):
                s = _const_strs(val.elts)
                if s:
                    tuples[node.targets[0].id] = s
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 1:
            for a in node.names:
                imports[a.asname or a.name] = (node.module, a.name)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = node.value
            if isinstance(val, ast.Call):
                fn = val.func
                nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                if nm == "APIRouter":
                    pref = ""
                    for kw in val.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            pref = kw.value.value
                    routers[node.targets[0].id] = pref
                else:
                    res = _alias_from_call(val, tuples)
                    if res:
                        aliases[node.targets[0].id] = res
    return tree, tuples, aliases, routers, imports


def _resolve(dnode: ast.expr, ctx: dict[str, Any], registry: dict[str, dict[str, tuple[str, Any]]]) -> tuple[str, Any]:
    tuples, aliases, imports = ctx["tuples"], ctx["aliases"], ctx["imports"]
    if isinstance(dnode, ast.Call):
        res = _alias_from_call(dnode, tuples)
        if res:
            return res
        fn = dnode.func
        nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
        return (nm or "call", None)
    if isinstance(dnode, ast.Name):
        n = dnode.id
        if n in aliases:
            return aliases[n]
        if n in imports:
            src_mod, src_name = imports[n]
            if src_mod in registry and src_name in registry[src_mod]:
                return registry[src_mod][src_name]
        if n in MEMBER_DEPS:
            return ("member", None)
        if n == "require_perimetre":
            return ("perimetre", None)
        if n == "current_user":
            return ("auth", None)
        return ("alias:" + n, None)
    return ("?", None)


def scan() -> dict[str, dict[str, Any]]:
    """Return {"METHOD /path": {"kind": ..., "admis": sorted-roles-or-marker, "cle": ...}}."""
    files = sorted(glob.glob(os.path.join(APP, "*.py")))
    parsed = {os.path.splitext(os.path.basename(p))[0]: _parse_module(p) for p in files}
    # global registry of module-level aliases per module, for cross-file imports
    registry = {mod: parts[2] for mod, parts in parsed.items()}

    out: dict[str, dict[str, Any]] = {}
    for parts in parsed.values():
        tree, tuples, aliases, routers, imports = parts
        ctx = {"tuples": tuples, "aliases": aliases, "imports": imports}

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name)):
                    continue
                method = dec.func.attr.upper()
                if method not in _METHODS:
                    continue
                pref = routers.get(dec.func.value.id, "?")
                sub = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else ""
                full = (pref + sub) if pref != "?" else sub
                gate: tuple[str, Any] = ("NONE", None)
                allargs = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
                for a in allargs:
                    holders = [a.annotation]
                    if a in node.args.kwonlyargs:
                        holders.append(node.args.kw_defaults[node.args.kwonlyargs.index(a)])
                    deps: list[ast.expr] = []
                    for holder in holders:
                        if holder is None:
                            continue
                        for sx in ast.walk(holder):
                            if isinstance(sx, ast.Call) and (
                                (isinstance(sx.func, ast.Name) and sx.func.id == "Depends")
                                or (isinstance(sx.func, ast.Attribute) and sx.func.attr == "Depends")
                            ) and sx.args:
                                deps.append(sx.args[0])
                    for d in deps:
                        k = _resolve(d, ctx, registry)
                        if k[0] != "?":
                            gate = k
                            break
                    if gate[0] != "NONE":
                        break
                kind, payload = gate
                if kind == "roles":
                    admis = sorted(payload)
                elif kind == "perm":
                    admis = sorted(_roles_holding(payload))
                elif kind in ("member", "auth"):
                    admis = sorted(ROLES)
                elif kind == "perimetre":
                    admis = ["PERIMETRE"]
                elif kind == "NONE":
                    admis = ["PUBLIC"]
                else:
                    admis = ["UNRESOLVED:" + kind]
                out[f"{method} {full}"] = {"kind": kind, "admis": admis, "cle": payload if kind == "perm" else None}
    return out
