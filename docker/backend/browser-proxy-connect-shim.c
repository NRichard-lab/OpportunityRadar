#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define RADAR_PROXY_PORT 17654
#define RADAR_SHIM_VERSION "network_namespace_dns_pinned_proxy_v1"

typedef int (*connect_function)(int, const struct sockaddr *, socklen_t);
typedef int (*socket_function)(int, int, int);
typedef int (*getname_function)(int, struct sockaddr *, socklen_t *);

static pthread_once_t resolver_once = PTHREAD_ONCE_INIT;
static connect_function real_connect_function;
static socket_function real_socket_function;
static getname_function real_getpeername_function;
static getname_function real_getsockname_function;

static void resolve_functions(void) {
    *(void **)(&real_connect_function) = dlsym(RTLD_NEXT, "connect");
    *(void **)(&real_socket_function) = dlsym(RTLD_NEXT, "socket");
    *(void **)(&real_getpeername_function) = dlsym(RTLD_NEXT, "getpeername");
    *(void **)(&real_getsockname_function) = dlsym(RTLD_NEXT, "getsockname");
}

static int functions_ready(void) {
    if (pthread_once(&resolver_once, resolve_functions) != 0) {
        errno = EACCES;
        return 0;
    }
    if (real_connect_function == NULL || real_socket_function == NULL ||
        real_getpeername_function == NULL || real_getsockname_function == NULL) {
        errno = EACCES;
        return 0;
    }
    return 1;
}

static int requested_proxy(const struct sockaddr *address, socklen_t length) {
    if (address == NULL || address->sa_family != AF_INET ||
        length < (socklen_t)sizeof(struct sockaddr_in)) {
        return 0;
    }
    const struct sockaddr_in *internet = (const struct sockaddr_in *)address;
    return internet->sin_port == htons(RADAR_PROXY_PORT) &&
           internet->sin_addr.s_addr == htonl(INADDR_LOOPBACK);
}

static int proxy_socket_path(struct sockaddr_un *unix_address, socklen_t *length) {
    const char *path = getenv("OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET");
    if (path == NULL || path[0] != '/') {
        errno = EACCES;
        return 0;
    }
    size_t path_length = strlen(path);
    if (path_length == 0 || path_length >= sizeof(unix_address->sun_path)) {
        errno = ENAMETOOLONG;
        return 0;
    }
    memset(unix_address, 0, sizeof(*unix_address));
    unix_address->sun_family = AF_UNIX;
    memcpy(unix_address->sun_path, path, path_length + 1);
    *length = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + path_length + 1);
    return 1;
}

static int is_proxy_unix_socket(int descriptor) {
    if (!functions_ready()) {
        return 0;
    }
    struct sockaddr_un peer;
    socklen_t peer_length = sizeof(peer);
    memset(&peer, 0, sizeof(peer));
    if (real_getpeername_function(descriptor, (struct sockaddr *)&peer, &peer_length) != 0 ||
        peer.sun_family != AF_UNIX) {
        return 0;
    }
    struct sockaddr_un expected;
    socklen_t expected_length;
    if (!proxy_socket_path(&expected, &expected_length)) {
        return 0;
    }
    (void)expected_length;
    return strcmp(peer.sun_path, expected.sun_path) == 0;
}

static int replace_with_proxy_socket(int descriptor) {
    if (!functions_ready()) {
        return -1;
    }
    int socket_type = 0;
    socklen_t type_length = sizeof(socket_type);
    if (getsockopt(descriptor, SOL_SOCKET, SO_TYPE, &socket_type, &type_length) != 0 ||
        (socket_type & SOCK_STREAM) != SOCK_STREAM) {
        errno = EPROTOTYPE;
        return -1;
    }
    int status_flags = fcntl(descriptor, F_GETFL);
    int descriptor_flags = fcntl(descriptor, F_GETFD);
    if (status_flags < 0 || descriptor_flags < 0) {
        return -1;
    }
    int creation_flags = SOCK_STREAM;
    if ((status_flags & O_NONBLOCK) != 0) {
        creation_flags |= SOCK_NONBLOCK;
    }
    if ((descriptor_flags & FD_CLOEXEC) != 0) {
        creation_flags |= SOCK_CLOEXEC;
    }
    int replacement = real_socket_function(AF_UNIX, creation_flags, 0);
    if (replacement < 0) {
        return -1;
    }
    struct sockaddr_un unix_address;
    socklen_t unix_length;
    if (!proxy_socket_path(&unix_address, &unix_length)) {
        int saved_error = errno;
        close(replacement);
        errno = saved_error;
        return -1;
    }
    int result = real_connect_function(
        replacement,
        (const struct sockaddr *)&unix_address,
        unix_length
    );
    int connect_error = errno;
    if (result != 0 && connect_error != EINPROGRESS) {
        close(replacement);
        errno = connect_error;
        return -1;
    }
    if (dup3(replacement, descriptor, (descriptor_flags & FD_CLOEXEC) != 0 ? O_CLOEXEC : 0) < 0) {
        int saved_error = errno;
        close(replacement);
        errno = saved_error;
        return -1;
    }
    close(replacement);
    errno = connect_error;
    return result;
}

__attribute__((visibility("default")))
const char *opportunity_radar_browser_connect_shim_version(void) {
    return RADAR_SHIM_VERSION;
}

__attribute__((visibility("default")))
int connect(int descriptor, const struct sockaddr *address, socklen_t length) {
    if (!functions_ready()) {
        return -1;
    }
    if (requested_proxy(address, length)) {
        return replace_with_proxy_socket(descriptor);
    }
    if (address != NULL && (address->sa_family == AF_INET || address->sa_family == AF_INET6)) {
        errno = EACCES;
        return -1;
    }
    return real_connect_function(descriptor, address, length);
}

static int synthetic_name(struct sockaddr *address, socklen_t *length, uint16_t port) {
    if (address == NULL || length == NULL || *length < (socklen_t)sizeof(struct sockaddr_in)) {
        errno = EINVAL;
        return -1;
    }
    struct sockaddr_in internet;
    memset(&internet, 0, sizeof(internet));
    internet.sin_family = AF_INET;
    internet.sin_port = htons(port);
    internet.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    memcpy(address, &internet, sizeof(internet));
    *length = sizeof(internet);
    return 0;
}

__attribute__((visibility("default")))
int getpeername(int descriptor, struct sockaddr *address, socklen_t *length) {
    if (!functions_ready()) {
        return -1;
    }
    if (is_proxy_unix_socket(descriptor)) {
        return synthetic_name(address, length, RADAR_PROXY_PORT);
    }
    return real_getpeername_function(descriptor, address, length);
}

__attribute__((visibility("default")))
int getsockname(int descriptor, struct sockaddr *address, socklen_t *length) {
    if (!functions_ready()) {
        return -1;
    }
    if (is_proxy_unix_socket(descriptor)) {
        return synthetic_name(address, length, 0);
    }
    return real_getsockname_function(descriptor, address, length);
}
