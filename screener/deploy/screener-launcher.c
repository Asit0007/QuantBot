/* A launcher whose only job is to have an identity.
 *
 * Same rationale as JobPipe's deploy/jobpipe-launcher.c (that repo's
 * CLAUDE.md §7.56 has the long version, verified 2026-09-10): macOS guards
 * ~/Documents with TCC and a plain LaunchAgent has no grant there -- launchd
 * cannot list this repo, read .env, or even exec run-daily.sh (exit 126),
 * while the identical script from Terminal is fine. A grant has to attach
 * to a code identity, so it attaches to this bundle, built by
 * build-launcher.sh and granted once in System Settings > Privacy &
 * Security > Full Disk Access. Children inherit the attribution, which is
 * how the venv python underneath ends up able to read the repo.
 *
 * Compiled rather than a shell script with a shebang -- the kernel would
 * exec /bin/bash for a script, and the grant would land on bash again,
 * which is the whole thing being avoided.
 *
 * Build with deploy/build-launcher.sh. SCRIPT_PATH is baked in at compile
 * time so the bundle carries no argument parsing and no config of its own.
 */
#include <stdlib.h>
#include <unistd.h>

#ifndef SCRIPT_PATH
#error "compile with -DSCRIPT_PATH=\"...\" -- see deploy/build-launcher.sh"
#endif

int main(void) {
    execl("/bin/bash", "/bin/bash", SCRIPT_PATH, (char *)NULL);
    return 127;  /* only reached if execl failed */
}
