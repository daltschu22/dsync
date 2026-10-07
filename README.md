# dsync

dsync partitions a local directory and copies its contents using parallel
[rsync](https://rsync.samba.org/) or [rclone](https://rclone.org/) processes.
[fpart](https://www.fpart.org/) balances chunks by size; basic chunking is also
available without fpart. Transfers can run locally or across SSH worker hosts
that share access to the source filesystem.

## Requirements

- Linux and Python 3.10 or later. No Python packages are required.
- rsync for filesystem transfers, or rclone for `--cloud` transfers.
- fpart unless using `--no-fpart` or reusing existing chunks.
- SSH with noninteractive authentication when using worker or destination hosts.

```sh
git clone https://github.com/daltschu22/dsync.git
cd dsync
python3 dsync.py --help
```

Install the external tools through your distribution, for example:

```sh
sudo apt install rsync fpart rclone
```

## Usage

Copy the **contents** of a directory with up to four concurrent processes:

```sh
python3 dsync.py /mnt/source /mnt/destination -n 4
```

Use basic chunking, including hidden files and directories:

```sh
python3 dsync.py /mnt/source /mnt/destination -n 4 --no-fpart
```

With rsync, basic chunks contain top-level entries and each transfer recurses
into its assigned directories. With rclone, basic chunking walks the source
recursively and distributes file paths across chunks. It streams the listing
to disk instead of retaining the whole tree in memory. For uneven directory
sizes, fpart generally provides better load balancing. Both modes exclude
`.zfs` and names beginning with `.snapshot` at every level.

Upload using an endpoint created with [`rclone config`](https://rclone.org/commands/rclone_config/):

```sh
python3 dsync.py /mnt/source remote:bucket/path -n 4 --cloud \
  --rclone-config ~/.config/rclone/rclone.conf
```

`--cloud` uses `rclone copy`, with two file transfers per process. Local rclone
destinations are also supported. Rsync uses archive mode. Neither backend
deletes destination files to mirror the source.

Preview a transfer with `--dry-run`. It still creates local chunks and logs,
but does not create the destination or perform cloud write/delete probes:

```sh
python3 dsync.py /mnt/source remote:bucket/path -n 4 --cloud --dry-run
```

The command waits for all transfers. Any partitioning or transfer failure
returns a nonzero exit status and identifies the relevant error log. Ctrl+C
and SIGTERM stop active local partition/transfer process groups before exiting
with status 130 and 143 respectively. Cleanup waits up to five seconds before
killing surviving group members, even if their parent has exited. Further
interruptions during cleanup do not abandon the remaining processes. Remote
worker cleanup depends on SSH and the remote tool's disconnect behavior.

## Working files, logs, and reuse

The default working directory is `~/dsync_working`. Logs default to its `logs`
subdirectory; override these with `--working-dir` and `--log-output`.
Working/log directories must not overlap the source or a local destination.
Local source and destination trees must also be disjoint.

The working directory reserves `chunks/`, `manifest.json`, and `.dsync.lock`.
Only one run can use it at a time. Use separate working **and log** directories
for independent simultaneous runs. Logs are overwritten on subsequent runs.
Chunk generation is staged so a partitioning failure preserves the previous
completed chunk set. Regeneration replaces only unchanged chunk files recorded
in a valid manifest. Unrecognized files, modified chunks, and symlinked chunk
directories cause an error and are preserved, including during dry runs. Use
a new working directory or move those files aside after reviewing them. An
interrupted replacement may also require a new working directory.

Reuse a successful partition without rescanning the source:

```sh
python3 dsync.py /mnt/source /mnt/destination -n 2 --reuse
```

Reuse verifies the source path and filesystem identity, backend, chunking
mode, and chunk checksums. Repeat `--cloud` and/or `--no-fpart` if used during
generation. `-n` still caps concurrent processes, even if more chunks exist.
Legacy chunks without a manifest must be regenerated once.

Reuse does **not** detect changes within the source tree. Regenerate chunks
when files are added or removed; in particular, rclone may silently skip
listed files that no longer exist. Do not modify a source tree during a copy
if a consistent snapshot is required.

## Multiple hosts

Host files contain one hostname or `user@hostname` per line. Blank lines and
lines beginning with `#` are ignored. Hosts are assigned round-robin.

```sh
python3 dsync.py /mnt/source /mnt/destination -n 8 \
  --source-hosts workers.txt --destination-hosts storage.txt
```

- Source workers must see the same absolute source path and have the transfer
  tool on their `PATH`. Chunk contents are sent over SSH stdin, so workers do
  not need access to the working directory.
- Without `--destination-hosts`, the destination is local to each source worker.
- With `--destination-hosts`, each destination host must expose the same
  destination storage. The option applies only to rsync.
- Rclone workers must have the configuration at the same absolute
  `--rclone-config` path. SSH workers need noninteractive access to any
  destination hosts they use.

Filesystem destinations on SSH hosts must be absolute paths; dsync passes them
unchanged so the destination host resolves any symlinks or `..` components.
Source and configuration paths sent to workers retain their symlink spelling
(relative paths and `~` are expanded on the controller). Destination overlap
checks apply to local transfers; the controller cannot validate a remote
host's filesystem layout.

Host files currently accept DNS names, IPv4 addresses, SSH aliases, and optional
usernames; IPv6 literals are not supported. Rsync remote destinations should
be supplied through `--destination-hosts`, rather than a `host:path` positional
argument.

## Filename and metadata handling

Paths with spaces, quotes, or shell metacharacters are passed safely as
arguments. Rsync chunk lists are NUL-delimited, including support for newline
filenames. Rclone uses raw file lists so leading/trailing spaces and names
starting with `#` or `;` are preserved. For compatibility with older rclone
versions, filenames containing newlines or carriage returns cause a clear
error before cloud transfers start.

Rsync preserves symlinks and empty directories. Rclone uses its standard copy
semantics: empty directories and symlinks are not uploaded. Parallel rsync
jobs can update common parent directories, so final directory modification
times are not guaranteed to match the source. This tool does not provide a
filesystem snapshot or cross-chunk hard-link preservation.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The suite covers actual rsync, rclone, and fpart transfers in temporary local
directories, plus command quoting, exit status, dry runs, chunk reuse,
concurrency limits, locking, and interruption. External-tool integration tests
are skipped when the corresponding binaries are missing. No cloud credentials
or network destinations are used by the tests.

## License

[MIT](LICENSE).
