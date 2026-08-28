# Browser seccomp profile provenance

`seccomp_profile.json` is derived from Microsoft Playwright's `v1.62.0`
`utils/docker/seccomp_profile.json`. The upstream unconditional allow for `clone`, `setns`, and
`unshare` was removed. Three argument-scoped `unshare` rules permit only `CLONE_NEWUSER`,
`CLONE_NEWNET`, or their exact combination for the Radar browser launcher. Other namespace types
remain denied by the fail-closed default action. `pidfd_open` is added for util-linux 2.38.1's
`unshare --fork --kill-child=SIGKILL` supervision path.

Source: `https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json`

Reviewed profile SHA-256 before the Stage 2 implementation commit:
`71b63be3c8ca04cd81561a777e2e20c8b466dc587af73542b7f454c8e13172ed`.

Recalculate and record this hash after every intentional profile edit. Production must retain
AppArmor, non-root UID/GID 10001, `cap_drop: ALL`, `no-new-privileges`, and a read-only rootfs. Never
replace this profile with `seccomp=unconfined`.
