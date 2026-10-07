#!/usr/bin/env python3
"""Partition a local filesystem and copy it with bounded parallel transfers."""

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class SyncError(Exception):
    """An actionable transfer or configuration error."""


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Partition a local source and copy its contents with parallel rsync/rclone processes.')
    parser.add_argument('source', help='Local source directory (contents are copied)')
    parser.add_argument('dest', help='Destination directory or rclone remote:path with --cloud')
    parser.add_argument('-n', '--number', type=positive_int, required=True,
                        help='Number of chunks and maximum concurrent transfer processes')
    parser.add_argument('--no-fpart', action='store_true',
                        help='Use basic chunking (top-level entries for rsync; recursive files for rclone)')
    parser.add_argument('--source-hosts', help='File containing SSH hosts on which to run transfers')
    parser.add_argument('--destination-hosts', help='File containing rsync destination SSH hosts')
    parser.add_argument('--reuse', action='store_true', help='Reuse validated chunks from the same source and mode')
    parser.add_argument('--cloud', action='store_true', help='Copy using rclone')
    parser.add_argument('--dry-run', action='store_true', help='Preview transfers without writing to the destination')
    parser.add_argument('--rclone-config', default='~/.config/rclone/rclone.conf', help='Rclone config file')
    parser.add_argument('--working-dir', default='~/dsync_working/', help='Directory for chunks and run state')
    parser.add_argument('--log-output', help='Log directory (default: WORKING_DIR/logs)')
    return parser.parse_args(argv)


def executable(name):
    path = shutil.which(name)
    if path is None:
        raise SyncError('{} is not installed or not on PATH'.format(name))
    return path


def local_path(value):
    return Path(value).expanduser().resolve()


def read_hosts(filename):
    if not filename:
        return []
    hosts = []
    for line in local_path(filename).read_text().splitlines():
        host = line.strip()
        if not host or host.startswith('#'):
            continue
        # Hosts become SSH operands and rsync host:path prefixes, never shell code.
        if not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*', host):
            raise SyncError('Invalid SSH host {!r} in {}'.format(host, filename))
        hosts.append(host)
    if not hosts:
        raise SyncError('Host file is empty: {}'.format(filename))
    return hosts


def inside(path, parent):
    return path == parent or parent in path.parents


@contextmanager
def working_lock(working):
    working.mkdir(parents=True, exist_ok=True)
    with (working / '.dsync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('Another dsync run is using {}'.format(working)) from None
        yield


def excluded(name):
    return name == '.zfs' or name.startswith('.snapshot')


def basic_entries(source, cloud):
    if not cloud:
        with os.scandir(source) as entries:
            for entry in entries:
                if not excluded(entry.name):
                    # Rsync treats leading # and ; as comments, even with --from0.
                    yield b'./' + os.fsencode(entry.name)
        return

    def onerror(error):
        raise error

    # Streaming traversal avoids holding the entire tree in memory. Do not follow
    # symlink directories: rclone's default copy semantics also skip symlinks.
    for root, dirs, files in os.walk(source, onerror=onerror):
        dirs[:] = [name for name in dirs if not excluded(name)]
        for name in files:
            if not excluded(name):
                yield os.fsencode(os.path.relpath(os.path.join(root, name), source))


def cloud_entry(entry):
    if b'\n' in entry or b'\r' in entry:
        raise SyncError('Rclone chunk lists cannot represent newline/carriage-return filenames: {!r}'.format(entry))
    return entry + b'\n'


def basic_chunks(directory, source, number, cloud):
    chunks = []
    handles = []
    with ExitStack() as stack:
        for index, entry in enumerate(basic_entries(source, cloud)):
            slot = index % number
            if slot == len(handles):
                path = directory / 'chunk.{}'.format(slot)
                chunks.append(path)
                handles.append(stack.enter_context(path.open('wb')))
            handles[slot].write(cloud_entry(entry) if cloud else entry + b'\0')
    return chunks


def nul_entries(path):
    pending = b''
    with path.open('rb') as handle:
        while True:
            block = handle.read(65536)
            if not block:
                break
            entries = (pending + block).split(b'\0')
            pending = entries.pop()
            yield from entries
    if pending:
        raise SyncError('Incomplete NUL-delimited chunk: {}'.format(path))


def stop_processes(processes):
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def partition_with_fpart(directory, source, number, cloud, logs):
    command = [executable('fpart'), '-0', '-x', '.zfs', '-x', '.snapshot*',
               '-n', str(number), '-o', str(directory / 'chunk')]
    if not cloud:
        command.append('-z')  # Preserve empty directories without recursively copying chunks twice.
    command.append('.')
    with (logs / 'fpart.out').open('wb') as out, (logs / 'fpart.err').open('wb') as err:
        process = subprocess.Popen(command, cwd=source, stdout=out, stderr=err, start_new_session=True)
        try:
            status = process.wait()
        finally:
            stop_processes([process])
    if status:
        raise SyncError('fpart failed (exit {}); see {}'.format(status, logs / 'fpart.err'))
    # Fpart can return zero after filesystem traversal errors. Without verbose
    # flags its only normal stderr output is partition statistics. Fail closed
    # on other diagnostics instead of reporting an incomplete copy as success.
    with (logs / 'fpart.err').open('rb') as errors:
        for line in errors:
            if line.strip() and not re.fullmatch(rb'Part #\d+: size = \d+, files = \d+', line.strip()):
                raise SyncError('fpart reported a diagnostic; see {}'.format(logs / 'fpart.err'))
    chunks = sorted(path for path in directory.iterdir()
                    if re.fullmatch(r'chunk\.\d+', path.name) and path.stat().st_size)
    if cloud:
        for path in chunks:
            converted = path.with_suffix(path.suffix + '.tmp')
            with converted.open('wb') as out:
                for entry in nul_entries(path):
                    if entry.startswith(b'./'):
                        entry = entry[2:]
                    out.write(cloud_entry(entry))
            converted.replace(path)
    return chunks


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            result.update(block)
    return result.hexdigest()


def chunk_identity(source, args):
    stat = source.stat()
    return {'version': 1, 'source': str(source), 'device': stat.st_dev, 'inode': stat.st_ino,
            'cloud': args.cloud, 'no_fpart': args.no_fpart}


def prepare_chunks(args, source, working, logs):
    manifest_path = working / 'manifest.json'
    identity = chunk_identity(source, args)
    if args.reuse:
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest['identity'] != identity:
                raise ValueError('source or chunking mode differs')
            chunks = []
            for name, checksum in manifest['chunks'].items():
                if not re.fullmatch(r'chunk\.\d+', name):
                    raise ValueError('invalid chunk name')
                path = working / 'chunks' / name
                if digest(path) != checksum:
                    raise ValueError('chunk changed: {}'.format(name))
                chunks.append(path)
            return chunks
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise SyncError('Cannot reuse chunks: {}. Run without --reuse to regenerate.'.format(error)) from error

    # Build in isolation; a failed partition must not replace the previous good set.
    with tempfile.TemporaryDirectory(prefix='.partition-', dir=working) as temp:
        directory = Path(temp)
        if args.no_fpart:
            chunks = basic_chunks(directory, source, args.number, args.cloud)
        else:
            chunks = partition_with_fpart(directory, source, args.number, args.cloud, logs)
        manifest = {'identity': identity, 'chunks': {p.name: digest(p) for p in chunks}}
        new_manifest = directory / 'manifest.json'
        new_manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        target = working / 'chunks'
        # Invalidate before replacing so interruption cannot leave reusable stale state.
        manifest_path.unlink(missing_ok=True)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        for path in chunks:
            path.replace(target / path.name)
        new_manifest.replace(manifest_path)
        return [target / path.name for path in chunks]


def transfer_command(args, binary, source, dest, source_host, dest_host):
    command = [Path(binary).name if source_host else binary]
    if args.cloud:
        command += ['copy', '-v', '--transfers', '2', '--config', str(local_path(args.rclone_config)),
                    '--files-from-raw', '-']
    else:
        command += ['-av', '--protect-args', '--from0', '--files-from=-']
        if args.no_fpart:
            command += ['--recursive', '--exclude=.zfs', '--exclude=.snapshot*']
        if dest_host:
            dest = '{}:{}'.format(dest_host, dest)
    if args.dry_run:
        command.append('--dry-run')
    command += ['--', os.path.join(str(source), ''), dest]
    if source_host:
        command = [executable('ssh'), '-o', 'BatchMode=yes', '--', source_host,
                   ' '.join(shlex.quote(arg) for arg in command)]
    return command


def run_transfers(args, chunks, binary, source, dest, source_hosts, dest_hosts, logs):
    source_cycle = itertools.cycle(source_hosts or [None])
    dest_cycle = itertools.cycle(dest_hosts or [None])
    jobs = iter(enumerate(chunks))
    active = []
    failures = []
    exhausted = False
    tool = 'rclone' if args.cloud else 'rsync'
    try:
        while active or not exhausted:
            while len(active) < args.number and not exhausted:
                try:
                    index, chunk = next(jobs)
                except StopIteration:
                    exhausted = True
                    break
                command = transfer_command(args, binary, source, dest,
                                           next(source_cycle), next(dest_cycle))
                error_path = logs / '{}.err.{}'.format(tool, index)
                with chunk.open('rb') as files, (logs / '{}.out.{}'.format(tool, index)).open('wb') as out, error_path.open('wb') as err:
                    process = subprocess.Popen(command, stdin=files, stdout=out, stderr=err,
                                               start_new_session=True)
                active.append((process, error_path))
            pending = []
            for process, error_path in active:
                status = process.poll()
                if status is None:
                    pending.append((process, error_path))
                elif status:
                    failures.append('exit {}: {}'.format(status, error_path))
            active = pending
            if active:
                time.sleep(0.05)
    finally:
        stop_processes([process for process, _ in active])
    if failures:
        raise SyncError('{} transfer(s) failed; {}'.format(len(failures), '; '.join(failures)))


def run(args):
    source = local_path(args.source)
    working = local_path(args.working_dir)
    logs = local_path(args.log_output) if args.log_output else working / 'logs'
    if inside(logs, working / 'chunks'):
        raise SyncError('Log directory must not be inside the reserved working chunks directory')
    if not source.is_dir():
        raise SyncError('Source is not a directory: {}'.format(source))
    source_hosts = read_hosts(args.source_hosts)
    dest_hosts = read_hosts(args.destination_hosts)
    if args.cloud and dest_hosts:
        raise SyncError('--destination-hosts applies only to rsync, not --cloud')
    if any(inside(path, source) or inside(source, path) for path in (working, logs)):
        raise SyncError('Working/log directories and the source tree must not overlap')
    if args.cloud:
        # Preserve remote syntax, including a bucket root; normalize local backends.
        dest = args.dest if ':' in args.dest and not os.path.isabs(args.dest) else str(local_path(args.dest))
    else:
        if not os.path.isabs(os.path.expanduser(args.dest)) and ':' in args.dest:
            raise SyncError('Use --destination-hosts for remote rsync destinations')
        dest = str(local_path(args.dest))
    # An absolute rclone destination denotes a local backend too.
    if not dest_hosts and (not args.cloud or os.path.isabs(dest)):
        destination = local_path(dest)
        if inside(destination, source) or inside(source, destination):
            raise SyncError('Source and destination directories must not overlap')
        if any(inside(path, destination) or inside(destination, path) for path in (working, logs)):
            raise SyncError('Working/log directories and the destination tree must not overlap')
    tool = 'rclone' if args.cloud else 'rsync'
    binary = tool if source_hosts else executable(tool)
    if source_hosts or dest_hosts:
        executable('ssh')
    with working_lock(working):
        logs.mkdir(parents=True, exist_ok=True)
        chunks = prepare_chunks(args, source, working, logs)
        print('-- {} {} chunk(s), up to {} concurrent transfers'.format(
            'Reusing' if args.reuse else 'Prepared', len(chunks), args.number), flush=True)
        if args.dry_run:
            print('-- Dry run: destination will not be changed', flush=True)
        if not chunks:
            print('-- No entries to transfer')
            return
        if not args.cloud and not dest_hosts and not source_hosts and not args.dry_run:
            Path(dest).mkdir(parents=True, exist_ok=True)
        run_transfers(args, chunks, binary, source, dest, source_hosts, dest_hosts, logs)
        print('-- {} completed successfully; logs: {}'.format('Dry run' if args.dry_run else 'Transfer', logs))


def main(argv=None):
    args = parse_arguments(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        print('ERROR: Interrupted; active local transfer processes stopped', file=sys.stderr)
        return 130
    except (SyncError, OSError) as error:
        print('ERROR: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
