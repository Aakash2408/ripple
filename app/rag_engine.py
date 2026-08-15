"""RAG engine for Ripple: learns fix patterns from git history and PropBench without any LLM."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class FixExample:
    trigger_description: str
    trigger_file: str
    trigger_diff: str
    fix_file: str
    fix_diff: str
    language: str
    change_type: str
    field_name: str
    embedding: Optional[list[float]] = field(default=None, repr=False)


class Embedder:
    """Embed text into vectors. Uses sentence-transformers if available, else pure-Python fallback."""

    def __init__(self):
        self._model = None
        self._vocab: dict[str, int] = {}
        self._use_transformer = False
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer('all-MiniLM-L6-v2')
            self._use_transformer = True
        except (ImportError, Exception):
            pass

    def embed(self, text: str) -> list[float]:
        if self._use_transformer and self._model is not None:
            vec = self._model.encode(text, normalize_embeddings=True)
            return vec.tolist()
        return self._fallback_embed(text)

    def _fallback_embed(self, text: str) -> list[float]:
        """Bag-of-words hashing into a fixed-size vector (256-dim)."""
        dim = 256
        vec = [0.0] * dim
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
        for w in words:
            idx = int(hashlib.md5(w.encode()).hexdigest(), 16) % dim
            vec[idx] += 1.0
        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def similarity(self, a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)


class RagStore:
    """Vector store for fix examples. Uses chromadb if available, else in-memory dict."""

    def __init__(self, collection_name: str = 'ripple_fixes', persist_dir: str = '/tmp/ripple_rag'):
        self._embedder = Embedder()
        self._use_chroma = False
        self._collection = None
        self._memory_store: list[tuple[str, FixExample]] = []  # (id, example) with embedding set

        try:
            import chromadb
            client = chromadb.Client(chromadb.Settings(
                chroma_db_impl='duckdb+parquet',
                persist_directory=persist_dir,
                anonymized_telemetry=False,
            ))
            self._collection = client.get_or_create_collection(
                name=collection_name,
                metadata={'hnsw:space': 'cosine'},
            )
            self._use_chroma = True
        except (ImportError, Exception):
            self._persist_path = Path(persist_dir) / f'{collection_name}.json'
            self._load_from_disk()

    def _load_from_disk(self):
        """Load in-memory store from disk if available."""
        if self._persist_path.exists():
            try:
                data = json.loads(self._persist_path.read_text())
                for item in data:
                    ex = FixExample(**{k: v for k, v in item.items()})
                    self._memory_store.append((self._make_id(ex), ex))
            except (json.JSONDecodeError, Exception):
                pass

    def _save_to_disk(self):
        """Persist in-memory store."""
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(ex) for _, ex in self._memory_store]
        self._persist_path.write_text(json.dumps(data, default=str))

    def _make_id(self, example: FixExample) -> str:
        content = f'{example.trigger_file}:{example.fix_file}:{example.field_name}:{example.change_type}'
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def add_example(self, example: FixExample) -> None:
        """Embed trigger description and store the example."""
        text = f'{example.change_type} {example.field_name} in {example.trigger_file}: {example.trigger_description}'
        embedding = self._embedder.embed(text)
        example.embedding = embedding
        doc_id = self._make_id(example)

        if self._use_chroma and self._collection is not None:
            self._collection.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text],
                metadatas=[{
                    'trigger_file': example.trigger_file,
                    'fix_file': example.fix_file,
                    'language': example.language,
                    'change_type': example.change_type,
                    'field_name': example.field_name,
                    'trigger_diff': example.trigger_diff[:500],
                    'fix_diff': example.fix_diff[:500],
                }],
            )
        else:
            # Deduplicate by id
            self._memory_store = [(i, e) for i, e in self._memory_store if i != doc_id]
            self._memory_store.append((doc_id, example))
            self._save_to_disk()

    def search_similar(self, query: str, top_k: int = 5) -> list[tuple[FixExample, float]]:
        """Find similar past fixes by embedding similarity."""
        query_vec = self._embedder.embed(query)

        if self._use_chroma and self._collection is not None:
            results = self._collection.query(query_embeddings=[query_vec], n_results=top_k)
            # Reconstruct FixExamples from metadata
            out: list[tuple[FixExample, float]] = []
            if results and results['metadatas']:
                distances = results['distances'][0] if results.get('distances') else [0.0] * len(results['metadatas'][0])
                for meta, dist in zip(results['metadatas'][0], distances):
                    ex = FixExample(
                        trigger_description=results['documents'][0][len(out)] if results.get('documents') else '',
                        trigger_file=meta.get('trigger_file', ''),
                        trigger_diff=meta.get('trigger_diff', ''),
                        fix_file=meta.get('fix_file', ''),
                        fix_diff=meta.get('fix_diff', ''),
                        language=meta.get('language', ''),
                        change_type=meta.get('change_type', ''),
                        field_name=meta.get('field_name', ''),
                    )
                    score = 1.0 - dist  # chromadb returns distance, convert to similarity
                    out.append((ex, score))
            return out
        else:
            # In-memory cosine search
            scored: list[tuple[FixExample, float]] = []
            for _, ex in self._memory_store:
                if ex.embedding:
                    sim = self._embedder.similarity(query_vec, ex.embedding)
                    scored.append((ex, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def count(self) -> int:
        if self._use_chroma and self._collection is not None:
            return self._collection.count()
        return len(self._memory_store)


# --- Indexers ---

SPEC_EXTENSIONS = {'.proto', '.graphql', '.gql', '.yaml', '.yml', '.json', '.avro', '.thrift', '.smithy'}
LANG_MAP = {
    '.go': 'go', '.ts': 'typescript', '.tsx': 'typescript', '.js': 'javascript',
    '.py': 'python', '.java': 'java', '.rs': 'rust', '.rb': 'ruby',
    '.kt': 'kotlin', '.cs': 'csharp', '.swift': 'swift', '.php': 'php',
    '.scala': 'scala', '.dart': 'dart',
}


def _detect_language(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    return LANG_MAP.get(ext, 'unknown')


def _detect_change_type(diff: str) -> str:
    if re.search(r'^-\s*\w+\s+\w+\s*=', diff, re.MULTILINE):
        return 'field_removed'
    if re.search(r'^\+\s*\w+\s+\w+\s*=', diff, re.MULTILINE):
        return 'field_added'
    if re.search(r'^-.*\btype\b', diff, re.MULTILINE) and re.search(r'^\+.*\btype\b', diff, re.MULTILINE):
        return 'type_changed'
    return 'modified'


def _extract_field_name(diff: str) -> str:
    # Try to find the field name from removed/added lines
    m = re.search(r'^[-+]\s*(?:optional|required|repeated)?\s*\w+\s+(\w+)\s*=', diff, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r'^[-+]\s*(\w+)\s*[=:]', diff, re.MULTILINE)
    if m:
        return m.group(1)
    return 'unknown'


def index_from_git(repo_path: str, store: RagStore, since: str = '12 months ago') -> dict:
    """Scan git log for commits with spec+consumer changes, index as fix examples."""
    stats = {'commits_scanned': 0, 'examples_stored': 0, 'languages': set()}

    try:
        log_output = subprocess.check_output(
            ['git', 'log', f'--since={since}', '--pretty=format:%H', '--diff-filter=M'],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {**stats, 'languages': list(stats['languages'])}

    if not log_output:
        return {**stats, 'languages': list(stats['languages'])}

    commits = log_output.split('\n')

    for commit_sha in commits:
        if not commit_sha.strip():
            continue
        stats['commits_scanned'] += 1

        try:
            files_output = subprocess.check_output(
                ['git', 'diff-tree', '--no-commit-id', '-r', '--name-only', commit_sha],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            continue

        files = files_output.split('\n') if files_output else []
        spec_files = [f for f in files if Path(f).suffix.lower() in SPEC_EXTENSIONS]
        consumer_files = [f for f in files if f not in spec_files and Path(f).suffix.lower() in LANG_MAP]

        if not spec_files or not consumer_files:
            continue

        # Get diffs
        for spec_f in spec_files:
            try:
                spec_diff = subprocess.check_output(
                    ['git', 'diff', f'{commit_sha}~1', commit_sha, '--', spec_f],
                    cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                continue

            for cons_f in consumer_files:
                try:
                    cons_diff = subprocess.check_output(
                        ['git', 'diff', f'{commit_sha}~1', commit_sha, '--', cons_f],
                        cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
                    )
                except subprocess.CalledProcessError:
                    continue

                lang = _detect_language(cons_f)
                change_type = _detect_change_type(spec_diff)
                field_name = _extract_field_name(spec_diff)

                example = FixExample(
                    trigger_description=f'{change_type} {field_name} in {spec_f}',
                    trigger_file=spec_f,
                    trigger_diff=spec_diff[:2000],
                    fix_file=cons_f,
                    fix_diff=cons_diff[:2000],
                    language=lang,
                    change_type=change_type,
                    field_name=field_name,
                )
                store.add_example(example)
                stats['examples_stored'] += 1
                stats['languages'].add(lang)

    stats['languages'] = sorted(stats['languages'])
    return stats


def index_from_propbench(propbench_dir: str, store: RagStore) -> dict:
    """Load PropBench YAML entries and index as fix examples."""
    stats = {'entries_loaded': 0, 'examples_stored': 0, 'languages': set()}

    try:
        import yaml
    except ImportError:
        # Minimal YAML parser for simple PropBench entries
        yaml = None

    dataset_dir = Path(propbench_dir) / 'datasets'
    if not dataset_dir.exists():
        dataset_dir = Path(propbench_dir)

    yaml_files = list(dataset_dir.glob('**/*.yaml')) + list(dataset_dir.glob('**/*.yml'))

    for yf in yaml_files:
        try:
            content = yf.read_text()
            if yaml:
                docs = list(yaml.safe_load_all(content))
            else:
                docs = _minimal_yaml_parse(content)

            for doc in docs:
                if not doc or not isinstance(doc, dict):
                    continue
                trigger = doc.get('trigger', {}) or {}
                consequences = doc.get('consequences', []) or []
                if not consequences:
                    continue

                stats['entries_loaded'] += 1
                trigger_file = trigger.get('file', '')
                trigger_desc = trigger.get('description', '') or f"change in {trigger_file}"
                change_type = trigger.get('change_type', 'modified')
                field_name = trigger.get('field_name', '') or trigger.get('name', 'unknown')

                for cons in consequences:
                    if not isinstance(cons, dict):
                        continue
                    fix_file = cons.get('file', '')
                    lang = _detect_language(fix_file)

                    example = FixExample(
                        trigger_description=trigger_desc,
                        trigger_file=trigger_file,
                        trigger_diff=trigger.get('diff', ''),
                        fix_file=fix_file,
                        fix_diff=cons.get('diff', ''),
                        language=lang,
                        change_type=change_type,
                        field_name=field_name,
                    )
                    store.add_example(example)
                    stats['examples_stored'] += 1
                    stats['languages'].add(lang)
        except Exception:
            continue

    stats['languages'] = sorted(stats['languages'])
    return stats


def _minimal_yaml_parse(content: str) -> list[dict]:
    """Very basic YAML-like parser for PropBench entries when PyYAML unavailable."""
    docs: list[dict] = []
    current: dict = {}
    for line in content.split('\n'):
        if line.strip() == '---':
            if current:
                docs.append(current)
            current = {}
        elif ':' in line and not line.startswith(' '):
            key, _, val = line.partition(':')
            current[key.strip()] = val.strip()
    if current:
        docs.append(current)
    return docs
