from __future__ import annotations
"""
ripple/app/fix_generator_multi.py

Multi-language fix generation — extends fix_generator.py with template-based
fix support for Go, Rust, Ruby, Kotlin, and C#.

Pattern: Each language implements two fix types:
  (1) Add parameter/field to struct/interface/class
  (2) Add field to API call payload/request body

Falls back to original generate_fix() for python/typescript/java.
"""

import re
from pathlib import Path
from typing import Optional

from .diff_engine import BreakingChange
from .consumer_finder import ConsumerMatch
from .fix_generator import GeneratedFix, generate_fix, _compute_diff


# Languages supported by this multi-language module
SUPPORTED_LANGUAGES = [
    "python",
    "typescript",
    "java",
    "go",
    "rust",
    "ruby",
    "kotlin",
    "csharp",
    "swift",
    "php",
    "scala",
    "dart",
]

# File extension -> language mapping
_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".swift": "swift",
    ".php": "php",
    ".scala": "scala",
    ".sc": "scala",
    ".dart": "dart",
}


def detect_language(filepath: str) -> str:
    """Determine programming language from file extension."""
    ext = Path(filepath).suffix.lower()
    return _EXTENSION_MAP.get(ext, "unknown")


def generate_fix_multi(
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    use_llm: bool = True,
) -> Optional[GeneratedFix]:
    """
    Unified fix generator that wraps the original generate_fix() and falls
    back to new language templates when the language matches Go/Rust/Ruby/Kotlin/C#.

    For python/typescript/java: delegates to original generate_fix().
    For go/rust/ruby/kotlin/csharp: uses extended templates below.
    """
    language = consumer.language or detect_language(consumer.file_path)

    # Original languages handled by fix_generator.py
    if language in ("python", "typescript", "java"):
        return generate_fix(consumer, breaking_change, use_llm=use_llm)

    # New languages handled here
    if language not in ("go", "rust", "ruby", "kotlin", "csharp", "swift", "php", "scala", "dart"):
        return None

    # Read the consumer file
    try:
        with open(consumer.file_path, "r") as f:
            original_code = f.read()
    except IOError:
        return None

    fixed_code, explanation = _generate_with_template_multi(
        original_code, consumer, breaking_change, language
    )

    if not fixed_code or fixed_code == original_code:
        return None

    diff = _compute_diff(original_code, fixed_code, consumer.file_path)

    return GeneratedFix(
        consumer=consumer,
        original_code=original_code,
        fixed_code=fixed_code,
        explanation=explanation,
        diff=diff,
    )


def _generate_with_template_multi(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    language: str,
) -> tuple[str, str]:
    """
    Template-based fix for Go, Rust, Ruby, Kotlin, and C#.
    Handles the common case: "add a required field."
    """
    if breaking_change.change_type != "added_required_field":
        return original_code, "Unsupported change type for template fix"

    field_name = breaking_change.field_name
    field_type = breaking_change.field_type

    handler = _LANGUAGE_HANDLERS.get(language)
    if handler:
        return handler(original_code, field_name, field_type)

    return original_code, f"Unsupported language: {language}"


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

def _fix_go(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Go fix: add field to struct literal + function parameter.

    Patterns:
      type CreateRequest struct { ... }  ->  add FieldName Type
      func CreateUser(name string, ...)  ->  add fieldName string
      payload := CreateRequest{ ... }    ->  add FieldName: fieldName,
    """
    fixed_code = original_code
    go_type = _go_type(field_type)
    go_field = _to_pascal_case(field_name)
    go_param = _to_camel_case(field_name)

    # (1) Add field to struct definition
    struct_pattern = re.compile(
        r'(type\s+\w+Request\s+struct\s*\{[^}]*?)(})',
        re.DOTALL,
    )
    match = struct_pattern.search(fixed_code)
    if match:
        fixed_code = struct_pattern.sub(
            rf'\1\t{go_field} {go_type} `json:"{field_name}"`\n\2',
            fixed_code,
        )

    # (2) Add parameter to function signature
    func_pattern = re.compile(
        r'(func\s+\w+\([^)]*?)(\)\s*(?:\([^)]*\)|[\w*]+|\{))',
        re.DOTALL,
    )
    match = func_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        rest = match.group(2)
        if params.strip().endswith("("):
            # No existing params
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{go_param} {go_type}{rest}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {go_param} {go_type}{rest}",
            )

    # (3) Add field to struct literal
    literal_pattern = re.compile(
        r'(\w+Request\{[^}]*?)(,?\n\s*\})',
        re.DOTALL,
    )
    match = literal_pattern.search(fixed_code)
    if match:
        fixed_code = literal_pattern.sub(
            rf'\1,\n\t\t{go_field}: {go_param}\2',
            fixed_code,
        )

    explanation = f"Added '{field_name}' to struct, function param, and struct literal"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

def _fix_rust(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Rust fix: add field to struct + builder pattern (.field_name(value)).

    Patterns:
      struct CreateRequest { ... }       ->  add pub field_name: Type,
      fn create_user(name: &str, ...)    ->  add field_name: &str
      .body(json!({ ... }))              ->  add "field_name": field_name
      RequestBuilder::new().name(...)    ->  add .field_name(field_name)
    """
    fixed_code = original_code
    rust_type = _rust_type(field_type)
    rust_field = field_name.replace("-", "_")

    # (1) Add field to struct definition
    struct_pattern = re.compile(
        r'(struct\s+\w+Request\s*\{[^}]*?)(})',
        re.DOTALL,
    )
    match = struct_pattern.search(fixed_code)
    if match:
        fixed_code = struct_pattern.sub(
            rf'\1    pub {rust_field}: {rust_type},\n\2',
            fixed_code,
        )

    # (2) Add parameter to function signature
    fn_pattern = re.compile(
        r'(fn\s+\w+\([^)]*?)(\)\s*(?:->)?)',
        re.DOTALL,
    )
    match = fn_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        rest = match.group(2)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{rust_field}: &str{rest}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {rust_field}: &str{rest}",
            )

    # (3) Add to builder pattern (.field_name(value))
    builder_pattern = re.compile(
        r'(\.build\(\))',
        re.DOTALL,
    )
    match = builder_pattern.search(fixed_code)
    if match:
        fixed_code = builder_pattern.sub(
            rf'.{rust_field}({rust_field})\n        \1',
            fixed_code,
        )
    else:
        # Try json!({}) macro pattern
        json_pattern = re.compile(
            r'(json!\(\s*\{[^}]*?)(,?\n\s*\}\s*\))',
            re.DOTALL,
        )
        match = json_pattern.search(fixed_code)
        if match:
            fixed_code = json_pattern.sub(
                rf'\1,\n            "{field_name}": {rust_field}\2',
                fixed_code,
            )

    explanation = f"Added '{field_name}' to struct, function param, and builder/payload"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------

def _fix_ruby(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Ruby fix: add key to hash literal + method parameter.

    Patterns:
      def create_user(name:, email:, ...)  ->  add field_name:
      payload = { name: name, ... }        ->  add field_name: field_name
      body: { ... }                        ->  add field_name: field_name
    """
    fixed_code = original_code
    ruby_sym = field_name.replace("-", "_")

    # (1) Add keyword argument to method definition
    def_pattern = re.compile(
        r'(def\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = def_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{ruby_sym}:{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {ruby_sym}:{match.group(2)}",
            )

    # (2) Add to hash literal (payload or body)
    hash_pattern = re.compile(
        r'((?:payload|body)\s*[:=]\s*\{[^}]*?)(,?\n\s*\})',
        re.DOTALL,
    )
    match = hash_pattern.search(fixed_code)
    if match:
        fixed_code = hash_pattern.sub(
            rf'\1,\n      {ruby_sym}: {ruby_sym}\2',
            fixed_code,
        )
    else:
        # Try inline hash { key: val, ... }
        inline_hash_pattern = re.compile(
            r'(\{[^}]*?\w+:\s*\w+)(,?\s*\})',
        )
        match = inline_hash_pattern.search(fixed_code)
        if match:
            fixed_code = inline_hash_pattern.sub(
                rf'\1, {ruby_sym}: {ruby_sym}\2',
                fixed_code,
                count=1,
            )

    explanation = f"Added '{field_name}' to method params and hash payload"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Kotlin
# ---------------------------------------------------------------------------

def _fix_kotlin(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Kotlin fix: add to data class + function parameter.

    Patterns:
      data class CreateRequest(val name: String, ...)  ->  add val fieldName: String
      fun createUser(name: String, ...)                ->  add fieldName: String
      mapOf("name" to name, ...)                       ->  add "field_name" to fieldName
    """
    fixed_code = original_code
    kt_type = _kotlin_type(field_type)
    kt_field = _to_camel_case(field_name)

    # (1) Add to data class constructor
    data_class_pattern = re.compile(
        r'(data\s+class\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = data_class_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}val {kt_field}: {kt_type}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params},\n    val {kt_field}: {kt_type}{match.group(2)}",
            )

    # (2) Add parameter to function
    fun_pattern = re.compile(
        r'(fun\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = fun_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{kt_field}: {kt_type}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {kt_field}: {kt_type}{match.group(2)}",
            )

    # (3) Add to mapOf / JSONObject payload
    map_pattern = re.compile(
        r'(mapOf\([^)]*?)(,?\n?\s*\))',
        re.DOTALL,
    )
    match = map_pattern.search(fixed_code)
    if match:
        fixed_code = map_pattern.sub(
            rf'\1,\n        "{field_name}" to {kt_field}\2',
            fixed_code,
        )
    else:
        # Try JSONObject put pattern
        put_pattern = re.compile(
            r'(\.put\("[^"]+",\s*\w+\))\s*\n',
        )
        matches = list(put_pattern.finditer(fixed_code))
        if matches:
            last_put = matches[-1]
            insert_pos = last_put.end()
            indent = "        "
            fixed_code = (
                fixed_code[:insert_pos] +
                f'{indent}.put("{field_name}", {kt_field})\n' +
                fixed_code[insert_pos:]
            )

    explanation = f"Added '{field_name}' to data class, function param, and payload"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------

def _fix_csharp(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    C# fix: add property to class + method parameter.

    Patterns:
      public class CreateRequest { ... }           ->  add public Type FieldName { get; set; }
      public async Task CreateUser(string name, ...)  ->  add string fieldName
      new { Name = name, ... }                     ->  add FieldName = fieldName
      var payload = new CreateRequest { ... }      ->  add FieldName = fieldName
    """
    fixed_code = original_code
    cs_type = _csharp_type(field_type)
    cs_property = _to_pascal_case(field_name)
    cs_param = _to_camel_case(field_name)

    # (1) Add property to class
    class_pattern = re.compile(
        r'(class\s+\w+Request\s*\{[^}]*?)(})',
        re.DOTALL,
    )
    match = class_pattern.search(fixed_code)
    if match:
        fixed_code = class_pattern.sub(
            rf'\1    public {cs_type} {cs_property} {{ get; set; }}\n\2',
            fixed_code,
        )

    # (2) Add parameter to method
    method_pattern = re.compile(
        r'((?:public|private|internal|protected)\s+(?:async\s+)?(?:Task<?[\w<>]*>?\s+|void\s+|[\w<>]+\s+)\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = method_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{cs_type.lower()} {cs_param}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {cs_type.lower()} {cs_param}{match.group(2)}",
            )

    # (3) Add to object initializer (new { ... } or new CreateRequest { ... })
    initializer_pattern = re.compile(
        r'(new\s+(?:\w+\s*)?\{[^}]*?)(,?\n\s*\})',
        re.DOTALL,
    )
    match = initializer_pattern.search(fixed_code)
    if match:
        fixed_code = initializer_pattern.sub(
            rf'\1,\n            {cs_property} = {cs_param}\2',
            fixed_code,
        )

    explanation = f"Added '{field_name}' property to class, method param, and initializer"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Type mapping helpers
# ---------------------------------------------------------------------------

def _go_type(field_type: str) -> str:
    """Map API field type to Go type."""
    mapping = {
        "string": "string",
        "integer": "int",
        "number": "float64",
        "boolean": "bool",
        "array": "[]interface{}",
        "object": "map[string]interface{}",
    }
    return mapping.get(field_type, "string")


def _rust_type(field_type: str) -> str:
    """Map API field type to Rust type."""
    mapping = {
        "string": "String",
        "integer": "i64",
        "number": "f64",
        "boolean": "bool",
        "array": "Vec<serde_json::Value>",
        "object": "serde_json::Value",
    }
    return mapping.get(field_type, "String")


def _kotlin_type(field_type: str) -> str:
    """Map API field type to Kotlin type."""
    mapping = {
        "string": "String",
        "integer": "Int",
        "number": "Double",
        "boolean": "Boolean",
        "array": "List<Any>",
        "object": "Map<String, Any>",
    }
    return mapping.get(field_type, "String")


def _csharp_type(field_type: str) -> str:
    """Map API field type to C# type."""
    mapping = {
        "string": "String",
        "integer": "int",
        "number": "double",
        "boolean": "bool",
        "array": "List<object>",
        "object": "Dictionary<string, object>",
    }
    return mapping.get(field_type, "String")


# ---------------------------------------------------------------------------
# Name casing helpers
# ---------------------------------------------------------------------------

def _to_pascal_case(name: str) -> str:
    """Convert snake_case/kebab-case to PascalCase."""
    parts = re.split(r'[-_]', name)
    return "".join(p.capitalize() for p in parts)


def _to_camel_case(name: str) -> str:
    """Convert snake_case/kebab-case to camelCase."""
    pascal = _to_pascal_case(name)
    if not pascal:
        return pascal
    return pascal[0].lower() + pascal[1:]


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------

def _fix_swift(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Swift fix: add property to struct/class + function parameter.

    Patterns:
      struct CreateRequest: Codable { ... }  ->  add let fieldName: Type
      func createUser(name: String, ...)     ->  add fieldName: String
      let payload = CreateRequest(...)       ->  add fieldName: fieldName
    """
    fixed_code = original_code
    swift_type = _swift_type(field_type)
    swift_prop = _to_camel_case(field_name)

    # (1) Add property to struct/class
    struct_pattern = re.compile(
        r'((?:struct|class)\s+\w+(?:Request)?[^{]*\{[^}]*?)(})',
        re.DOTALL,
    )
    match = struct_pattern.search(fixed_code)
    if match:
        fixed_code = struct_pattern.sub(
            rf'\1    let {swift_prop}: {swift_type}\n\2',
            fixed_code,
        )

    # (2) Add parameter to function
    func_pattern = re.compile(
        r'(func\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = func_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{swift_prop}: {swift_type}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {swift_prop}: {swift_type}{match.group(2)}",
            )

    # (3) Add to initializer call
    init_pattern = re.compile(
        r'(\w+Request\([^)]*?)(\))',
        re.DOTALL,
    )
    match = init_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{swift_prop}: {swift_prop}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {swift_prop}: {swift_prop}{match.group(2)}",
            )

    explanation = f"Added '{field_name}' to struct, function param, and initializer"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------

def _fix_php(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    PHP fix: add to associative array + function parameter.

    Patterns:
      function createUser($name, $email, ...)  ->  add $field_name
      $payload = ['name' => $name, ...]        ->  add 'field_name' => $field_name
      'body' => json_encode([...])             ->  add 'field_name' => $field_name
    """
    fixed_code = original_code
    php_var = "$" + field_name.replace("-", "_")

    # (1) Add parameter to function
    func_pattern = re.compile(
        r'(function\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = func_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{php_var}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {php_var}{match.group(2)}",
            )

    # (2) Add to associative array
    array_pattern = re.compile(
        r"(\$(?:payload|body|data)\s*=\s*\[[^\]]*?)(,?\n?\s*\])",
        re.DOTALL,
    )
    match = array_pattern.search(fixed_code)
    if match:
        fixed_code = array_pattern.sub(
            rf"\1,\n        '{field_name}' => {php_var}\2",
            fixed_code,
        )
    else:
        # Try inline array
        inline_pattern = re.compile(
            r"(\[[^\]]*?'[^']+'\s*=>\s*\$\w+)(,?\s*\])",
        )
        match = inline_pattern.search(fixed_code)
        if match:
            fixed_code = inline_pattern.sub(
                rf"\1, '{field_name}' => {php_var}\2",
                fixed_code,
                count=1,
            )

    explanation = f"Added '{field_name}' to function params and array payload"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Scala
# ---------------------------------------------------------------------------

def _fix_scala(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Scala fix: add to case class + function parameter.

    Patterns:
      case class CreateRequest(name: String, ...)  ->  add fieldName: String
      def createUser(name: String, ...)            ->  add fieldName: String
      Map("name" -> name, ...)                     ->  add "field_name" -> fieldName
    """
    fixed_code = original_code
    scala_type = _scala_type(field_type)
    scala_field = _to_camel_case(field_name)

    # (1) Add to case class
    case_class_pattern = re.compile(
        r'(case\s+class\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = case_class_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{scala_field}: {scala_type}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params},\n  {scala_field}: {scala_type}{match.group(2)}",
            )

    # (2) Add parameter to def
    def_pattern = re.compile(
        r'(def\s+\w+\([^)]*?)(\))',
        re.DOTALL,
    )
    match = def_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        if params.strip().endswith("("):
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}{scala_field}: {scala_type}{match.group(2)}",
            )
        else:
            fixed_code = fixed_code.replace(
                match.group(0),
                f"{params}, {scala_field}: {scala_type}{match.group(2)}",
            )

    # (3) Add to Map literal
    map_pattern = re.compile(
        r'(Map\([^)]*?)(,?\n?\s*\))',
        re.DOTALL,
    )
    match = map_pattern.search(fixed_code)
    if match:
        fixed_code = map_pattern.sub(
            rf'\1,\n      "{field_name}" -> {scala_field}\2',
            fixed_code,
        )

    explanation = f"Added '{field_name}' to case class, def param, and Map payload"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Dart
# ---------------------------------------------------------------------------

def _fix_dart(original_code: str, field_name: str, field_type: str) -> tuple[str, str]:
    """
    Dart fix: add field to class + constructor + toJson.

    Patterns:
      class CreateRequest { ... }           ->  add final Type fieldName;
      CreateRequest({required this.name})   ->  add required this.fieldName
      Map<String, dynamic> toJson() => {}   ->  add 'field_name': fieldName
    """
    fixed_code = original_code
    dart_type = _dart_type(field_type)
    dart_field = _to_camel_case(field_name)

    # (1) Add field to class body
    class_pattern = re.compile(
        r'(class\s+\w+(?:Request)?\s*\{)',
        re.DOTALL,
    )
    match = class_pattern.search(fixed_code)
    if match:
        insert_pos = match.end()
        fixed_code = (
            fixed_code[:insert_pos] +
            f"\n  final {dart_type} {dart_field};" +
            fixed_code[insert_pos:]
        )

    # (2) Add to constructor
    constructor_pattern = re.compile(
        r'(\w+(?:Request)?\(\{[^}]*?)(\})',
        re.DOTALL,
    )
    match = constructor_pattern.search(fixed_code)
    if match:
        params = match.group(1)
        fixed_code = fixed_code.replace(
            match.group(0),
            f"{params}, required this.{dart_field}{match.group(2)}",
        )

    # (3) Add to toJson map
    tojson_pattern = re.compile(
        r"(toJson\(\)\s*=>\s*\{[^}]*?)(,?\n?\s*\})",
        re.DOTALL,
    )
    match = tojson_pattern.search(fixed_code)
    if match:
        fixed_code = tojson_pattern.sub(
            rf"\1,\n      '{field_name}': {dart_field}\2",
            fixed_code,
        )

    explanation = f"Added '{field_name}' to class field, constructor, and toJson"
    return fixed_code, explanation


# ---------------------------------------------------------------------------
# Type mapping helpers (new languages)
# ---------------------------------------------------------------------------

def _swift_type(field_type: str) -> str:
    mapping = {
        "string": "String", "integer": "Int", "number": "Double",
        "boolean": "Bool", "array": "[Any]", "object": "[String: Any]",
    }
    return mapping.get(field_type, "String")


def _scala_type(field_type: str) -> str:
    mapping = {
        "string": "String", "integer": "Int", "number": "Double",
        "boolean": "Boolean", "array": "List[Any]", "object": "Map[String, Any]",
    }
    return mapping.get(field_type, "String")


def _dart_type(field_type: str) -> str:
    mapping = {
        "string": "String", "integer": "int", "number": "double",
        "boolean": "bool", "array": "List<dynamic>", "object": "Map<String, dynamic>",
    }
    return mapping.get(field_type, "String")


# ---------------------------------------------------------------------------
# Language handler registry
# ---------------------------------------------------------------------------

_LANGUAGE_HANDLERS: dict[str, callable] = {
    "go": _fix_go,
    "rust": _fix_rust,
    "ruby": _fix_ruby,
    "kotlin": _fix_kotlin,
    "csharp": _fix_csharp,
    "swift": _fix_swift,
    "php": _fix_php,
    "scala": _fix_scala,
    "dart": _fix_dart,
}
