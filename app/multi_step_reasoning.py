"""
Multi-Step Reasoning module for complex fix generation.

Instead of naive 'find reference, replace it', this module handles indirect
dependencies by tracing call chains through wrappers, interceptors, middleware,
and decorators to identify the correct fix target.
"""

from dataclasses import dataclass, field
from typing import Optional
import re


@dataclass
class CallStep:
    """One step in a call chain from consumer to the breaking reference."""
    file: str
    function: str
    calls_into: Optional[str] = None
    wrapper: bool = False


@dataclass
class CallChain:
    """Full call chain from entry point to the actual breaking reference."""
    steps: list[CallStep] = field(default_factory=list)
    final_target: Optional[str] = None  # file:line that actually needs changing


@dataclass
class WrapperInfo:
    """Describes a wrapper/interceptor pattern wrapping a field access."""
    wrapper_file: str
    wrapper_function: str
    wraps_field: str
    pattern_type: str  # interceptor | middleware | decorator | proxy


@dataclass
class FixTarget:
    """The resolved location where the fix should be applied."""
    file: str
    function: str
    line_hint: Optional[int] = None
    reason: str = ""


# Language-specific wrapper/interceptor patterns
WRAPPER_PATTERNS = {
    "go": [
        # gRPC interceptor
        (r"func\s+(\w+)\(.*grpc\.(Unary|Stream)(Server|Client)Interceptor", "interceptor"),
        # Middleware func pattern
        (r"func\s+(\w+)\(.*http\.Handler\).*http\.Handler", "middleware"),
        # Wrapper returning same type
        (r"func\s+(\w+)\(.*\)\s+func\(", "proxy"),
    ],
    "python": [
        # Decorator definition
        (r"def\s+(\w+)\(.*\):\s*\n\s+def\s+wrapper", "decorator"),
        (r"def\s+(\w+)\(.*func.*\):", "decorator"),
        # Middleware class
        (r"class\s+(\w+).*Middleware", "middleware"),
        (r"def\s+process_(request|response|view)\(self", "middleware"),
    ],
    "javascript": [
        # Express/Koa middleware
        (r"(?:const|function)\s+(\w+)\s*=?\s*(?:async\s*)?\(.*req.*res.*next", "middleware"),
        (r"app\.use\(\s*(\w+)", "middleware"),
        # Proxy/interceptor
        (r"new\s+Proxy\(", "proxy"),
        (r"(\w+)\.interceptors\.(request|response)\.use", "interceptor"),
    ],
    "typescript": [
        # Express/Koa middleware
        (r"(?:const|function)\s+(\w+)\s*=?\s*(?:async\s*)?\(.*req.*res.*next", "middleware"),
        (r"app\.use\(\s*(\w+)", "middleware"),
        # NestJS interceptor/guard
        (r"@Injectable\(\)\s*\n\s*export\s+class\s+(\w+).*Interceptor", "interceptor"),
        (r"intercept\(.*ExecutionContext.*CallHandler", "interceptor"),
    ],
    "java": [
        # Spring aspect
        (r"@(Around|Before|After|Aspect)", "interceptor"),
        (r"public\s+\w+\s+(\w+)\(.*ProceedingJoinPoint", "interceptor"),
        # Servlet filter
        (r"class\s+(\w+)\s+implements\s+Filter", "middleware"),
        (r"void\s+doFilter\(", "middleware"),
        # Spring interceptor
        (r"class\s+(\w+)\s+implements\s+HandlerInterceptor", "interceptor"),
    ],
    "ruby": [
        # around_action / before_action
        (r"(around_action|before_action|after_action)\s+:(\w+)", "middleware"),
        # Rack middleware
        (r"class\s+(\w+)\s*\n.*def\s+call\(env\)", "middleware"),
    ],
    "kotlin": [
        # Spring aspect
        (r"@(Around|Before|After|Aspect)", "interceptor"),
        (r"fun\s+(\w+)\(.*ProceedingJoinPoint", "interceptor"),
        # OkHttp interceptor
        (r"class\s+(\w+)\s*:\s*Interceptor", "interceptor"),
    ],
}


def build_call_chain(consumer_file: str, breaking_change: dict, repo_path: str) -> CallChain:
    """
    Build the call chain from a consumer file to the breaking reference.

    Args:
        consumer_file: Path to the file that references the breaking change
        breaking_change: Dict with 'field', 'type', 'file' describing what broke
        repo_path: Root path of the repository

    Returns:
        CallChain with steps from entry to the actual usage point
    """
    chain = CallChain()
    field_name = breaking_change.get("field", "")
    language = _detect_language(consumer_file)

    try:
        with open(f"{repo_path}/{consumer_file}", "r") as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        chain.steps.append(CallStep(file=consumer_file, function="<unknown>"))
        chain.final_target = f"{consumer_file}:1"
        return chain

    # Find direct references to the breaking field
    lines = content.split("\n")
    reference_lines = []
    for i, line in enumerate(lines, 1):
        if field_name and field_name in line:
            reference_lines.append((i, line.strip()))

    # Detect if accesses go through wrappers
    wrappers = detect_wrappers(content, field_name, language)

    if wrappers:
        # Build chain through wrapper
        for wrapper in wrappers:
            chain.steps.append(CallStep(
                file=wrapper.wrapper_file,
                function=wrapper.wrapper_function,
                calls_into=field_name,
                wrapper=True,
            ))
        # Final target is the wrapper, not the direct caller
        primary_wrapper = wrappers[0]
        chain.final_target = f"{primary_wrapper.wrapper_file}:{_find_function_line(content, primary_wrapper.wrapper_function)}"
    elif reference_lines:
        # Direct reference -- find the containing function
        first_ref_line = reference_lines[0][0]
        containing_func = _find_containing_function(lines, first_ref_line, language)
        chain.steps.append(CallStep(
            file=consumer_file,
            function=containing_func or "<top-level>",
            calls_into=field_name,
            wrapper=False,
        ))
        chain.final_target = f"{consumer_file}:{first_ref_line}"
    else:
        chain.steps.append(CallStep(file=consumer_file, function="<unknown>"))
        chain.final_target = f"{consumer_file}:1"

    return chain


def detect_wrappers(file_content: str, field_name: str, language: str) -> list[WrapperInfo]:
    """
    Identify if the field is accessed through a wrapper/interceptor/middleware/decorator.

    Args:
        file_content: Source code content
        field_name: The field/symbol being accessed
        language: Programming language of the file

    Returns:
        List of WrapperInfo describing any wrapper patterns found
    """
    wrappers = []
    patterns = WRAPPER_PATTERNS.get(language, [])

    for pattern_regex, pattern_type in patterns:
        matches = re.finditer(pattern_regex, file_content, re.MULTILINE)
        for match in matches:
            # Check if this wrapper/interceptor touches the field
            func_name = match.group(1) if match.lastindex else "<anonymous>"
            # Look in the surrounding context for the field reference
            start = max(0, match.start() - 50)
            end = min(len(file_content), match.end() + 500)
            context = file_content[start:end]

            if field_name and field_name in context:
                wrappers.append(WrapperInfo(
                    wrapper_file="<current>",
                    wrapper_function=func_name,
                    wraps_field=field_name,
                    pattern_type=pattern_type,
                ))

    return wrappers


def resolve_fix_target(call_chain: CallChain) -> FixTarget:
    """
    Determine where the fix should actually be applied.

    Logic:
    - If the last step is a wrapper, fix the wrapper (not the caller)
    - If there's an interceptor in the chain, fix the interceptor
    - If direct access, fix the caller

    Args:
        call_chain: The traced call chain

    Returns:
        FixTarget indicating where to apply the fix
    """
    if not call_chain.steps:
        return FixTarget(
            file="<unknown>",
            function="<unknown>",
            reason="Empty call chain -- could not trace dependency",
        )

    # Check for interceptors first (highest priority)
    for step in call_chain.steps:
        if step.wrapper:
            line_hint = None
            if call_chain.final_target and ":" in call_chain.final_target:
                try:
                    line_hint = int(call_chain.final_target.split(":")[1])
                except ValueError:
                    pass
            return FixTarget(
                file=step.file,
                function=step.function,
                line_hint=line_hint,
                reason=f"Field flows through {step.function} (wrapper/interceptor) -- fix here, not downstream callers",
            )

    # Direct access -- fix the caller
    last_step = call_chain.steps[-1]
    line_hint = None
    if call_chain.final_target and ":" in call_chain.final_target:
        try:
            line_hint = int(call_chain.final_target.split(":")[1])
        except ValueError:
            pass

    return FixTarget(
        file=last_step.file,
        function=last_step.function,
        line_hint=line_hint,
        reason=f"Direct access in {last_step.function} -- no wrapper layer, fix the caller directly",
    )


def format_reasoning(call_chain: CallChain, fix_target: FixTarget) -> str:
    """
    Format a human-readable markdown explanation of the reasoning chain.

    Shows the flow: breaking change -> intermediate steps -> fix location.
    """
    lines = ["**Fix Reasoning Chain:**\n"]

    if not call_chain.steps:
        lines.append("- ⚠️ Could not trace call chain\n")
        lines.append(f"- **Fix target:** `{fix_target.file}` in `{fix_target.function}`")
        lines.append(f"- **Reason:** {fix_target.reason}")
        return "\n".join(lines)

    # Build the flow
    for i, step in enumerate(call_chain.steps):
        prefix = "→" if i > 0 else "•"
        wrapper_tag = " **[WRAPPER]**" if step.wrapper else ""
        calls = f" → calls `{step.calls_into}`" if step.calls_into else ""
        lines.append(f"{prefix} `{step.file}` :: `{step.function}`{calls}{wrapper_tag}")

    lines.append("")
    lines.append(f"**Fix target:** `{fix_target.file}` :: `{fix_target.function}`")
    if fix_target.line_hint:
        lines.append(f"**Line:** ~{fix_target.line_hint}")
    lines.append(f"**Reason:** {fix_target.reason}")

    return "\n".join(lines)


# --- Private helpers ---

def _detect_language(filepath: str) -> str:
    """Detect language from file extension."""
    ext_map = {
        ".go": "go",
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".rb": "ruby",
        ".kt": "kotlin",
        ".kts": "kotlin",
    }
    for ext, lang in ext_map.items():
        if filepath.endswith(ext):
            return lang
    return "unknown"


def _find_containing_function(lines: list[str], target_line: int, language: str) -> Optional[str]:
    """Find the function containing a given line number."""
    func_patterns = {
        "go": r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\(",
        "python": r"def\s+(\w+)\(",
        "javascript": r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()",
        "typescript": r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()",
        "java": r"(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(",
        "ruby": r"def\s+(\w+)",
        "kotlin": r"fun\s+(\w+)\(",
    }
    pattern = func_patterns.get(language)
    if not pattern:
        return None

    # Search backwards from target line
    for i in range(target_line - 1, -1, -1):
        match = re.search(pattern, lines[i])
        if match:
            # Return first non-None group
            return next((g for g in match.groups() if g), None)
    return None


def _find_function_line(content: str, func_name: str) -> int:
    """Find the line number of a function definition."""
    for i, line in enumerate(content.split("\n"), 1):
        if func_name in line and ("def " in line or "func " in line or "function " in line):
            return i
    return 1
