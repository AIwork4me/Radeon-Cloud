from __future__ import annotations

import contextlib
import io
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rc  # noqa: E402


class RcRegressionTests(unittest.TestCase):
    def test_status_returns_failure_when_gpu_probe_fails_without_torch_flag(self):
        args = types.SimpleNamespace(torch=False)
        cfg = dict(rc.DEFAULTS)
        output = io.StringIO()
        with patch.object(rc, "require_remote"), patch.object(rc, "remote_capture", return_value=None), contextlib.redirect_stdout(output):
            result = rc.cmd_status(args, cfg)
        self.assertEqual(result, rc.EXIT_FAIL)
        self.assertIn("rocm-smi failed", output.getvalue())

    def test_run_returns_failure_when_job_metadata_cannot_be_written(self):
        args = types.SimpleNamespace(
            cwd=None,
            allow_ephemeral=False,
            name="metadata-failure",
            command=["echo", "hello"],
            no_auto_venv=True,
            no_env=False,
            venv=None,
            dry_run=False,
            # This test intentionally exercises the post-launch metadata failure;
            # opt into the CLI's explicit non-interactive execution gate.
            yes=True,
        )
        cfg = dict(rc.DEFAULTS)
        ssh_calls = []

        def fake_ssh_run(_cfg, command, **_kwargs):
            ssh_calls.append(command)
            if len(ssh_calls) == 1:
                return 0, "12345\n", ""
            return 1, "", "permission denied"

        output = io.StringIO()
        with patch.object(rc, "require_remote"), patch.object(rc, "remote_capture", return_value="YES"), patch.object(rc, "ssh_run", side_effect=fake_ssh_run), patch.object(rc.time, "strftime", return_value="20260902-080000"), contextlib.redirect_stdout(output):
            result = rc.cmd_run(args, cfg)
        self.assertEqual(result, rc.EXIT_FAIL)
        self.assertNotIn("job started", output.getvalue())
        self.assertIn("metadata could not be written", output.getvalue())

    def test_noninteractive_remote_execution_is_denied_by_default(self):
        cfg = dict(rc.DEFAULTS)
        with patch.object(rc.sys.stdin, "isatty", return_value=False), patch.object(rc.sys.stdout, "isatty", return_value=False):
            with self.assertRaises(SystemExit) as raised:
                rc.require_exec_consent(cfg, "echo hello", assume_yes=False)
        self.assertEqual(raised.exception.code, rc.EXIT_FAIL)

    def test_host_override_must_match_configured_alias(self):
        cfg = dict(rc.DEFAULTS)
        with patch.object(rc, "load_config", return_value=cfg), patch.object(rc, "ssh_alias_defined", return_value=True):
            with self.assertRaises(SystemExit) as raised:
                rc.main(["--host", "another-alias", "guide"])
        self.assertEqual(raised.exception.code, rc.EXIT_CONNECT)

    def test_safe_extract_supports_streaming_archives(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "out"
            destination.mkdir()
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                payload = b"streamed"
                member = tarfile.TarInfo("nested/result.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r|gz") as tar:
                rc.safe_extract(tar, destination)
            self.assertEqual((destination / "nested" / "result.txt").read_bytes(), b"streamed")

    def test_safe_extract_rejects_path_traversal_and_absolute_archive_members(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "out"
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                payload = b"owned"
                member = tarfile.TarInfo("../../escape.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            archive.seek(0)

            destination.mkdir()
            with tarfile.open(fileobj=archive, mode="r:gz") as tar:
                with self.assertRaises(ValueError):
                    rc.safe_extract(tar, destination)
            self.assertFalse((Path(temp_dir) / "escape.txt").exists())

            for unsafe_name in ("/absolute.txt", "C:/absolute.txt"):
                single = io.BytesIO()
                with tarfile.open(fileobj=single, mode="w:gz") as unsafe_tar:
                    payload = b"owned"
                    member = tarfile.TarInfo(unsafe_name)
                    member.size = len(payload)
                    unsafe_tar.addfile(member, io.BytesIO(payload))
                single.seek(0)
                with tarfile.open(fileobj=single, mode="r:gz") as unsafe_tar:
                    with self.assertRaises(ValueError):
                        rc.safe_extract(unsafe_tar, destination)


    # ------------------------------------------------------------------
    # 2026-09-04 e2e UX findings: F4 command-string path guard,
    # F1 loadavg interpretation, F2 MSYS virtual-mount rejection
    # ------------------------------------------------------------------

    def test_command_string_ephemeral_paths_are_refused(self):
        cfg = dict(rc.DEFAULTS)
        note = rc.check_command_paths("touch /tmp/rc-escape && cp x /var/tmp/y", cfg, False)
        self.assertIsNotNone(note)
        self.assertIn("--allow-ephemeral", note)
        # deep paths and bare zone roots are both caught
        self.assertIsNotNone(rc.check_command_paths("mkdir -p /dev/shm/block", cfg, False))
        self.assertIsNotNone(rc.check_command_paths("tar xf a.tgz -C /root", cfg, False))

    def test_command_string_benign_paths_pass(self):
        cfg = dict(rc.DEFAULTS)
        benign = [
            "python /workspace/train.py --out /workspace/out",
            "ls /etc; cat /usr/bin/env | head -1",
            "curl -s https://example.com/a/b -o /dev/null",
            "echo hi >/dev/null 2>/dev/null",
            "PATH=/opt/venv/bin:$PATH python -c 'import torch'",
            "timeout -s KILL 45 rocm-smi --showtemp 2>/dev/null",
        ]
        for cmd in benign:
            self.assertIsNone(rc.check_command_paths(cmd, cfg, False), cmd)

    def test_command_string_allow_ephemeral_overrides(self):
        cfg = dict(rc.DEFAULTS)
        self.assertIsNone(rc.check_command_paths("touch /tmp/ok", cfg, True))

    def test_exec_refuses_ephemeral_path_before_connecting(self):
        args = types.SimpleNamespace(
            cwd=None, allow_ephemeral=False, command=["touch", "/tmp/rc-escape"],
            no_env=False, no_auto_venv=True, venv=None, stream=False,
            dry_run=False, yes=True, timeout=None,
        )
        cfg = dict(rc.DEFAULTS)
        with patch.object(rc, "require_remote") as probe:
            with self.assertRaises(SystemExit) as raised:
                rc.cmd_exec(args, cfg)
        self.assertEqual(raised.exception.code, 1)
        probe.assert_not_called()

    def test_run_refuses_ephemeral_path_in_command(self):
        args = types.SimpleNamespace(
            cwd=None, allow_ephemeral=False, name="escape", command=["touch", "/tmp/x"],
            no_env=False, no_auto_venv=True, venv=None, dry_run=False, yes=True,
        )
        cfg = dict(rc.DEFAULTS)
        with self.assertRaises(SystemExit) as raised:
            rc.cmd_run(args, cfg)
        self.assertEqual(raised.exception.code, 1)

    def test_parse_loadavg_handles_both_formats(self):
        cores, load1, running, line = rc.parse_loadavg("128\n102.63 103.15 103.49 8/4987 911619")
        self.assertEqual(cores, 128)
        self.assertEqual(running, 8)
        self.assertAlmostEqual(load1, 102.63)
        self.assertIn("4987", line)
        cores, load1, running, _ = rc.parse_loadavg("3.2 3.0 2.8 2/300 123")
        self.assertIsNone(cores)
        self.assertAlmostEqual(load1, 3.2)
        self.assertEqual(running, 2)
        cores, load1, running, _ = rc.parse_loadavg("")
        self.assertIsNone(cores)
        self.assertIsNone(load1)
        self.assertIsNone(running)

    def test_load_verdict_flags_d_state_buildup(self):
        # the 2026-09-04 signature: loadavg 103 but only 8 running tasks
        level, detail = rc.load_verdict(128, 103.0, running=8)
        self.assertEqual(level, "warn")
        self.assertIn("blocked", detail)
        # busy but proportional: 100 running on 128 cores at load 103 is fine
        level, _ = rc.load_verdict(128, 103.0, running=100)
        self.assertEqual(level, "ok")
        # load above 2x cores warns even when running is unknown
        level, _ = rc.load_verdict(8, 30.0)
        self.assertEqual(level, "warn")
        level, _ = rc.load_verdict(8, 10.0, running=9)
        self.assertEqual(level, "ok")
        # legacy format without nproc: absolute fallback threshold
        level, _ = rc.load_verdict(None, 3.2, running=2)
        self.assertEqual(level, "ok")

    def test_msys_virtual_path_detection(self):
        with patch.object(rc.os, "name", "nt"):
            self.assertTrue(rc._msys_virtual_path("/tmp/rc-e2e-test/myproject"))
            self.assertTrue(rc._msys_virtual_path("/tmp"))
            self.assertTrue(rc._msys_virtual_path("/"))
            self.assertFalse(rc._msys_virtual_path("/c/Users/rocm/workbuddy-ai"))
            self.assertFalse(rc._msys_virtual_path("C:\\Users\\rocm"))
            self.assertFalse(rc._msys_virtual_path("relative/dir"))
            self.assertFalse(rc._msys_virtual_path("~/x"))
        with patch.object(rc.os, "name", "posix"):
            self.assertFalse(rc._msys_virtual_path("/tmp/x"))

    def test_push_rejects_msys_virtual_mount_with_guidance(self):
        args = types.SimpleNamespace(local="/tmp/proj", remote="/workspace/proj")
        cfg = dict(rc.DEFAULTS)
        with patch.object(rc.os, "name", "nt"), patch.object(rc, "require_remote") as probe:
            with self.assertRaises(SystemExit) as raised:
                rc.cmd_push(args, cfg)
        self.assertEqual(raised.exception.code, 1)
        probe.assert_not_called()

    def test_pull_rejects_msys_virtual_mount_with_guidance(self):
        args = types.SimpleNamespace(local="/tmp/out", remote="/workspace/out",
                                     overwrite=False, allow_ephemeral=False)
        cfg = dict(rc.DEFAULTS)
        with patch.object(rc.os, "name", "nt"), patch.object(rc, "require_remote") as probe:
            with self.assertRaises(SystemExit) as raised:
                rc.cmd_pull(args, cfg)
        self.assertEqual(raised.exception.code, 1)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
