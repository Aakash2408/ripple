"""RAG engine for Ripple: learns fix patterns from git history and PropBench without any LLM."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# --- Data Classes ---

@dataclass
class StructuredPattern:
    """Structured representation of a code change pattern extracted from diffs."""
    action: str  # remove / rename / retype / add / modify
    target: str  # struct_field / param / interface_prop / class_field / function / import
    language: str
    naming_pattern: str  # snake / camel / pascal / upper_snake / kebab / unknown
    affected_count: int  # number of lines changed
    co_changes: list[str] = field(default_factory=list)  # other files changed in same commit


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
    # Enhanced fields
    pattern: Optional[StructuredPattern] = None
    framework: str = ''  # rails / express / spring / gin / fastapi / django / etc
    repo_name: str = ''
    added_at: float = field(default_factory=time.time)  # timestamp
    merged_count: int = 0
    total_count: int = 0
    example_id: str = ''


@dataclass
class StrategyCluster:
    """A cluster of similar fix patterns grouped by action + target + language."""
    archetype_name: str
    example_count: int
    avg_confidence: float
    representative_example: FixExample
    example_ids: list[str] = field(default_factory=list)


# --- Embedder ---

class Embedder:
    """Embed text into vectors. Tries: sentence-transformers -> TF-IDF -> bag-of-words."""

    def __init__(self):
        self._model = None
        self._tfidf = None
        self._use_transformer = False
        self._use_tfidf = False
        self.model_name: str = 'bag-of-words'

        # Try sentence-transformers first
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer('all-MiniLM-L6-v2')
            self._use_transformer = True
            self.model_name = 'all-MiniLM-L6-v2'
            return
        except (ImportError, Exception):
            pass

        # Try sklearn TF-IDF
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self._tfidf = TfidfVectorizer(max_features=384, sublinear_tf=True)
            self._tfidf_fitted = False
            self._tfidf_corpus: list[str] = []
            self._use_tfidf = True
            self.model_name = 'tfidf-sklearn'
        except (ImportError, Exception):
            pass

    def embed(self, text: str) -> list[float]:
        if self._use_transformer and self._model is not None:
            vec = self._model.encode(text, normalize_embeddings=True)
            return vec.tolist()
        if self._use_tfidf and self._tfidf is not None:
            return self._tfidf_embed(text)
        return self._bow_embed(text)

    def _tfidf_embed(self, text: str) -> list[float]:
        """TF-IDF embedding with incremental fitting."""
        self._tfidf_corpus.append(text)
        if len(self._tfidf_corpus) >= 2:
            try:
                self._tfidf.fit(self._tfidf_corpus)
                self._tfidf_fitted = True
            except Exception:
                return self._bow_embed(text)
        if self._tfidf_fitted:
            try:
                vec = self._tfidf.transform([text]).toarray()[0]
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                return [float(x / norm) for x in vec]
            except Exception:
                return self._bow_embed(text)
        return self._bow_embed(text)

    def _bow_embed(self, text: str) -> list[float]:
        """Bag-of-words hashing into a fixed-size vector (256-dim)."""
        dim = 256
        vec = [0.0] * dim
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
        for w in words:
            idx = int(hashlib.md5(w.encode()).hexdigest(), 16) % dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def similarity(self, a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        min_len = min(len(a), len(b))
        a, b = a[:min_len], b[:min_len]
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)


# --- RagStore ---

class RagStore:
    """Vector store for fix examples with multi-signal ranking."""

    def __init__(self, collection_name: str = 'ripple_fixes', persist_dir: str = '/tmp/ripple_rag'):
        self._embedder = Embedder()
        self._memory_store: list[tuple[str, FixExample]] = []
        self._persist_path = Path(persist_dir) / f'{collection_name}.json'
        self._load_from_disk()

    def _load_from_disk(self):
        if self._persist_path.exists():
            try:
                data = json.loads(self._persist_path.read_text())
                for item in data:
                    # Handle StructuredPattern reconstruction
                    pattern_data = item.pop('pattern', None)
                    pattern = None
                    if pattern_data and isinstance(pattern_data, dict):
                        pattern = StructuredPattern(**pattern_data)
                    ex = FixExample(**{k: v for k, v in item.items() if k in FixExample.__dataclass_fields__})
                    ex.pattern = pattern
                    if not ex.example_id:
                        ex.example_id = self._make_id(ex)
                    self._memory_store.append((ex.example_id, ex))
            except (json.JSONDecodeError, Exception):
                pass

    def _save_to_disk(self):
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for _, ex in self._memory_store:
            d = asdict(ex)
            data.append(d)
        self._persist_path.write_text(json.dumps(data, default=str))

    def _make_id(self, example: FixExample) -> str:
        content = f'{example.trigger_file}:{example.fix_file}:{example.field_name}:{example.change_type}:{example.repo_name}'
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def add_example(self, example: FixExample) -> str:
        """Embed and store an example. Returns the example_id."""
        text = f'{example.change_type} {example.field_name} in {example.trigger_file}: {example.trigger_description}'
        embedding = self._embedder.embed(text)
        example.embedding = embedding
        doc_id = self._make_id(example)
        example.example_id = doc_id

        # Deduplicate
        self._memory_store = [(i, e) for i, e in self._memory_store if i != doc_id]
        self._memory_store.append((doc_id, example))
        self._save_to_disk()
        return doc_id

    def search_similar(self, query: str, top_k: int = 5, language: str = '',
                       repo_name: str = '', include_cross_repo: bool = True) -> list[tuple[FixExample, float]]:
        """Multi-signal ranked search: embedding + language + recency + success_rate."""
        query_vec = self._embedder.embed(query)
        now = time.time()
        scored: list[tuple[FixExample, float]] = []

        for _, ex in self._memory_store:
            if not ex.embedding:
                continue

            # 1. Embedding similarity (weight: 0.5)
            embed_sim = self._embedder.similarity(query_vec, ex.embedding)

            # 2. Language match (weight: 0.2)
            lang_score = 1.0 if (language and ex.language == language) else 0.0

            # 3. Recency score (weight: 0.2) -- decays over 90 days
            age_days = (now - ex.added_at) / 86400.0
            recency = max(0.0, 1.0 - (age_days / 90.0))

            # 4. Success rate (weight: 0.1)
            if ex.total_count > 0:
                success_rate = ex.merged_count / ex.total_count
            else:
                success_rate = 0.5  # neutral for untracked

            # Confidence calibration: decay low-merge patterns
            calibration = 1.0
            if ex.total_count >= 3 and success_rate < 0.3:
                calibration = 0.5

            combined = (0.5 * embed_sim + 0.2 * lang_score + 0.2 * recency + 0.1 * success_rate) * calibration

            # Cross-repo filtering
            if not include_cross_repo and repo_name and ex.repo_name and ex.repo_name != repo_name:
                continue

            scored.append((ex, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_by_id(self, example_id: str) -> Optional[FixExample]:
        for eid, ex in self._memory_store:
            if eid == example_id:
                return ex
        return None

    def record_outcome(self, example_id: str, merged: bool) -> None:
        """Track merge success for confidence calibration."""
        for i, (eid, ex) in enumerate(self._memory_store):
            if eid == example_id:
                ex.total_count += 1
                if merged:
                    ex.merged_count += 1
                self._memory_store[i] = (eid, ex)
                self._save_to_disk()
                return

    def count(self) -> int:
        return len(self._memory_store)

    def all_examples(self) -> list[FixExample]:
        return [ex for _, ex in self._memory_store]


# --- Pattern Extraction ---

SPEC_EXTENSIONS = {'.proto', '.graphql', '.gql', '.yaml', '.yml', '.json', '.avro', '.thrift', '.smithy'}
LANG_MAP = {
    '.go': 'go', '.ts': 'typescript', '.tsx': 'typescript', '.js': 'javascript',
    '.py': 'python', '.java': 'java', '.rs': 'rust', '.rb': 'ruby',
    '.kt': 'kotlin', '.cs': 'csharp', '.swift': 'swift', '.php': 'php',
    '.scala': 'scala', '.dart': 'dart',
    # Config and scripts. Measured on the PropBench replay: 137 files a real PR
    # had to change were skipped for having no matcher -- 24 of 36 on
    # kubernetes#109798, i.e. most of that change. Manifests and scripts
    # reference removed resources by name just as source does.
    '.yaml': 'yaml', '.yml': 'yaml',
    '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell',
}

FRAMEWORK_PATTERNS = {
    'rails': ['/app/controllers/', '/app/models/', 'Gemfile', 'config/routes.rb'],
    'express': ['package.json', '/routes/', '/middleware/', 'express'],
    'spring': ['/src/main/java/', 'pom.xml', 'build.gradle', '@Controller', '@Service'],
    'gin': ['/handlers/', '/routes/', 'gin.Context', 'go.mod'],
    'fastapi': ['fastapi', '@app.get', '@app.post', 'Depends('],
    'django': ['/views.py', '/models.py', '/urls.py', 'manage.py', 'settings.py'],
    'flask': ['flask', '@app.route', 'Blueprint'],
    'nestjs': ['@Module', '@Controller', '@Injectable', '.module.ts'],
    'axum': ['axum::', 'Router::new()', 'Cargo.toml'],
    'actix': ['actix_web', 'HttpServer', 'Cargo.toml'],
}


def _detect_language(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    return LANG_MAP.get(ext, 'unknown')


def _detect_framework(files: list[str]) -> str:
    """Detect framework from file paths in a commit."""
    joined = ' '.join(files)
    for framework, patterns in FRAMEWORK_PATTERNS.items():
        matches = sum(1 for p in patterns if p in joined)
        if matches >= 2:
            return framework
    return ''


def _detect_naming_pattern(name: str) -> str:
    if '_' in name and name == name.lower():
        return 'snake'
    if '_' in name and name == name.upper():
        return 'upper_snake'
    if '-' in name:
        return 'kebab'
    if name[0:1].isupper() and not '_' in name:
        return 'pascal'
    if name[0:1].islower() and any(c.isupper() for c in name[1:]):
        return 'camel'
    return 'unknown'


def _detect_action(diff: str) -> str:
    removed = len(re.findall(r'^-[^-]', diff, re.MULTILINE))
    added = len(re.findall(r'^\+[^+]', diff, re.MULTILINE))
    if removed > 0 and added == 0:
        return 'remove'
    if removed > 0 and added > 0:
        # Check for rename patterns
        old_names = set(re.findall(r'^-\s*(?:\w+\s+)?(\w+)', diff, re.MULTILINE))
        new_names = set(re.findall(r'^\+\s*(?:\w+\s+)?(\w+)', diff, re.MULTILINE))
        if old_names and new_names and not old_names & new_names:
            return 'rename'
        # Check for type change
        if re.search(r'^-.*:\s*\w+', diff, re.MULTILINE) and re.search(r'^\+.*:\s*\w+', diff, re.MULTILINE):
            return 'retype'
        return 'modify'
    if added > 0 and removed == 0:
        return 'add'
    return 'modify'


def _detect_target(diff: str, language: str) -> str:
    if re.search(r'(struct|message|model)\s+\w+', diff):
        return 'struct_field'
    if re.search(r'(interface|protocol|trait)\s+\w+', diff):
        return 'interface_prop'
    if re.search(r'(class)\s+\w+', diff):
        return 'class_field'
    if re.search(r'(func|def|fn|function)\s+\w+\s*\(', diff):
        return 'param'
    return 'struct_field'


def extract_structured_pattern(diff: str, language: str, co_change_files: list[str] = None) -> StructuredPattern:
    """Extract a StructuredPattern from a diff."""
    action = _detect_action(diff)
    target = _detect_target(diff, language)
    affected_lines = len(re.findall(r'^[+-][^+-]', diff, re.MULTILINE))

    # Detect naming from field names in diff
    field_names = re.findall(r'^[-+]\s*(?:\w+\s+)?(\w+)\s*[=:;(]', diff, re.MULTILINE)
    naming = _detect_naming_pattern(field_names[0]) if field_names else 'unknown'

    return StructuredPattern(
        action=action,
        target=target,
        language=language,
        naming_pattern=naming,
        affected_count=affected_lines,
        co_changes=co_change_files or [],
    )


def _detect_change_type(diff: str) -> str:
    if re.search(r'^-\s*\w+\s+\w+\s*=', diff, re.MULTILINE):
        return 'field_removed'
    if re.search(r'^\+\s*\w+\s+\w+\s*=', diff, re.MULTILINE):
        return 'field_added'
    if re.search(r'^-.*\btype\b', diff, re.MULTILINE) and re.search(r'^\+.*\btype\b', diff, re.MULTILINE):
        return 'type_changed'
    return 'modified'


def _extract_field_name(diff: str) -> str:
    m = re.search(r'^[-+]\s*(?:optional|required|repeated)?\s*\w+\s+(\w+)\s*=', diff, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r'^[-+]\s*(\w+)\s*[=:]', diff, re.MULTILINE)
    if m:
        return m.group(1)
    return 'unknown'


# --- Pattern Clustering ---

def cluster_patterns(store: RagStore) -> list[StrategyCluster]:
    """Group examples by (action + target + language) into strategy clusters."""
    groups: dict[str, list[FixExample]] = defaultdict(list)

    for ex in store.all_examples():
        if ex.pattern:
            key = f'{ex.pattern.action}_{ex.pattern.target}_{ex.language}'
        else:
            key = f'{ex.change_type}_{ex.language}'
        groups[key].append(ex)

    clusters: list[StrategyCluster] = []
    for key, examples in groups.items():
        if not examples:
            continue
        # Avg confidence = avg success rate
        confidences = []
        for ex in examples:
            if ex.total_count > 0:
                confidences.append(ex.merged_count / ex.total_count)
            else:
                confidences.append(0.5)
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.5

        # Representative = highest success rate, or most recent
        representative = max(examples, key=lambda e: (
            e.merged_count / max(e.total_count, 1), e.added_at
        ))

        clusters.append(StrategyCluster(
            archetype_name=key,
            example_count=len(examples),
            avg_confidence=round(avg_conf, 3),
            representative_example=representative,
            example_ids=[e.example_id for e in examples],
        ))

    clusters.sort(key=lambda c: c.example_count, reverse=True)
    return clusters


# --- Indexers ---

def index_from_git(repo_path: str, store: RagStore, since: str = '12 months ago') -> dict:
    """Scan git log for commits with spec+consumer changes, index as fix examples."""
    stats = {'commits_scanned': 0, 'examples_stored': 0, 'languages': set()}
    repo_name = Path(repo_path).name

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

        framework = _detect_framework(files)

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
                pattern = extract_structured_pattern(spec_diff, lang, co_change_files=files)

                example = FixExample(
                    trigger_description=f'{change_type} {field_name} in {spec_f}',
                    trigger_file=spec_f,
                    trigger_diff=spec_diff[:2000],
                    fix_file=cons_f,
                    fix_diff=cons_diff[:2000],
                    language=lang,
                    change_type=change_type,
                    field_name=field_name,
                    pattern=pattern,
                    framework=framework,
                    repo_name=repo_name,
                )
                store.add_example(example)
                stats['examples_stored'] += 1
                stats['languages'].add(lang)

    stats['languages'] = sorted(stats['languages'])
    return stats


def index_single_commit(repo_path: str, commit_sha: str, store: RagStore) -> dict:
    """Index a single commit (called per webhook). Returns stats."""
    stats = {'examples_stored': 0, 'is_spec_change': False}
    repo_name = Path(repo_path).name

    try:
        files_output = subprocess.check_output(
            ['git', 'diff-tree', '--no-commit-id', '-r', '--name-only', commit_sha],
            cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return stats

    files = files_output.split('\n') if files_output else []
    spec_files = [f for f in files if Path(f).suffix.lower() in SPEC_EXTENSIONS]
    consumer_files = [f for f in files if f not in spec_files and Path(f).suffix.lower() in LANG_MAP]

    if not spec_files or not consumer_files:
        return stats

    stats['is_spec_change'] = True
    framework = _detect_framework(files)

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
            pattern = extract_structured_pattern(spec_diff, lang, co_change_files=files)

            example = FixExample(
                trigger_description=f'{change_type} {field_name} in {spec_f}',
                trigger_file=spec_f,
                trigger_diff=spec_diff[:2000],
                fix_file=cons_f,
                fix_diff=cons_diff[:2000],
                language=lang,
                change_type=change_type,
                field_name=field_name,
                pattern=pattern,
                framework=framework,
                repo_name=repo_name,
            )
            store.add_example(example)
            stats['examples_stored'] += 1

    return stats


def _as_file_list(container: dict) -> list[str]:
    """Read a file list from either the 'files' (list) or 'file' (str) shape.

    Every PropBench entry uses `files:` as a LIST. The original indexer read
    `.get('file')` -- a key that appears zero times in 881 entries -- so it
    silently produced empty paths for every record, which made language
    detection resolve to 'unknown' throughout.
    """
    files = container.get('files')
    if isinstance(files, list):
        return [f for f in files if isinstance(f, str) and f.strip()]
    if isinstance(files, str) and files.strip():
        return [files]
    single = container.get('file')
    if isinstance(single, str) and single.strip():
        return [single]
    return []


def index_from_propbench(propbench_dir: str, store: RagStore) -> dict:
    """Load PropBench YAML entries and index as fix examples.

    IMPORTANT -- what this can and cannot yield.

    PropBench is a *prediction* benchmark: each entry records that when a
    trigger file changes, certain consequence files must change too. It does
    NOT record what the fix was -- across 881 entries and 5,077 consequences,
    not one carries a diff. So while this reads the schema correctly, the
    examples it produces have no fix content and no change_type, and
    PatternStore.ingest_examples() will correctly reject them as unfixable.

    That is the honest outcome, not a bug to work around: a retrievable
    pattern that cannot produce a fix is worse than no pattern, because it can
    win retrieval and then return nothing.

    The stats therefore report `entries_without_diff` and `parse_errors` so
    that outcome is visible here, at the source, instead of only showing up as
    a filtered-to-zero count further downstream.
    """
    stats = {
        'entries_loaded': 0,
        'examples_stored': 0,
        'entries_without_diff': 0,
        'parse_errors': 0,
        'languages': set(),
    }

    try:
        import yaml
    except ImportError:
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
        except Exception as e:
            # Was a bare `continue`, so a corrupt or unreadable entry vanished
            # without trace and the total silently under-reported.
            stats['parse_errors'] += 1
            stats.setdefault('parse_error_files', []).append(f"{yf.name}: {str(e)[:80]}")
            continue

        for doc in docs:
            if not doc or not isinstance(doc, dict):
                continue
            trigger = doc.get('trigger', {}) or {}
            consequences = doc.get('consequences', []) or []
            if not consequences:
                continue

            stats['entries_loaded'] += 1

            trigger_files = _as_file_list(trigger)
            trigger_file = trigger_files[0] if trigger_files else ''
            # Real key is 'intent'; 'diff_summary' is a SUMMARY string such as
            # "Primary change: .gitignore (+1/-0)" -- deliberately not used as
            # trigger_diff, since passing a summary off as a diff would make an
            # unusable example look complete.
            trigger_desc = (
                trigger.get('intent')
                or trigger.get('description')
                or trigger.get('diff_summary')
                or (f"change in {trigger_file}" if trigger_file else "unspecified change")
            )
            trigger_diff = trigger.get('diff', '') or ''
            # No PropBench entry carries a change_type. 'modified' is left
            # deliberately unmapped in change_types.py, so these are filtered
            # rather than being handed an invented fix strategy.
            change_type = trigger.get('change_type', 'modified')
            field_name = trigger.get('field_name', '') or trigger.get('name', '') or 'unknown'
            repo_name = (
                doc.get('source_repo')
                or doc.get('repo')
                or trigger.get('package')
                or yf.parent.name
            )

            if not trigger_diff:
                stats['entries_without_diff'] += 1

            for cons in consequences:
                if not isinstance(cons, dict):
                    continue
                # One example per consequence FILE -- entries list several, and
                # reading a single scalar dropped all but nothing (the scalar
                # key never existed).
                for fix_file in _as_file_list(cons) or ['']:
                    lang = _detect_language(fix_file)

                    example = FixExample(
                        trigger_description=trigger_desc,
                        trigger_file=trigger_file,
                        trigger_diff=trigger_diff,
                        fix_file=fix_file,
                        fix_diff=cons.get('diff', '') or '',
                        language=lang,
                        change_type=change_type,
                        field_name=field_name,
                        repo_name=repo_name,
                        framework=_detect_framework([trigger_file, fix_file]),
                    )
                    store.add_example(example)
                    stats['examples_stored'] += 1
                    stats['languages'].add(lang)

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
