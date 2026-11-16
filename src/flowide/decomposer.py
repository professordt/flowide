#!/usr/bin/env python3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import ast
import os
import re
import sqlite3
import sys

LATTICE_SIZE = 3200
MAX_LOC_BUDGET = 150
IGNORE_DIRS = {".git", ".flow", ".venv", "venv", "__pycache__", "node_modules", "target", "dist", "build", ".next", "vendor"}

LANG_PATTERNS: Dict[str, List[Tuple[str, re.Pattern[str]]]] = {
    "rust": [
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]+\"\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("impl", re.compile(r"^\s*(?:unsafe\s+)?impl(?:<[^>]+>)?\s+(?:[A-Za-z_][A-Za-z0-9_:<>]*\s+for\s+)?([A-Za-z_][A-Za-z0-9_]*)")),
    ],
    "typescript": [
        ("fn", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("const_fn", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=>")),
        ("type", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?(?:class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
    ],
    "go": [
        ("method", re.compile(r"^\s*func\s*\([^)]+\)\s*([A-Za-z_][A-Za-z0-9_]*)")),
        ("fn", re.compile(r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b")),
    ],
}

COMMON_KEYWORDS = {
    "if", "else", "for", "while", "loop", "match", "switch", "case", "return",
    "let", "const", "var", "mut", "pub", "fn", "func", "type", "struct", "enum",
    "trait", "impl", "class", "interface", "export", "import", "from", "async",
    "await", "self", "Self", "this", "super", "true", "false", "null", "nil",
    "None", "Some", "Ok", "Err", "string", "number", "boolean", "void", "any",
    "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize",
    "f32", "f64", "bool", "str", "String", "Vec", "Option", "Result", "int",
    "int64", "uint64", "float64", "byte", "rune", "error", "err", "ctx"
}

@dataclass
class ExtractedSymbol:
    qualname: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    signature_text: str
    referenced_names: Set[str] = field(default_factory=set)
    chunk_index: int = 0
    total_chunks: int = 1

    @property
    def loc_count(self) -> int:
        return max(1, self.end_line - self.start_line + 1)

def _slice_oversized_symbols(symbols: List[ExtractedSymbol]) -> List[ExtractedSymbol]:
    sliced: List[ExtractedSymbol] = []
    for sym in symbols:
        if sym.loc_count <= MAX_LOC_BUDGET:
            sliced.append(sym)
            continue
        num_chunks = (sym.loc_count + MAX_LOC_BUDGET - 1) // MAX_LOC_BUDGET
        for idx in range(num_chunks):
            c_start = sym.start_line + idx * MAX_LOC_BUDGET
            c_end = min(sym.end_line, c_start + MAX_LOC_BUDGET - 1)
            sliced.append(ExtractedSymbol(
                qualname=f"{sym.qualname}[part_{idx + 1}/{num_chunks}]",
                file_path=sym.file_path, language=sym.language,
                start_line=c_start, end_line=c_end,
                signature_text=f"{sym.signature_text}  // Slice {idx + 1}/{num_chunks} (L{c_start}-L{c_end})",
                referenced_names=sym.referenced_names, chunk_index=idx, total_chunks=num_chunks,
            ))
    return sliced

def _format_py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str = "") -> str:
    async_kw = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args_str = ast.unparse(node.args)
    ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    doc = ast.get_docstring(node, clean=True)
    doc_line = f'    """{doc.splitlines()[0]}"""' if doc else ""
    header = f"{async_kw}def {prefix}{node.name}({args_str}){ret_str}:"
    return f"{header}\n{doc_line}" if doc_line else header

def _collect_py_identifiers(node: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names - COMMON_KEYWORDS

def extract_python_symbols(file_path: Path, repo_root: Path) -> List[ExtractedSymbol]:
    rel_path = file_path.relative_to(repo_root).as_posix()
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=rel_path)
    raw_symbols: List[ExtractedSymbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            raw_symbols.append(ExtractedSymbol(
                qualname=f"{rel_path}::{node.name}", file_path=rel_path, language="python",
                start_line=node.lineno, end_line=end_line, signature_text=_format_py_signature(node),
                referenced_names=_collect_py_identifiers(node) - {node.name},
            ))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            raw_symbols.append(ExtractedSymbol(
                qualname=f"{rel_path}::{node.name}", file_path=rel_path, language="python",
                start_line=node.lineno, end_line=node.lineno,
                signature_text=f"class {node.name}({bases}):" if bases else f"class {node.name}:",
                referenced_names={ast.unparse(b) for b in node.bases},
            ))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end_line = getattr(item, "end_lineno", item.lineno)
                    raw_symbols.append(ExtractedSymbol(
                        qualname=f"{rel_path}::{node.name}.{item.name}", file_path=rel_path, language="python",
                        start_line=item.lineno, end_line=end_line,
                        signature_text=_format_py_signature(item, prefix=f"{node.name}."),
                        referenced_names=_collect_py_identifiers(item) - {item.name, "self", "cls"},
                    ))
    return _slice_oversized_symbols(raw_symbols)

def _find_matching_brace_line(lines: List[str], start_idx: int) -> int:
    depth = 0
    saw_open = False
    in_block_comment = False
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        i, n = 0, len(line)
        in_str: Optional[str] = None
        while i < n:
            ch = line[i]
            nxt = line[i + 1] if i + 1 < n else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str is not None:
                if ch == "\\" and in_str != "`":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in ('"', "'", "`"):
                in_str = ch
                i += 1
                continue
            if ch == "{":
                depth += 1
                saw_open = True
            elif ch == "}":
                depth -= 1
                if saw_open and depth <= 0:
                    return idx
            i += 1
        if not saw_open and line.rstrip().endswith(";"):
            return idx
    return len(lines) - 1

def _extract_brace_signature(lines: List[str], start_idx: int, end_idx: int) -> str:
    sig_lines: List[str] = []
    for idx in range(start_idx, min(end_idx + 1, start_idx + 8)):
        text = lines[idx].rstrip()
        if "{" in text:
            head = text.split("{", 1)[0].rstrip()
            if head:
                sig_lines.append(head + " { ... }")
            break
        sig_lines.append(text)
        if text.endswith(";"):
            break
    return "\n".join(sig_lines).strip()

def extract_brace_language_symbols(file_path: Path, repo_root: Path, lang: str) -> List[ExtractedSymbol]:
    rel_path = file_path.relative_to(repo_root).as_posix()
    lines = file_path.read_text(encoding="utf-8").splitlines()
    patterns = LANG_PATTERNS[lang]
    raw_symbols: List[ExtractedSymbol] = []
    idx, n_lines = 0, len(lines)

    while idx < n_lines:
        line = lines[idx]
        matched_kind: Optional[str] = None
        matched_name: Optional[str] = None
        for kind, regex in patterns:
            m = regex.match(line)
            if m:
                matched_kind, matched_name = kind, m.group(1)
                break
        if matched_kind and matched_name:
            end_idx = _find_matching_brace_line(lines, idx)
            block_text = "\n".join(lines[idx : end_idx + 1])
            refs = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", block_text)) - COMMON_KEYWORDS - {matched_name}
            sig_text = _extract_brace_signature(lines, idx, end_idx)
            if lang == "rust" and matched_kind == "impl" and (end_idx - idx + 1) > MAX_LOC_BUDGET:
                raw_symbols.append(ExtractedSymbol(
                    qualname=f"{rel_path}::impl_{matched_name}", file_path=rel_path, language=lang,
                    start_line=idx + 1, end_line=idx + 1, signature_text=sig_text, referenced_names={matched_name},
                ))
                idx += 1
                continue
            qual = f"{rel_path}::impl_{matched_name}" if matched_kind == "impl" else f"{rel_path}::{matched_name}"
            raw_symbols.append(ExtractedSymbol(
                qualname=qual, file_path=rel_path, language=lang,
                start_line=idx + 1, end_line=end_idx + 1, signature_text=sig_text, referenced_names=refs,
            ))
            idx = end_idx + 1
        else:
            idx += 1
    return _slice_oversized_symbols(raw_symbols)

def detect_language(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext == ".rs":
        return "rust"
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext == ".go":
        return "go"
    return None

def resolve_dependencies(symbols: List[ExtractedSymbol]) -> Dict[str, Set[str]]:
    short_name_map: Dict[str, List[str]] = {}
    for sym in symbols:
        base = sym.qualname.split("::")[-1].split("[")[0].split(".")[-1]
        if base.startswith("impl_"):
            base = base[5:]
        short_name_map.setdefault(base, []).append(sym.qualname)

    deps: Dict[str, Set[str]] = {sym.qualname: set() for sym in symbols}
    by_base_qual: Dict[str, List[ExtractedSymbol]] = {}
    for sym in symbols:
        by_base_qual.setdefault(sym.qualname.split("[")[0], []).append(sym)
    for slices in by_base_qual.values():
        if len(slices) > 1:
            slices.sort(key=lambda s: s.chunk_index)
            for i in range(1, len(slices)):
                deps[slices[i].qualname].add(slices[i - 1].qualname)

    for sym in symbols:
        for ref in sym.referenced_names:
            for target_q in short_name_map.get(ref, []):
                if target_q != sym.qualname:
                    deps[sym.qualname].add(target_q)
    return deps

def topological_sort_symbols(symbols: List[ExtractedSymbol], deps: Dict[str, Set[str]]) -> List[ExtractedSymbol]:
    sym_map = {s.qualname: s for s in symbols}
    in_degree = {q: len(d) for q, d in deps.items()}
    reverse_deps: Dict[str, Set[str]] = {q: set() for q in sym_map}
    for downstream, upstreams in deps.items():
        for up in upstreams:
            reverse_deps[up].add(downstream)

    ready = sorted([q for q, deg in in_degree.items() if deg == 0])
    ordered: List[ExtractedSymbol] = []
    visited: Set[str] = set()
    while len(ordered) < len(symbols):
        nxt = sorted(set(sym_map.keys()) - visited)[0] if not ready else ready.pop(0)
        if nxt in visited:
            continue
        visited.add(nxt)
        ordered.append(sym_map[nxt])
        for child in sorted(reverse_deps[nxt]):
            in_degree[child] -= 1
            if in_degree[child] <= 0 and child not in visited:
                ready.append(child)
        ready.sort()
    return ordered

def assign_rational_coordinates(ordered_symbols: List[ExtractedSymbol]) -> List[Tuple[int, int, ExtractedSymbol]]:
    total = len(ordered_symbols)
    if total == 0:
        return []
    if total > 850:
        raise ValueError(f"Repository slice count ({total}) exceeds single-pass active capacity (850 nodes).")
    assigned: List[Tuple[int, int, ExtractedSymbol]] = []
    for idx, sym in enumerate(ordered_symbols):
        node_id = 500 + ((idx * 850) // total)
        assigned.append((node_id, (node_id // 100) + 1, sym))
    return assigned

def populate_sqlite(db_path: Path, assigned: List[Tuple[int, int, ExtractedSymbol]], deps: Dict[str, Set[str]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS roadmap_nodes (
            node_id         INTEGER PRIMARY KEY CHECK (node_id >= 0 AND node_id < 3200),
            band            INTEGER NOT NULL CHECK (band >= 1 AND band <= 32),
            title           TEXT NOT NULL,
            target_path     TEXT NOT NULL,
            max_loc_budget  INTEGER NOT NULL DEFAULT 150,
            progress_num    INTEGER NOT NULL DEFAULT 0,
            progress_den    INTEGER NOT NULL DEFAULT 1 CHECK (progress_den > 0),
            stall_count     INTEGER NOT NULL DEFAULT 0,
            updated_at_iso  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roadmap_edges (
            upstream_id     INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            downstream_id   INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            PRIMARY KEY (upstream_id, downstream_id),
            CHECK (upstream_id != downstream_id)
        );
        CREATE TABLE IF NOT EXISTS symbol_index (
            symbol_qualname TEXT PRIMARY KEY,
            language        TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            start_line      INTEGER NOT NULL,
            end_line        INTEGER NOT NULL,
            loc_count       INTEGER NOT NULL,
            signature_text  TEXT NOT NULL,
            node_id         INTEGER REFERENCES roadmap_nodes(node_id) ON DELETE SET NULL
        );
    """)
    now_iso = datetime.now(timezone.utc).isoformat()
    qual_to_node: Dict[str, int] = {sym.qualname: nid for nid, _, sym in assigned}

    for node_id, band, sym in assigned:
        title = f"[{sym.language}] {sym.qualname} (L{sym.start_line}-L{sym.end_line}, {sym.loc_count} LOC)"
        conn.execute(
            """
            INSERT INTO roadmap_nodes (
                node_id, band, title, target_path, max_loc_budget,
                progress_num, progress_den, stall_count, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, 0, 1, 0, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                band=excluded.band, title=excluded.title, target_path=excluded.target_path,
                max_loc_budget=excluded.max_loc_budget, updated_at_iso=excluded.updated_at_iso;
            """,
            (node_id, band, title, sym.file_path, MAX_LOC_BUDGET, now_iso),
        )
        conn.execute(
            """
            INSERT INTO symbol_index (
                symbol_qualname, language, file_path, start_line, end_line,
                loc_count, signature_text, node_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol_qualname) DO UPDATE SET
                language=excluded.language, file_path=excluded.file_path,
                start_line=excluded.start_line, end_line=excluded.end_line,
                loc_count=excluded.loc_count, signature_text=excluded.signature_text,
                node_id=excluded.node_id;
            """,
            (sym.qualname, sym.language, sym.file_path, sym.start_line, sym.end_line, sym.loc_count, sym.signature_text, node_id),
        )

    for downstream_q, upstream_set in deps.items():
        down_id = qual_to_node.get(downstream_q)
        if down_id is None:
            continue
        for up_q in upstream_set:
            up_id = qual_to_node.get(up_q)
            if up_id is not None and up_id < down_id:
                conn.execute("INSERT OR IGNORE INTO roadmap_edges (upstream_id, downstream_id) VALUES (?, ?);", (up_id, down_id))
    conn.commit()
    conn.close()

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 decomposer.py <repo_dir> [db_path]", file=sys.stderr)
        sys.exit(1)
    repo_root = Path(sys.argv[1]).resolve()
    default_db = repo_root / ".flow" / "flow_substrate.db"
    db_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(os.environ.get("FLOW_DB_PATH", str(default_db)))

    all_symbols: List[ExtractedSymbol] = []
    lang_counts: Dict[str, int] = {}
    for src_file in sorted(repo_root.rglob("*")):
        if not src_file.is_file():
            continue
        if any(part in IGNORE_DIRS or part.startswith(".") for part in src_file.relative_to(repo_root).parts):
            continue
        lang = detect_language(src_file)
        if not lang:
            continue
        extracted = extract_python_symbols(src_file, repo_root) if lang == "python" else extract_brace_language_symbols(src_file, repo_root, lang)
        all_symbols.extend(extracted)
        lang_counts[lang] = lang_counts.get(lang, 0) + len(extracted)

    deps = resolve_dependencies(all_symbols)
    ordered = topological_sort_symbols(all_symbols, deps)
    assigned = assign_rational_coordinates(ordered)
    populate_sqlite(db_path, assigned, deps)

    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(lang_counts.items())) or "none"
    print(f"Indexed {len(assigned)} atomic nodes (<= {MAX_LOC_BUDGET} LOC) [{breakdown}] -> {db_path}")

if __name__ == "__main__":
    main()
