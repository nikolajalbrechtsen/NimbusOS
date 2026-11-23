#!/bin/bash
# =============================================================================
# tools/import-signing-key.sh: load the APT signing key in a workflow job
# =============================================================================
# What: imports the ASCII-armored secret key in $APT_SIGNING_KEY into
#       $GNUPGHOME (created with mode 0700), then checks, failing with a
#       plain message:
#         - there is exactly one secret key, and it can sign;
#         - $APT_SIGNING_PASSPHRASE unlocks it (a test signature);
#         - its fingerprint is one of the public keys committed in
#           NIMBUS_ARCHIVE_KEYS_DIR, so installed systems trust what it
#           signs (docs/PLAN.md NOTE-44).
#       Prints the fingerprint on stdout.
# Why:  release.yml and refresh.yml sign with this key; a wrong or
#       mismatched secret must stop the job before anything is published.
#       The key exists only in the GitHub secret (spec ground rule 6,
#       docs/SIGNING.md).
#
# Usage: GNUPGHOME=DIR APT_SIGNING_KEY=… APT_SIGNING_PASSPHRASE=… tools/import-signing-key.sh
#        Needs gpg and gpg-agent.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"
nimbus_load_conf

[[ -n "${GNUPGHOME:-}" ]] || die "Set GNUPGHOME to an empty directory for the key (for example \$RUNNER_TEMP/gnupg)."
[[ -n "${APT_SIGNING_KEY:-}" ]] || die "The secret APT_SIGNING_KEY is empty. Add it as described in docs/SIGNING.md (repository settings, Secrets and variables, Actions)."
install -d -m 0700 -- "$GNUPGHOME"
printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import 2>/dev/null \
	|| die "APT_SIGNING_KEY is not an OpenPGP secret key (export it with: gpg --armor --export-secret-keys FPR)."

mapfile -t fprs < <(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "sec" { want = 1; next } $1 == "fpr" && want { print $10; want = 0 }')
((${#fprs[@]} == 1)) || die "APT_SIGNING_KEY holds ${#fprs[@]} secret keys; it must hold exactly one."
fpr="${fprs[0]}"

nimbus_mktemp_dir tmp
printf 'NimbusOS signing test\n' >"$tmp/test"
printf '%s' "${APT_SIGNING_PASSPHRASE:-}" | gpg --batch --pinentry-mode loopback --passphrase-fd 0 \
	--local-user "$fpr" --detach-sign --output "$tmp/test.sig" "$tmp/test" 2>"$tmp/err" \
	|| die "The key $fpr can't sign with APT_SIGNING_PASSPHRASE: $(tr '\n' ' ' <"$tmp/err")"

mapfile -t committed < <(nimbus_archive_fingerprints)
((${#committed[@]})) || die "No public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/ yet. Commit the public half of the signing key first (docs/SIGNING.md)."
for c in "${committed[@]}"; do
	if [[ "$c" == "$fpr" ]]; then
		log "Signing key $fpr is loaded and matches $NIMBUS_ARCHIVE_KEYS_DIR/."
		printf '%s\n' "$fpr"
		exit 0
	fi
done
die "The signing key $fpr is not one of the committed public keys (${committed[*]}).
Installed systems would reject what it signs. Commit its public half to $NIMBUS_ARCHIVE_KEYS_DIR/,
release that keyring package first, or put the right key in APT_SIGNING_KEY (docs/SIGNING.md)."
