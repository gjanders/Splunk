#!/usr/bin/env bash

#
# Cleanup old search peer directories
#
# Logic:
#   1. Look only at top-level directories under:
#        /opt/splunk/var/run/searchpeers/
#   2. If the directory itself is newer than 30 days, skip it.
#   3. If directory contains alive_tokens/ and any file inside
#      alive_tokens/ is newer than 30 days, skip deletion.
#   4. Otherwise delete the top-level directory.
#

set -o errexit
set -o nounset
set -o pipefail

SEARCHPEERS_DIR="/opt/splunk/var/run/searchpeers"
AGE_DAYS=30

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

log "Starting searchpeers cleanup"

# Only process top-level directories
find "$SEARCHPEERS_DIR" -mindepth 1 -maxdepth 1 -mtime +"${AGE_DAYS}" | while read -r dir; do

    dir_name=$(basename "$dir")

    alive_tokens_dir="${dir}/alive_tokens"

    if [[ -d "$alive_tokens_dir" ]]; then
        log "INFO: Found alive_tokens directory: $alive_tokens_dir"

        # Any file newer than AGE_DAYS?
        if find "$alive_tokens_dir" -mtime -"${AGE_DAYS}" | grep -q .; then
            log "SKIP: $dir_name - alive_tokens contains file newer than ${AGE_DAYS} days"
            continue
        fi

        log "INFO: No recent files found in alive_tokens"
    else
        log "INFO: No alive_tokens directory present"
    fi

    log "DELETE: Removing $dir"
    rm -rf -- "$dir"

    log "DELETE: Successfully removed $dir_name"

done

log "Cleanup complete"
