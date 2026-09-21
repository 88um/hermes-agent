# Standalone plugin readiness — offline validation

Result: ready for a later standalone installation into HumorBank, subject to the native CLI, login, and model-entitlement prerequisites below. This is installation/interface readiness, not live model qualification.

Plugin HEAD: `c92c27c9f919178a58974a72333b473c6cb2e71d` (manifest 0.3.0). Source checkout and actual installed clone both resolve to this SHA. Source checkout remains clean.

Synced Hermes: tests began at `189fce9821065f5ce35e5153b6a3ffcf062c8713`; final HEAD `24796be53144a163e0534246a1fa9b17a4bd885d`. The intervening commit only adds `tests/tools/test_docker_session_context_forwarding.py`, unrelated to this validation.

## Real command evidence

Commands used the requested Python executable, `-m hermes_cli.main`, the synced checkout as cwd/PYTHONPATH, isolated temporary HOME and HERMES_HOME, no inherited credentials, no real Claude executable, and an external-network-blocking Python startup guard.

1. `plugins install file:///Users/joshua/hermes-claude-plugin-validation-20260921 --ref c92c27c9f919178a58974a72333b473c6cb2e71d --enable --no-deps`: exit 0, pinned clone installed and enabled.
2. `plugins list --user --json`: exit 0, provider listed enabled, version 0.3.0, source pinned@c92c27c9.
3. `plugins doctor claude-subscription-directsdk-experimental --ci`: exit 0, runtime discovery, manifest parsing, import and provider registration passed. Missing-Claude warning was expected.
4. Real `python -m tui_gateway.entry` stdio JSON-RPC `model.options` (include_unconfigured=true), exit 0:
   - Missing executable: configured provider remains visible, authenticated=false, saved model sonnet retained.
   - Offline executable fixture reporting loggedIn=false: pinned catalog exposed: Sonnet 5 1M, Haiku 4.5, Opus 5 1M, Opus 4.8 1M, Fable 5.1 1M, plus configured sonnet alias. Fixture call log contains only `auth status`; no model invocation.
   - Picker authenticated=true in the fixture case means executable availability, NOT proven login. Setup's separate login gate is covered by upstream and plugin tests.

The isolated config disabled install-time security scanning to avoid a network/LLM scan; this validation does not claim scan approval. `--no-deps` prevented package installation; plugin pyproject declares no Python dependencies beyond Hermes. The actual Claude CLI and account were neither accessed nor validated.

## Test evidence

Both suites used `scripts/run_tests.sh`, with `HERMES_PYTHON=/Users/joshua/.hermes/hermes-agent/.venv/bin/python`, isolated HOME, `-j 2 --file-timeout 90 --file-retries 0`. Runner confirmed selection of that interpreter.

Upstream: 9 files, 61 passed, 0 failed, 5.9s:
- tests/hermes_cli/test_external_process_auth_status.py
- tests/hermes_cli/test_external_process_provider_seam.py
- tests/hermes_cli/test_model_switch_external_process_alias.py
- tests/hermes_cli/test_model_flow_external_process_setup.py
- tests/agent/test_external_process_provider_init.py
- tests/tools/test_delegate_external_provider.py
- tests/tui_gateway/test_external_process_picker.py
- tests/hermes_cli/test_plugin_install_ref.py
- tests/hermes_cli/test_plugin_install_manifest_version.py

Plugin: all 6 offline test files, 15 passed, 0 failed, 3.2s. Copied plugin to temporary storage and changed only that copy's conftest HERMES_AGENT_REPO assignment to the synced checkout, because the runner strips that environment variable. Original plugin tracked files untouched. Tests use fake native processes/loopback protocol fixtures, not paid model calls.

## Later HumorBank installation prerequisites

- Use Hermes with the tested external-process provider support; plugin declares Hermes >=0.21.4 and Python >=3.10.
- Install the standalone plugin into HumorBank's own HERMES_HOME/profile plugins directory, pinned to the exact SHA above. Do not add vendor code to the core tree. A future remote install may use `hermes plugins install NousResearch/hermes-plugin-claude-subscription-directsdk --ref c92c27c9f919178a58974a72333b473c6cb2e71d --enable`; remote reachability and availability of this commit were not tested here.
- Install official Claude Code CLI; plugin documentation qualifies native version 2.1.263. This run did not validate any real native version.
- Perform `claude auth login` under the account/config directory intended for HumorBank, with suitable subscription/model access. Point CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND at a non-PATH binary if needed; use the documented dedicated config-dir setting if auth is separated.
- Remove conflicting inherited Anthropic API-key/custom-endpoint/cloud-backend overrides for the normal OAuth provider path.
- Select `claude-subscription-directsdk-experimental` via `hermes model`; restart an already-running gateway to discover a newly installed plugin. Auxiliary/fallback providers remain separately configured.
- Account entitlement, extra-usage settings, current native-version compatibility, and actual inference remain unverified. Picker entries and native list-price estimates are not proof of included subscription usage.

No active profiles changed, no real authentication/API/model calls, no tracked core/plugin modifications. An unrelated untracked docs directory appeared in the shared sync tree and was left untouched.
