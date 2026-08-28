# Browser seccomp profile provenance

`seccomp_profile.json` is derived from Microsoft Playwright's `v1.62.0`
`utils/docker/seccomp_profile.json`. The upstream unconditional allow for `clone`, `setns`, and
`unshare` was removed. The profile retains the upstream masked `clone` rule for ordinary
non-namespace process/thread creation, while namespace transitions are limited to the exact calls
observed in the reviewed Radar launcher and Chromium sandbox runtime:

- `unshare(CLONE_NEWUSER)` = `268435456` (`0x10000000`)
- `unshare(CLONE_NEWNET)` = `1073741824` (`0x40000000`)
- `unshare(CLONE_NEWUSER|CLONE_NEWNET)` = `1342177280` (`0x50000000`)
- `clone(CLONE_NEWUSER|SIGCHLD)` = `268435473` (`0x10000011`)
- `clone(CLONE_NEWPID|SIGCHLD)` = `536870929` (`0x20000011`)
- `clone(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET|SIGCHLD)` = `1879048209`
  (`0x70000011`)

Every listed argument is matched at index 0 with `SCMP_CMP_EQ`; other namespace combinations and
unprivileged `setns` remain denied by the fail-closed default action. `pidfd_open` is present for
util-linux 2.38.1's `unshare --fork --kill-child=SIGKILL` supervision path.

An unconditional seccomp allow for the `chroot` syscall is required by Chromium after it enters its
child user namespace. This is not an outer-container privilege grant: the backend runs as non-root
UID/GID 10001 with `cap_drop: ALL`, so the kernel's capability check still denies `chroot` outside
the scoped Chromium user namespace.

Source: `https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json`

Reviewed Stage 2 profile SHA-256:
`22d9e4aef7f8ce6c01c6034f267797a7a717182e7820da5ef8c64d32c307ef20`.

Recalculate and record this hash after every intentional profile edit. Production must retain
AppArmor, non-root UID/GID 10001, `cap_drop: ALL`, `no-new-privileges`, and a read-only rootfs. Never
replace this profile with `seccomp=unconfined`.
