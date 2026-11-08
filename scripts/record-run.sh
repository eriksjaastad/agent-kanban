#!/bin/sh
# Run a scheduled job and record the run (#8847): record-run.sh <label> -- <command...>
# The job always runs, in launchd's working directory, and its exit code is ours.
# Recording is best effort: a pt or database failure goes to stderr (the job's log).
pt="${PT_BIN:-$(dirname "$0")/../pt}"  # PT_BIN: tests point it at a scratch setup
if [ "$2" != -- ] || [ $# -lt 3 ]; then
  echo "usage: record-run.sh <label> -- <command...>" >&2
  exit 2
fi
label="$1"
shift 2
# Each pt call gets 60 s, so a stalled pt delays the job by at most that. The
# whole process group is killed: pt's own child (uv's python) would otherwise
# keep the output pipe open and the wait would go on.
bounded() {
  perl -e '$t = shift; $pid = fork; if (!$pid) { setpgrp; exec @ARGV or exit 127 }
    $SIG{ALRM} = sub { kill "KILL", -$pid; exit 124 }; alarm $t; waitpid $pid, 0; exit($? & 127 ? 128 + ($? & 127) : $? >> 8)' 60 "$@"
}
run_id="$(bounded "$pt" record-run start "$label")" || echo "record-run: could not record the start of $label" >&2
"$@"
code=$?
if [ -n "$run_id" ]; then
  bounded "$pt" record-run finish "$run_id" "$code" || echo "record-run: could not record the end of $label" >&2
fi
exit "$code"
