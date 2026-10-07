"""Regression tests; integration tests use only temporary local directories."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import dsync


SCRIPT = Path(dsync.__file__).resolve()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsync test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source ' $(touch INJECTED)"
        self.source.mkdir()
        self.dest = self.root / 'destination space'
        self.working = self.root / "work ' space"
        self.config = self.root / 'custom config.conf'
        self.config.write_text('[testremote]\ntype = alias\nremote = {}\n'.format(self.dest))

    def cli(self, *options, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), str(self.dest),
             '-n', '3', '--working-dir', str(self.working), *map(str, options)],
            capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fixture(self):
        files = ['plain', '.hidden', '#hash', ';semicolon', ' leading and trailing ',
                 "quote'$(touch INJECTED)", 'nested/deep/file', 'nested/.hidden']
        for index, name in enumerate(files):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('content {}\n'.format(index))
        (self.source / 'empty').mkdir()
        for name in ['.zfs', '.snapshot-old', 'nested/.snapshot']:
            folder = self.source / name
            folder.mkdir()
            (folder / 'excluded').write_text('excluded')
        return files

    def assert_copied(self, files):
        for name in files:
            self.assertEqual((self.dest / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((self.dest / '.zfs').exists())
        self.assertFalse((self.dest / '.snapshot-old').exists())
        self.assertFalse((self.dest / 'nested/.snapshot').exists())
        self.assertFalse((self.source / 'INJECTED').exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_rsync_basic_preserves_hidden_special_names_empty_dirs_and_symlinks(self):
        files = self.fixture()
        (self.source / 'line\nbreak').write_text('newline')
        files.append('line\nbreak')
        (self.source / 'link').symlink_to('plain')
        self.cli('--no-fpart')
        self.assert_copied(files)
        self.assertTrue((self.dest / 'empty').is_dir())
        self.assertEqual(os.readlink(self.dest / 'link'), 'plain')

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('fpart'), 'rsync and fpart required')
    def test_rsync_fpart_preserves_files_empty_dirs_and_symlinks(self):
        files = self.fixture()
        (self.source / 'line\nbreak').write_text('newline')
        files.append('line\nbreak')
        (self.source / 'link').symlink_to('plain')
        self.cli()
        self.assert_copied(files)
        self.assertTrue((self.dest / 'empty').is_dir())
        self.assertEqual(os.readlink(self.dest / 'link'), 'plain')

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_rclone_basic_recursive_and_raw_names(self):
        files = self.fixture()
        self.cli('--no-fpart', '--cloud', '--rclone-config', self.config)
        self.assert_copied(files)

    @unittest.skipUnless(shutil.which('rclone') and shutil.which('fpart'), 'rclone and fpart required')
    def test_rclone_fpart(self):
        files = self.fixture()
        self.cli('--cloud', '--rclone-config', self.config)
        self.assert_copied(files)

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_rclone_honors_config_and_preserves_existing_testfile(self):
        (self.source / 'file').write_text('data')
        self.dest.mkdir()
        (self.dest / 'testfile.dsync').write_text('keep me')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), 'testremote:', '-n', '2',
             '--working-dir', str(self.working), '--no-fpart', '--cloud',
             '--rclone-config', str(self.config)], capture_output=True, text=True, timeout=30,
            cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'file').read_text(), 'data')
        self.assertEqual((self.dest / 'testfile.dsync').read_text(), 'keep me')
        self.assertFalse((self.root / 'testremote:').exists())

    def test_dry_runs_do_not_create_destination(self):
        (self.source / 'file').write_text('data')
        for tool in ['rsync', 'rclone']:
            if not shutil.which(tool):
                continue
            with self.subTest(tool=tool):
                options = ['--no-fpart', '--dry-run']
                if tool == 'rclone':
                    options += ['--cloud', '--rclone-config', self.config]
                self.cli(*options)
                self.assertFalse(self.dest.exists())
                self.assertTrue((self.working / 'logs').is_dir())

    def test_nonpositive_counts_and_missing_arguments_fail(self):
        for count in ['0', '-1']:
            self.cli('--no-fpart', '-n', count, success=False)
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_source_and_invalid_host_files_fail(self):
        for content in ['', '# only a comment\n', '-oProxyCommand=evil', 'host;touch bad']:
            hosts = self.root / 'hosts'
            hosts.write_text(content)
            self.cli('--no-fpart', '--source-hosts', hosts, success=False)
        self.source.rmdir()
        self.cli('--no-fpart', success=False)

    def test_host_comments_and_whitespace(self):
        hosts = self.root / 'hosts'
        hosts.write_text('  # comment\n\n host-one \nuser@host.two\n')
        self.assertEqual(dsync.read_hosts(str(hosts)), ['host-one', 'user@host.two'])

    def test_overlap_rejected_before_creating_work(self):
        self.cli('--no-fpart', '--working-dir', self.source / 'work', success=False)
        self.assertFalse((self.source / 'work').exists())
        self.dest = self.source / 'dest'
        self.cli('--no-fpart', success=False)
        self.assertFalse(self.dest.exists())

    def test_work_directory_cannot_contain_source_or_destination(self):
        (self.source / 'keep').write_text('data')
        self.cli('--no-fpart', '--working-dir', self.root, success=False)
        self.assertEqual((self.source / 'keep').read_text(), 'data')
        self.dest = self.working / 'chunks'
        self.cli('--no-fpart', success=False)
        self.assertFalse(self.working.exists())

    def test_logs_cannot_be_deleted_by_chunk_replacement(self):
        self.cli('--no-fpart', '--log-output', self.working / 'chunks' / 'logs', success=False)
        self.assertFalse(self.working.exists())

    def test_remote_dest_does_not_create_local_directory(self):
        hosts = self.root / 'hosts'
        hosts.write_text('destination-host\n')
        (self.source / 'file').write_text('data')
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '1', '--no-fpart',
                                      '--working-dir', str(self.working), '--destination-hosts', str(hosts)])
        with mock.patch.object(dsync, 'run_transfers') as transfer, mock.patch.object(dsync, 'executable', side_effect=lambda name: name):
            dsync.run(args)
        transfer.assert_called_once()
        self.assertFalse(self.dest.exists())

    def test_basic_chunking_avoids_empty_jobs(self):
        (self.source / '.hidden').write_text('data')
        directory = self.root / 'parts'
        directory.mkdir()
        chunks = dsync.basic_chunks(directory, self.source, 100, False)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read_bytes(), b'./.hidden\0')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_empty_source_has_no_jobs_and_is_reusable(self):
        self.cli('--no-fpart')
        self.cli('--no-fpart', '--reuse')
        self.assertFalse(self.dest.exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_reuse_validates_source_mode_and_chunk_integrity(self):
        (self.source / 'file').write_text('data')
        self.cli('--no-fpart')
        self.cli('--no-fpart', '--reuse', '-n', '1')
        self.cli('--reuse', success=False)  # Different chunk mode.
        original = self.source
        self.source = self.root / 'other-source'
        self.source.mkdir()
        self.cli('--no-fpart', '--reuse', success=False)
        self.source = original
        chunk = next((self.working / 'chunks').iterdir())
        chunk.write_bytes(b'tampered\0')
        self.cli('--no-fpart', '--reuse', success=False)

    def test_reuse_without_manifest_fails(self):
        self.cli('--no-fpart', '--reuse', success=False)

    def test_lock_rejects_competing_run(self):
        with dsync.working_lock(self.working):
            self.cli('--no-fpart', success=False)

    def test_cloud_rejects_newlines_and_retains_previous_manifest(self):
        args = dsync.parse_arguments([str(self.source), 'remote:', '-n', '2', '--cloud', '--no-fpart'])
        self.working.mkdir()
        logs = self.working / 'logs'
        logs.mkdir()
        (self.source / 'valid').write_text('data')
        previous = dsync.prepare_chunks(args, self.source, self.working, logs)
        manifest = (self.working / 'manifest.json').read_bytes()
        (self.source / 'bad\nname').write_text('data')
        with self.assertRaises(dsync.SyncError):
            dsync.prepare_chunks(args, self.source, self.working, logs)
        self.assertEqual((self.working / 'manifest.json').read_bytes(), manifest)
        self.assertTrue(previous[0].exists())

    def test_ssh_command_quotes_and_streams_chunk_on_stdin(self):
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '2', '--no-fpart'])
        with mock.patch.object(dsync, 'executable', return_value='/usr/bin/ssh'):
            command = dsync.transfer_command(args, '/custom/bin/rsync', self.source,
                                             str(self.dest), 'user@worker', 'storage')
        self.assertEqual(command[:5], ['/usr/bin/ssh', '-o', 'BatchMode=yes', '--', 'user@worker'])
        remote = shlex.split(command[5])
        self.assertEqual(remote[0], 'rsync')
        self.assertIn('--files-from=-', remote)
        self.assertEqual(remote[-2], str(self.source) + '/')
        self.assertEqual(remote[-1], 'storage:' + str(self.dest))

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('rclone'), 'transfer tools required')
    def test_source_host_execution_with_local_ssh_shim(self):
        files = self.fixture()
        hosts = self.root / 'hosts'
        hosts.write_text('test-worker\n')
        self.fake_tool('ssh', '''
            import subprocess, sys
            sys.exit(subprocess.call(['/bin/sh', '-c', sys.argv[-1]]))
        ''')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            for cloud in [False, True]:
                with self.subTest(cloud=cloud):
                    options = ['--no-fpart', '--source-hosts', hosts]
                    if cloud:
                        options += ['--cloud', '--rclone-config', self.config]
                    self.cli(*options)
                    self.assert_copied(files)
                    shutil.rmtree(self.dest)

    def fake_tool(self, name, code):
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        tool = bindir / name
        tool.write_text('#!{}\n'.format(sys.executable) + textwrap.dedent(code))
        tool.chmod(0o755)
        return tool

    def test_transfer_and_partition_failures_are_nonzero(self):
        self.fake_tool('rsync', 'import sys\nsys.exit(23)\n')
        self.fake_tool('fpart', 'import sys\nsys.exit(4)\n')
        (self.source / 'file').write_text('data')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            result = self.cli('--no-fpart', success=False)
            self.assertIn('exit 23', result.stderr)
            result = self.cli(success=False)
            self.assertIn('fpart failed', result.stderr)

    def test_fpart_diagnostic_with_zero_exit_is_a_failure(self):
        self.fake_tool('fpart', "import sys\nprint('./unreadable: Permission denied', file=sys.stderr)\n")
        self.fake_tool('rsync', "raise AssertionError('transfer should not start')\n")
        (self.source / 'file').write_text('data')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            result = self.cli(success=False)
        self.assertIn('fpart reported a diagnostic', result.stderr)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.working / 'manifest.json').exists())

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('fpart') and os.geteuid() != 0,
                         'rsync, fpart and a non-root user required')
    def test_fpart_unreadable_directory_cannot_silently_succeed(self):
        unreadable = self.source / 'unreadable'
        unreadable.mkdir()
        (unreadable / 'file').write_text('data')
        unreadable.chmod(0)
        try:
            self.cli(success=False)
            self.assertFalse(self.dest.exists())
        finally:
            unreadable.chmod(0o700)

    def test_bounded_concurrency_waits_for_completion(self):
        events = self.root / 'events'
        tool = self.fake_tool('rsync', '''
            import os, sys, time
            from pathlib import Path
            event = Path(os.environ['DSYNC_TEST_EVENTS'])
            with event.open('a') as log:
                log.write('start %s\\n' % os.getpid())
            sys.stdin.buffer.read()
            time.sleep(.15)
            with event.open('a') as log:
                log.write('end %s\\n' % os.getpid())
        ''')
        chunks = []
        for index in range(5):
            chunk = self.root / 'chunk.{}'.format(index)
            chunk.write_bytes(b'file\0')
            chunks.append(chunk)
        logs = self.root / 'logs'
        logs.mkdir()
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '2', '--no-fpart'])
        with mock.patch.dict(os.environ, {'DSYNC_TEST_EVENTS': str(events)}):
            dsync.run_transfers(args, chunks, str(tool), self.source, str(self.dest), [], [], logs)
        active = peak = started = 0
        for line in events.read_text().splitlines():
            if line.startswith('start'):
                active += 1
                started += 1
            else:
                active -= 1
            peak = max(peak, active)
        self.assertEqual(started, 5)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    def test_interrupt_stops_transfer_process(self):
        pidfile = self.root / 'pid'
        self.fake_tool('rsync', '''
            import os, time
            from pathlib import Path
            Path(os.environ['DSYNC_TEST_PID']).write_text(str(os.getpid()))
            time.sleep(30)
        ''')
        (self.source / 'file').write_text('data')
        env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                   DSYNC_TEST_PID=str(pidfile))
        process = subprocess.Popen([sys.executable, str(SCRIPT), str(self.source), str(self.dest),
                                    '-n', '1', '--no-fpart', '--working-dir', str(self.working)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists())
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, out + err)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
