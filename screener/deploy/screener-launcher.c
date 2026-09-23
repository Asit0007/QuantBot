/* A launcher whose only job is to have an identity.
 *
 * Same shape as JobPipe's deploy/jobpipe-launcher.c: macOS's Desktop/
 * Documents/Downloads TCC protection can only be granted to a real code
 * identity (a binary or app bundle), not a bare interpreter, so a raw
 * `/bin/bash run-daily.sh` LaunchAgent would have any grant land on bash
 * itself -- shared by every background shell this Mac runs, not scoped to
 * this job. Compiling gives this its own identity to hang a grant on if one
 * is ever needed.
 *
 * JobPipe's CLAUDE.md §7.56 describes hitting exit 126 (couldn't even exec
 * the script) without a manual System Settings > Privacy & Security > Full
 * Disk Access grant, on 2026-09-10. **This launcher did NOT need that same
 * step, verified 2026-09-17** -- see build-launcher.sh's header for the
 * live TCC.db evidence. Keep the compiled-binary pattern regardless (it's
 * the correct shape either way), but don't assume the manual grant is a
 * prerequisite before trying `launchctl kickstart`.
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
