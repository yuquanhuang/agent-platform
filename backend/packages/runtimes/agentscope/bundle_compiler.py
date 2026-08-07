"""AgentScope Runtime-facing Bundle compiler facade."""

from collections.abc import Iterable

from packages.domain.bundles import (
    BUNDLE_COMPILER_NAME,
    BUNDLE_COMPILER_VERSION,
    BundleAgentInput,
    BundleArtifactInput,
    CompiledRuntimeBundle,
    compile_agentscope_bundle,
    verify_compiled_bundle,
)


class AgentScopeBundleCompiler:
    """Compile platform Snapshot facts without importing AgentScope objects."""

    name = BUNDLE_COMPILER_NAME
    version = BUNDLE_COMPILER_VERSION

    def compile(
        self,
        root: BundleAgentInput,
        artifacts: Iterable[BundleArtifactInput] = (),
    ) -> CompiledRuntimeBundle:
        return compile_agentscope_bundle(root, artifacts)

    def verify(self, bundle: CompiledRuntimeBundle) -> None:
        verify_compiled_bundle(bundle)
