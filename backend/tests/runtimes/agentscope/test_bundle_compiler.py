"""AgentScope Bundle adapter boundary tests."""

import inspect

from packages.runtimes.agentscope import AgentScopeBundleCompiler


def test_bundle_compiler_facade_has_stable_identity_without_agentscope_types() -> None:
    compiler = AgentScopeBundleCompiler()

    assert compiler.name == "AgentScopeBundleCompiler"
    assert compiler.version == "1.0.0"
    source = inspect.getsource(type(compiler))
    assert "agentscope.agent" not in source
    assert "agentscope.model" not in source
