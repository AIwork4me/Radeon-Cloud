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


if __name__ == "__main__":
    unittest.main()
