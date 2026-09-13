from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.discovery import (
    DiscoveryError,
    ProcessRecord,
    diagnostic_payload,
    select_trusted_runtime,
)


class DiscoveryTests(unittest.TestCase):
    def _runtime_fixture(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = (
            Path(temp_dir.name)
            / "WindowsApps"
            / "OpenAI.Codex_26.825.6671.0_x64__2p2nqsd0c76g0"
            / "app"
        )
        runtime = root / "resources" / "codex-runtime"
        runtime.mkdir(parents=True)
        chatgpt = root / "ChatGPT.exe"
        codex = runtime / "codex.exe"
        node = runtime / "node.exe"
        server = runtime / "app-tools" / "server.mjs"
        server.parent.mkdir()
        for path in (chatgpt, codex, node, server):
            path.write_text("fixture", encoding="utf-8")
        environment = {
            "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\codex-app-tools-200",
            "CODEX_BUNDLED_NODE_PATH": str(node),
            "CODEX_APP_TOOLS_SERVER_PATH": str(server),
        }
        records = [
            ProcessRecord(100, 1, str(chatgpt), "ChatGPT.exe", alive=True),
            ProcessRecord(200, 100, str(codex), "codex.exe --app-tools", environment, alive=True),
            ProcessRecord(
                300,
                1,
                str(Path(temp_dir.name) / "global" / "codex.exe"),
                "codex.exe",
                environment,
                alive=True,
            ),
        ]
        return temp_dir, records, environment, (chatgpt, codex, node, server)

    def test_selects_verified_packaged_descendant_over_global_codex(self):
        temp_dir, records, _, paths = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)

        snapshot = select_trusted_runtime(records)

        self.assertEqual(snapshot.chatgpt_pid, 100)
        self.assertEqual(snapshot.codex_pid, 200)
        self.assertEqual(Path(snapshot.codex_executable), paths[1])
        self.assertEqual(Path(snapshot.node_executable), paths[2])
        self.assertEqual(Path(snapshot.bridge_server), paths[3])
        self.assertEqual(snapshot.pipe_path, r"\\.\pipe\codex-app-tools-200")

    def test_global_codex_is_not_a_fallback_when_no_descendant_exists(self):
        temp_dir, records, _, _ = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)
        records = [records[0], records[2]]

        with self.assertRaises(DiscoveryError):
            select_trusted_runtime(records)

    def test_stale_chatgpt_or_codex_process_is_rejected(self):
        temp_dir, records, _, _ = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)
        records[0] = ProcessRecord(
            records[0].pid,
            records[0].parent_pid,
            records[0].executable_path,
            records[0].command_line,
            records[0].environment,
            alive=False,
        )

        with self.assertRaises(DiscoveryError):
            select_trusted_runtime(records)

    def test_untrusted_named_pipe_is_rejected(self):
        temp_dir, records, _, _ = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)
        records[1] = ProcessRecord(
            records[1].pid,
            records[1].parent_pid,
            records[1].executable_path,
            records[1].command_line,
            {**records[1].environment, "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\unrelated-200"},
            alive=True,
        )

        with self.assertRaises(DiscoveryError):
            select_trusted_runtime(records)

    def test_resources_outside_verified_package_are_rejected(self):
        temp_dir, records, _, paths = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)
        outside = Path(temp_dir.name) / "outside-node.exe"
        outside.write_text("fixture", encoding="utf-8")
        records[1] = ProcessRecord(
            records[1].pid,
            records[1].parent_pid,
            records[1].executable_path,
            records[1].command_line,
            {**records[1].environment, "CODEX_BUNDLED_NODE_PATH": str(outside)},
            alive=True,
        )

        with self.assertRaises(DiscoveryError):
            select_trusted_runtime(records)

    def test_chatgpt_outside_a_packaged_windowsapps_root_is_rejected(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name) / "Untrusted" / "ChatGPT"
        runtime = root / "resources" / "codex-runtime"
        runtime.mkdir(parents=True)
        chatgpt = root / "ChatGPT.exe"
        codex = runtime / "codex.exe"
        node = runtime / "node.exe"
        server = runtime / "app-tools" / "server.mjs"
        server.parent.mkdir()
        for path in (chatgpt, codex, node, server):
            path.write_text("fixture", encoding="utf-8")
        environment = {
            "CODEX_APP_TOOLS_PIPE_PATH": r"\\.\pipe\codex-app-tools-untrusted",
            "CODEX_BUNDLED_NODE_PATH": str(node),
            "CODEX_APP_TOOLS_SERVER_PATH": str(server),
        }
        records = [
            ProcessRecord(400, 1, str(chatgpt), "ChatGPT.exe"),
            ProcessRecord(401, 400, str(codex), "codex.exe", environment),
        ]

        with self.assertRaises(DiscoveryError):
            select_trusted_runtime(records)

    def test_diagnostic_payload_contains_only_sanitized_runtime_facts(self):
        temp_dir, records, _, _ = self._runtime_fixture()
        self.addCleanup(temp_dir.cleanup)

        payload = diagnostic_payload(select_trusted_runtime(records), version="0.151.0-alpha")

        self.assertEqual(payload["codex_version"], "0.151.0-alpha")
        self.assertEqual(payload["codex_pid"], 200)
        self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", json.dumps(payload))
        self.assertNotIn("Authorization", json.dumps(payload))
        self.assertTrue(payload["pipe_path"].startswith("\\\\.\\pipe\\"))

    def test_accepts_current_app_managed_codex_layout_and_nested_quoted_configuration(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        package = base / "WindowsApps" / "OpenAI.Codex_26.825.6671.0_x64__2p2nqsd0c76g0" / "app"
        local_app_data = base / "LocalAppData"
        codex_root = local_app_data / "OpenAI" / "Codex"
        codex_bin = codex_root / "bin" / "bundled"
        node_dir = codex_root / "runtimes" / "cua_node" / "bundled" / "bin"
        tools_dir = package / "resources" / "plugins" / "openai-bundled" / "plugins" / "codex-app-tools"
        package.mkdir(parents=True)
        codex_bin.mkdir(parents=True)
        node_dir.mkdir(parents=True)
        tools_dir.mkdir(parents=True)
        chatgpt = package / "ChatGPT.exe"
        codex = codex_bin / "codex.exe"
        node = node_dir / "node.exe"
        server = tools_dir / "server.mjs"
        for path in (chatgpt, codex, node, server):
            path.write_text("fixture", encoding="utf-8")
        pipe = r"\\.\pipe\codex-browser-use-08813e0e-4c7d-4839-bea7-74b2fb06e398"
        pipe_config = pipe.replace("\\", "\\\\")
        node_config = str(node).replace("\\", "\\\\")
        tools_config = str(tools_dir).replace("\\", "\\\\")
        command_line = (
            f'codex.exe app-server -c "mcp_servers.codex_app={{"cwd"="{tools_config}",'
            f'"env"={{"CODEX_APP_TOOLS_PIPE_PATH"="{pipe_config}","CODEX_MCP_NODE_PATH"="{node_config}"}}}}"'
        )
        records = [
            ProcessRecord(100, 1, str(chatgpt), "ChatGPT.exe"),
            ProcessRecord(200, 100, str(codex), command_line),
        ]

        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False):
            snapshot = select_trusted_runtime(records)

        self.assertEqual(snapshot.codex_pid, 200)
        self.assertEqual(snapshot.node_executable, os.path.normcase(str(node.resolve())))
        self.assertEqual(snapshot.bridge_server, os.path.normcase(str(server.resolve())))
        self.assertEqual(snapshot.pipe_path, pipe)


if __name__ == "__main__":
    unittest.main()
