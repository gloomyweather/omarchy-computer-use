/*
 * hypr-pointer — tiny virtual-pointer driver for Hyprland (no root, no uinput).
 *
 * Usage: hypr-pointer CMD [CMD ...]
 *   move X Y          warp cursor to logical layout coords (Hyprland IPC movecursor)
 *   down BTN | up BTN press / release button (left|right|middle|back|forward|<linux code>)
 *   click BTN [N]     click N times (default 1)
 *   scroll DY [DX]    wheel notches; positive DY = down, positive DX = right
 *   sleep MS
 * All commands run over one Wayland connection / one virtual device, so drags work.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct wl_seat *seat;
static struct zwlr_virtual_pointer_manager_v1 *mgr;
static volatile sig_atomic_t stop;
static int held[16], nheld; /* buttons currently pressed by us: released on any exit path */

static void on_signal(int s) { (void)s; stop = 1; }
static void hold(int b) { for (int i = 0; i < nheld; i++) if (held[i] == b) return; if (nheld < 16) held[nheld++] = b; }
static void unhold(int b) { for (int i = 0; i < nheld; i++) if (held[i] == b) { held[i] = held[--nheld]; return; } }

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
    (void)d;
    if (!strcmp(iface, wl_seat_interface.name) && !seat)
        seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
    else if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name))
        mgr = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, ver < 2 ? ver : 2);
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

static uint32_t now_ms(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}
static void msleep(long ms) { struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L }; nanosleep(&ts, NULL); }

static int button_code(const char *s) {
    if (!strcasecmp(s, "left")) return 0x110;
    if (!strcasecmp(s, "right")) return 0x111;
    if (!strcasecmp(s, "middle")) return 0x112;
    if (!strcasecmp(s, "back") || !strcasecmp(s, "side")) return 0x113;
    if (!strcasecmp(s, "forward") || !strcasecmp(s, "extra")) return 0x114;
    char *e; long v = strtol(s, &e, 0);
    if (*e || v <= 0) { fprintf(stderr, "bad button: %s\n", s); return -1; }
    return (int)v;
}

static int hypr_ipc(const char *cmd, int quiet) {
    const char *sig = getenv("HYPRLAND_INSTANCE_SIGNATURE");
    const char *rt = getenv("XDG_RUNTIME_DIR");
    if (!sig || !rt) { fprintf(stderr, "not in a Hyprland session\n"); return -1; }
    struct sockaddr_un a = { .sun_family = AF_UNIX };
    snprintf(a.sun_path, sizeof a.sun_path, "%s/hypr/%s/.socket.sock", rt, sig);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&a, sizeof a) < 0) { perror("hyprland ipc"); if (fd >= 0) close(fd); return -1; }
    if (write(fd, cmd, strlen(cmd)) < 0) { perror("write"); close(fd); return -1; }
    char buf[256]; ssize_t n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n > 0) { buf[n] = 0; if (strncmp(buf, "ok", 2)) { if (!quiet) fprintf(stderr, "hyprland: %s\n", buf); return -1; } }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s move X Y | down B | up B | click B [N] | scroll DY [DX] | sleep MS ...\n", argv[0]); return 2; }
    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) { fprintf(stderr, "cannot connect to wayland display\n"); return 1; }
    struct wl_registry *reg = wl_display_get_registry(dpy);
    wl_registry_add_listener(reg, &reg_listener, NULL);
    wl_display_roundtrip(dpy);
    if (!mgr) { fprintf(stderr, "compositor lacks zwlr_virtual_pointer_manager_v1\n"); return 1; }
    struct sigaction sa = { .sa_handler = on_signal };
    sigaction(SIGTERM, &sa, NULL); sigaction(SIGINT, &sa, NULL); sigaction(SIGHUP, &sa, NULL);
    struct zwlr_virtual_pointer_v1 *p = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(mgr, seat);
    int rc = 0;
    /* a protocol error (or a dead compositor connection) must fail the command, not report success */
#define RT() do { if (wl_display_roundtrip(dpy) < 0) { \
        fprintf(stderr, "wayland error: %s\n", strerror(wl_display_get_error(dpy))); rc = 1; goto out; } } while (0)
    RT();

    for (int i = 1; i < argc; i++) {
        const char *c = argv[i];
        if (stop) { fprintf(stderr, "interrupted\n"); rc = 1; goto out; }
#define NEED(n) do { if (i + (n) >= argc) { fprintf(stderr, "%s: missing args\n", c); rc = 2; goto out; } } while (0)
        if (!strcmp(c, "move")) {
            NEED(2);
            int x = atoi(argv[i + 1]), y = atoi(argv[i + 2]);
            char cmd[160];
            /* Hyprland >= 0.55 (lua config) first, then legacy hyprlang dispatcher */
            snprintf(cmd, sizeof cmd, "/dispatch hl.dsp.cursor.move({ x = %d, y = %d })", x, y);
            if (hypr_ipc(cmd, 1)) {
                snprintf(cmd, sizeof cmd, "/dispatch movecursor %d %d", x, y);
                if (hypr_ipc(cmd, 0)) { rc = 1; goto out; }
            }
            /* zero-length motion so pointer focus/hover is recomputed for our seat device */
            zwlr_virtual_pointer_v1_motion(p, now_ms(), wl_fixed_from_int(0), wl_fixed_from_int(0));
            zwlr_virtual_pointer_v1_frame(p);
            RT();
            i += 2;
        } else if (!strcmp(c, "down") || !strcmp(c, "up")) {
            NEED(1);
            int b = button_code(argv[i + 1]);
            if (b < 0) { rc = 2; goto out; }
            zwlr_virtual_pointer_v1_button(p, now_ms(), b, c[0] == 'd' ? 1 : 0);
            zwlr_virtual_pointer_v1_frame(p);
            if (c[0] == 'd') hold(b); else unhold(b);
            RT();
            i += 1;
        } else if (!strcmp(c, "click")) {
            NEED(1);
            int b = button_code(argv[i + 1]); int n = 1;
            if (b < 0) { rc = 2; goto out; }
            if (i + 2 < argc) { char *e; long v = strtol(argv[i + 2], &e, 10); if (!*e && v > 0) { n = (int)v; i++; } }
            for (int k = 0; k < n; k++) {
                if (stop) { rc = 1; goto out; }
                zwlr_virtual_pointer_v1_button(p, now_ms(), b, 1); zwlr_virtual_pointer_v1_frame(p); hold(b); RT();
                msleep(15);
                zwlr_virtual_pointer_v1_button(p, now_ms(), b, 0); zwlr_virtual_pointer_v1_frame(p); unhold(b); RT();
                if (k + 1 < n) msleep(60);
            }
            i += 1;
        } else if (!strcmp(c, "scroll")) {
            NEED(1);
            int dy = atoi(argv[i + 1]), dx = 0; i++;
            if (i + 1 < argc) { char *e; long v = strtol(argv[i + 1], &e, 10); if (!*e) { dx = (int)v; i++; } }
            int axes[2] = { dy, dx };
            for (int a = 0; a < 2; a++) {
                int n = axes[a]; if (!n) continue;
                int step = n > 0 ? 1 : -1;
                for (int k = 0; k != n; k += step) {
                    if (stop) { rc = 1; goto out; }
                    zwlr_virtual_pointer_v1_axis_source(p, 0 /* wheel */);
                    zwlr_virtual_pointer_v1_axis_discrete(p, now_ms(), a, wl_fixed_from_int(15 * step), step);
                    zwlr_virtual_pointer_v1_frame(p);
                    RT();
                    msleep(20);
                }
            }
        } else if (!strcmp(c, "axis")) { /* axis AXIS VALUE DISCRETE  (low level, DISCRETE=0 -> continuous) */
            NEED(3);
            int ax = atoi(argv[i + 1]); double v = atof(argv[i + 2]); int d = atoi(argv[i + 3]);
            zwlr_virtual_pointer_v1_axis_source(p, d ? 0 : 1);
            if (d) zwlr_virtual_pointer_v1_axis_discrete(p, now_ms(), ax, wl_fixed_from_double(v), d);
            else zwlr_virtual_pointer_v1_axis(p, now_ms(), ax, wl_fixed_from_double(v));
            zwlr_virtual_pointer_v1_frame(p);
            RT();
            i += 3;
        } else if (!strcmp(c, "sleep")) {
            NEED(1); msleep(atol(argv[i + 1])); i += 1;
        } else {
            fprintf(stderr, "unknown command: %s\n", c); rc = 2; goto out;
        }
    }
out:
    /* on failure / signal never leave a button stuck down; on success a trailing `down` is intentional
     * (mouse_button down ... up in separate calls) and stays pressed */
    for (int k = 0; (rc || stop) && k < nheld; k++) {
        zwlr_virtual_pointer_v1_button(p, now_ms(), held[k], 0);
        zwlr_virtual_pointer_v1_frame(p);
    }
    zwlr_virtual_pointer_v1_destroy(p);
    wl_display_roundtrip(dpy);
    wl_display_disconnect(dpy);
    return rc;
}
