"""Standalone distributions use explicitly configured model APIs."""
REASONING_LEVEL = "max"  # cache compatibility; API profiles keep their own reasoning identity
class ClaudeError(RuntimeError): pass
class CodexError(RuntimeError): pass
class CodexQuotaError(CodexError): pass
def ask(*args, **kwargs):
    raise RuntimeError("Standalone Library Sync requires an API profile; CLI adapters are not bundled.")
