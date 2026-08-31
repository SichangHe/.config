#define _GNU_SOURCE
#include <errno.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <linux/memfd.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

extern char **environ;

#ifndef PR_SET_PTRACER_ANY
#define PR_SET_PTRACER_ANY ((unsigned long)-1)
#endif

static int emit_file(const char *path) {
    char value[64];
    ssize_t count;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return 31;
    }
    count = read(fd, value, sizeof(value));
    close(fd);
    if (count <= 0 || write(STDOUT_FILENO, value, (size_t)count) != count) {
        return 32;
    }
    return 0;
}

static int extension_value(const char *path, const char *expected, int emit) {
    const char *(*value)(void);
    const char *result;
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (handle == NULL) {
        return 49;
    }
    *(void **)(&value) = dlsym(handle, "extension_value");
    if (value == NULL) {
        dlclose(handle);
        return 50;
    }
    result = value();
    if (strcmp(result, expected) != 0) {
        dlclose(handle);
        return 51;
    }
    if (emit && write(STDOUT_FILENO, result, strlen(result)) != (ssize_t)strlen(result)) {
        dlclose(handle);
        return 52;
    }
    dlclose(handle);
    return 0;
}

int main(int argc, char **argv) {
    char cwd[256];
    int environment_count = 0;
    if (strcmp(argv[0], "/bin/child") == 0) {
        return emit_file("/app/child.dat");
    }
    while (environ[environment_count] != NULL) {
        environment_count++;
    }
    errno = 0;
    if (environment_count != 1 || strcmp(environ[0], "SEALED=yes") != 0 ||
        getcwd(cwd, sizeof(cwd)) == NULL || strcmp(cwd, "/work") != 0) {
        return 33;
    }
    if (fcntl(STDIN_FILENO, F_GETFD) != -1 || errno != EBADF) {
        return 47;
    }
    if (extension_value("/app/startup.so", "startup-original\n", 0) != 0) {
        return 44;
    }
    if (argc == 2 && strcmp(argv[1], "child") == 0) {
        char *child_argv[] = {"/bin/child", NULL};
        char *child_environment[] = {"SEALED=yes", NULL};
        execve(child_argv[0], child_argv, child_environment);
        return 34;
    }
    if (argc == 2 && strcmp(argv[1], "missing") == 0) {
        char *child_argv[] = {"/bin/missing", NULL};
        execve(child_argv[0], child_argv, environ);
        return errno == ENOENT ? 41 : 35;
    }
    if (argc == 2 && strcmp(argv[1], "memfd") == 0) {
        int fd = (int)syscall(SYS_memfd_create, "runtime", MFD_CLOEXEC);
        return fd < 0 && errno == EPERM ? emit_file("/app/blocked.dat") : 36;
    }
    if (argc == 2 && strcmp(argv[1], "write") == 0) {
        int fd = open("created", O_CREAT | O_WRONLY, 0600);
        return fd < 0 && errno == EROFS ? emit_file("/app/blocked.dat") : 37;
    }
    if (argc == 2 && strcmp(argv[1], "anonexec") == 0) {
        void *memory = mmap(NULL, 4096, PROT_READ | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        return memory == MAP_FAILED && errno == EPERM ? emit_file("/app/blocked.dat") : 38;
    }
    if (argc == 2 && strcmp(argv[1], "mprotect") == 0) {
        void *memory = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (memory == MAP_FAILED) {
            return 39;
        }
        return mprotect(memory, 4096, PROT_READ | PROT_EXEC) < 0 && errno == EPERM ?
            emit_file("/app/blocked.dat") : 40;
    }
    if (argc == 2 && strcmp(argv[1], "filewx") == 0) {
        int fd = open("/bin/main", O_RDONLY | O_CLOEXEC);
        void *memory;
        if (fd < 0) {
            return 45;
        }
        memory = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE, fd, 0);
        close(fd);
        return memory == MAP_FAILED && errno == EPERM ? emit_file("/app/blocked.dat") : 46;
    }
    if (argc == 2 && strcmp(argv[1], "socket") == 0) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        return fd < 0 && errno == EPERM ? emit_file("/app/blocked.dat") : 48;
    }
    if (argc == 2 && strcmp(argv[1], "prctl") == 0) {
        int dumpable_result;
        int dumpable_error;
        int ptracer_result;
        int ptracer_error;
        errno = 0;
        dumpable_result = prctl(PR_SET_DUMPABLE, 1, 0, 0, 0);
        dumpable_error = errno;
        errno = 0;
        ptracer_result = prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
        ptracer_error = errno;
        return dumpable_result < 0 && dumpable_error == EPERM &&
               ptracer_result < 0 && ptracer_error == EPERM ?
            emit_file("/app/blocked.dat") : 53;
    }
    return extension_value("/app/module.so", "module-original\n", 1);
}
