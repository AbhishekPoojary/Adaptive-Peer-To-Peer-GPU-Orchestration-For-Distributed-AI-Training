#!/usr/bin/env bash
# Enforces the anti-fabrication rules in CONTRIBUTING.md.
#
# Fails (exit 1) if it finds, in non-test source:
#   1. random.uniform|random.gauss|random.randint|np.random|numpy.random
#      — unless the line carries a `# allow-random: <reason>` annotation.
#   2. class/def names matching Fake|Stub|Mock outside tests/.
#   3. time.sleep under trainer/ (training must never sleep-simulate work).
#
# Runs under Git Bash on Windows and in CI (ubuntu-latest).

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

fail=0
fail_msgs=()

# Directories subject to the random-data ban.
random_scan_dirs=(orchestrator agent trainer/checkpoint)

# --- Rule 1: fabricated randomness ---
random_pattern='random\.uniform|random\.gauss|random\.randint|np\.random|numpy\.random'

for dir in "${random_scan_dirs[@]}"; do
    [ -d "$dir" ] || continue
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        file="${line%%:*}"
        rest="${line#*:}"
        lineno="${rest%%:*}"
        content="${rest#*:}"

        case "$content" in
            *"# allow-random:"*)
                continue
                ;;
        esac

        fail=1
        fail_msgs+=("FAKE DATA: $file:$lineno uses banned random API without a '# allow-random: <reason>' annotation: ${content# }")
    done < <(grep -rnE "$random_pattern" --include='*.py' "$dir" 2>/dev/null)
done

# --- Rule 2: Fake/Stub/Mock class/def names outside tests/ ---
name_pattern='^\s*(class|def)\s+(Fake|Stub|Mock)[A-Za-z0-9_]*'

while IFS= read -r line; do
    [ -z "$line" ] && continue
    file="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    content="${rest#*:}"

    case "$file" in
        tests/*|*/tests/*)
            continue
            ;;
    esac

    fail=1
    fail_msgs+=("FAKE/STUB/MOCK OUTSIDE tests/: $file:$lineno defines a Fake*/Stub*/Mock* symbol: ${content# }")
done < <(grep -rnE "$name_pattern" --include='*.py' orchestrator agent trainer bench scripts 2>/dev/null)

# --- Rule 3: time.sleep under trainer/ ---
while IFS= read -r line; do
    [ -z "$line" ] && continue
    file="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    content="${rest#*:}"

    fail=1
    fail_msgs+=("SLEEP-SIMULATED WORK: $file:$lineno uses time.sleep under trainer/: ${content# }")
done < <(grep -rnE 'time\.sleep' --include='*.py' trainer 2>/dev/null)

if [ "$fail" -ne 0 ]; then
    echo "check_no_fake_data: FAILED" >&2
    echo "" >&2
    for msg in "${fail_msgs[@]}"; do
        echo "  - $msg" >&2
    done
    echo "" >&2
    echo "See CONTRIBUTING.md for the anti-fabrication rules this enforces." >&2
    exit 1
fi

echo "check_no_fake_data: OK (no banned random/, Fake*/Stub*/Mock* outside tests/, or trainer/ time.sleep found)"
exit 0
