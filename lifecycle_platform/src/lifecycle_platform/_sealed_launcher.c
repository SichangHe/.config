#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/mount.h>
#include <linux/seccomp.h>
#include <linux/stat.h>
#include <stdint.h>
#include <stddef.h>

#define SB_AT_FDCWD -100
#define SB_AT_SYMLINK_NOFOLLOW 0x100
#define SB_AT_EMPTY_PATH 0x1000
#define SB_AT_RECURSIVE 0x8000
#define SB_O_RDONLY 0
#define SB_O_WRONLY 1
#define SB_O_RDWR 2
#define SB_O_ACCMODE 3
#define SB_O_CREAT 0100
#define SB_O_EXCL 0200
#define SB_O_NONBLOCK 04000
#define SB_O_DIRECTORY 0200000
#define SB_O_NOFOLLOW 0400000
#define SB_O_CLOEXEC 02000000
#define SB_F_GETFL 3
#define SB_CLONE_NEWNS 0x00020000
#define SB_CLONE_NEWUSER 0x10000000
#define SB_FSOPEN_CLOEXEC 1
#define SB_FSCONFIG_CMD_CREATE 6
#define SB_FSMOUNT_CLOEXEC 1
#define SB_CLOSE_RANGE_UNSHARE 2
#define SB_PR_SET_DUMPABLE 4
#define SB_PR_SET_NO_NEW_PRIVS 38
#define SB_PR_SET_SECUREBITS 28
#define SB_PR_CAPBSET_DROP 24
#define SB_PR_CAP_AMBIENT 47
#define SB_PR_CAP_AMBIENT_CLEAR_ALL 4
#define SB_SECBIT_NOROOT 1
#define SB_SECBIT_NOROOT_LOCKED 2
#define SB_SECBIT_NO_SETUID_FIXUP 4
#define SB_SECBIT_NO_SETUID_FIXUP_LOCKED 8
#define SB_SECBIT_KEEP_CAPS_LOCKED 32
#define SB_STATX_BASIC_STATS 0x7ff
#define SB_S_IFMT 0170000
#define SB_S_IFREG 0100000
#define SB_S_IFDIR 0040000
#define SB_S_IFCHR 0020000
#define SB_S_IFIFO 0010000
#define SB_S_IFSOCK 0140000
#define SB_MAX_ROOTS 256
#define SB_MAX_FILES 1024
#define SB_MAX_ARGS 256
#define SB_MAX_ENV 256
#define SB_MAX_CHAIN 64
#define SB_MAX_MANIFEST_BYTES (64UL * 1024UL * 1024UL)
#define SB_PAGE_BYTES 4096UL

#define SB_SYS_read 0
#define SB_SYS_write 1
#define SB_SYS_close 3
#define SB_SYS_lseek 8
#define SB_SYS_fcntl 72
#define SB_SYS_mmap 9
#define SB_SYS_mprotect 10
#define SB_SYS_munmap 11
#define SB_SYS_pread64 17
#define SB_SYS_shmat 30
#define SB_SYS_socket 41
#define SB_SYS_connect 42
#define SB_SYS_accept 43
#define SB_SYS_sendto 44
#define SB_SYS_recvfrom 45
#define SB_SYS_sendmsg 46
#define SB_SYS_recvmsg 47
#define SB_SYS_bind 49
#define SB_SYS_listen 50
#define SB_SYS_socketpair 53
#define SB_SYS_fsync 74
#define SB_SYS_getcwd 79
#define SB_SYS_chdir 80
#define SB_SYS_fchmod 91
#define SB_SYS_umask 95
#define SB_SYS_getuid 102
#define SB_SYS_getgid 104
#define SB_SYS_geteuid 107
#define SB_SYS_getegid 108
#define SB_SYS_setresuid 117
#define SB_SYS_setresgid 119
#define SB_SYS_capget 125
#define SB_SYS_capset 126
#define SB_SYS_fstatfs 138
#define SB_SYS_prctl 157
#define SB_SYS_chroot 161
#define SB_SYS_mount 165
#define SB_SYS_pivot_root 155
#define SB_SYS_umount2 166
#define SB_SYS_getdents64 217
#define SB_SYS_exit_group 231
#define SB_SYS_getpid 39
#define SB_SYS_clone 56
#define SB_SYS_clone3 435
#define SB_SYS_openat 257
#define SB_SYS_mkdirat 258
#define SB_SYS_newfstatat 262
#define SB_SYS_unlinkat 263
#define SB_SYS_setns 308
#define SB_SYS_seccomp 317
#define SB_SYS_memfd_create 319
#define SB_SYS_execveat 322
#define SB_SYS_statx 332
#define SB_SYS_io_uring_setup 425
#define SB_SYS_io_uring_enter 426
#define SB_SYS_io_uring_register 427
#define SB_SYS_fsopen 430
#define SB_SYS_fsconfig 431
#define SB_SYS_fsmount 432
#define SB_SYS_move_mount 429
#define SB_SYS_open_tree 428
#define SB_SYS_close_range 436
#define SB_SYS_openat2 437
#define SB_SYS_mount_setattr 442
#define SB_SYS_execve 59
#define SB_SYS_exit 60
#define SB_SYS_unshare 272
#define SB_SYS_open_by_handle_at 304
#define SB_SYS_name_to_handle_at 303
#define SB_SYS_ptrace 101
#define SB_SYS_recvmmsg 299
#define SB_SYS_accept4 288
#define SB_SYS_bpf 321
#define SB_SYS_userfaultfd 323
#define SB_SYS_pkey_mprotect 329
#define SB_SYS_pidfd_getfd 438
#define SB_SYS_memfd_secret 447
#define SB_SYS_fchdir 81

#define SB_PROT_READ 1
#define SB_PROT_WRITE 2
#define SB_MAP_PRIVATE 2
#define SB_MAP_ANONYMOUS 0x20
#define SB_MAP_FAILED ((void *)-1)
#define SB_SECCOMP_SET_MODE_FILTER 1
#define SB_SECCOMP_FILTER_FLAG_TSYNC 1
#define SB_SECCOMP_RET_KILL_PROCESS 0x80000000U
#define SB_SECCOMP_RET_ERRNO 0x00050000U
#define SB_SECCOMP_RET_ALLOW 0x7fff0000U
#define SB_EPERM 1
#define SB_EINVAL 22
#define SB_EEXIST 17
#define SB_EROFS 30
#define SB_PROC_SUPER_MAGIC 0x9fa0UL
#define SB_BINFMTFS_MAGIC 0x42494e4dUL

#ifndef SB_MANIFEST_SHA256_HEX
#error SB_MANIFEST_SHA256_HEX is required
#endif

struct sb_identity {
    uint64_t device_major;
    uint64_t device_minor;
    uint64_t inode;
    uint64_t mode;
    uint64_t uid;
    uint64_t gid;
    uint64_t link_count;
    uint64_t size_bytes;
    int64_t modified_ns;
    int64_t changed_ns;
};

struct sb_string {
    char *value;
    uint32_t size;
};

struct sb_chain {
    struct sb_string path;
    uint32_t count;
    struct sb_identity expected[SB_MAX_CHAIN];
    int fds[SB_MAX_CHAIN];
};

struct sb_file {
    uint32_t root_index;
    struct sb_string source;
    struct sb_string destination;
    struct sb_identity expected;
    uint8_t digest[32];
    uint32_t mode;
    int source_fd;
    int destination_fd;
    struct sb_identity destination_identity;
};

struct sb_manifest {
    struct sb_chain launch_directory;
    struct sb_string executable;
    struct sb_string cwd;
    uint32_t root_count;
    uint32_t file_count;
    uint32_t arg_count;
    uint32_t env_count;
    struct sb_chain roots[SB_MAX_ROOTS];
    struct sb_file files[SB_MAX_FILES];
    char *args[SB_MAX_ARGS + 1];
    char *environment[SB_MAX_ENV + 1];
};

struct sb_parser {
    uint8_t *cursor;
    uint8_t *end;
};

struct sb_sha256 {
    uint32_t state[8];
    uint64_t total_bytes;
    uint8_t block[64];
    uint32_t block_bytes;
};

struct sb_statfs {
    long type;
    long block_size;
    unsigned long blocks;
    unsigned long blocks_free;
    unsigned long blocks_available;
    unsigned long files;
    unsigned long files_free;
    struct { int first; int second; } filesystem_id;
    long name_length;
    long fragment_size;
    long flags;
    long spare[4];
};

struct sb_linux_dirent64 {
    uint64_t inode;
    int64_t offset;
    uint16_t record_size;
    uint8_t type;
    char name[];
};

static struct sb_manifest sb_manifest;
static const char sb_expected_manifest_hex[] = SB_MANIFEST_SHA256_HEX;
static int sb_manifest_fd = -1;
static struct sb_identity sb_manifest_identity;
static void sb_test_pause(int phase);

static long sb_syscall1(long number, long first) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first) : "rcx", "r11", "memory");
    return result;
}

static long sb_syscall2(long number, long first, long second) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first), "S"(second) : "rcx", "r11", "memory");
    return result;
}

static long sb_syscall3(long number, long first, long second, long third) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first), "S"(second), "d"(third) : "rcx", "r11", "memory");
    return result;
}

static long sb_syscall4(long number, long first, long second, long third, long fourth) {
    long result;
    register long r10 __asm__("r10") = fourth;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first), "S"(second), "d"(third), "r"(r10) : "rcx", "r11", "memory");
    return result;
}

static long sb_syscall5(long number, long first, long second, long third, long fourth, long fifth) {
    long result;
    register long r10 __asm__("r10") = fourth;
    register long r8 __asm__("r8") = fifth;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first), "S"(second), "d"(third), "r"(r10), "r"(r8) : "rcx", "r11", "memory");
    return result;
}

static long sb_syscall6(long number, long first, long second, long third, long fourth, long fifth, long sixth) {
    long result;
    register long r10 __asm__("r10") = fourth;
    register long r8 __asm__("r8") = fifth;
    register long r9 __asm__("r9") = sixth;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(first), "S"(second), "d"(third), "r"(r10), "r"(r8), "r"(r9) : "rcx", "r11", "memory");
    return result;
}

static size_t sb_strlen(const char *value) {
    size_t size = 0;
    while (value[size] != 0) {
        size++;
    }
    return size;
}

static int sb_memory_equal(const void *first_value, const void *second_value, size_t size) {
    const uint8_t *first = first_value;
    const uint8_t *second = second_value;
    size_t index;
    uint8_t difference = 0;
    for (index = 0; index < size; index++) {
        difference |= first[index] ^ second[index];
    }
    return difference == 0;
}

static int sb_string_equal(const char *first, const char *second) {
    size_t index = 0;
    while (first[index] != 0 && first[index] == second[index]) {
        index++;
    }
    return first[index] == second[index];
}

static int sb_prefix(const char *value, const char *prefix) {
    size_t index = 0;
    while (prefix[index] != 0) {
        if (value[index] != prefix[index]) {
            return 0;
        }
        index++;
    }
    return 1;
}

__attribute__((noreturn)) static void sb_fail(int status, const char *message) {
    sb_syscall3(SB_SYS_write, 2, (long)message, (long)sb_strlen(message));
    sb_syscall3(SB_SYS_write, 2, (long)"\n", 1);
    sb_syscall1(SB_SYS_exit_group, status);
    __builtin_unreachable();
}

static void sb_require(long result, int status, const char *message) {
    if (result < 0) {
        sb_fail(status, message);
    }
}

static uint32_t sb_rotr(uint32_t value, uint32_t count) {
    return (value >> count) | (value << (32U - count));
}

static uint32_t sb_be32(const uint8_t *value) {
    return ((uint32_t)value[0] << 24) | ((uint32_t)value[1] << 16) | ((uint32_t)value[2] << 8) | value[3];
}

static uint16_t sb_le16(const uint8_t *value) {
    return (uint16_t)value[0] | ((uint16_t)value[1] << 8);
}

static uint32_t sb_le32(const uint8_t *value) {
    return (uint32_t)value[0] | ((uint32_t)value[1] << 8) |
           ((uint32_t)value[2] << 16) | ((uint32_t)value[3] << 24);
}

static uint64_t sb_le64(const uint8_t *value) {
    uint64_t result = 0;
    uint32_t index;
    for (index = 0; index < 8; index++) {
        result |= (uint64_t)value[index] << (index * 8U);
    }
    return result;
}

static void sb_store_be32(uint8_t *target, uint32_t value) {
    target[0] = (uint8_t)(value >> 24);
    target[1] = (uint8_t)(value >> 16);
    target[2] = (uint8_t)(value >> 8);
    target[3] = (uint8_t)value;
}

static void sb_sha256_transform(struct sb_sha256 *context, const uint8_t block[64]) {
    static const uint32_t constants[64] = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
        0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
        0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
        0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
        0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
        0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
        0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U, 0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U
    };
    uint32_t words[64];
    uint32_t a, b, c, d, e, f, g, h, first, second, choice, majority;
    uint32_t index;
    for (index = 0; index < 16; index++) {
        words[index] = sb_be32(block + index * 4);
    }
    for (index = 16; index < 64; index++) {
        first = sb_rotr(words[index - 15], 7) ^ sb_rotr(words[index - 15], 18) ^ (words[index - 15] >> 3);
        second = sb_rotr(words[index - 2], 17) ^ sb_rotr(words[index - 2], 19) ^ (words[index - 2] >> 10);
        words[index] = words[index - 16] + first + words[index - 7] + second;
    }
    a = context->state[0]; b = context->state[1]; c = context->state[2]; d = context->state[3];
    e = context->state[4]; f = context->state[5]; g = context->state[6]; h = context->state[7];
    for (index = 0; index < 64; index++) {
        first = sb_rotr(e, 6) ^ sb_rotr(e, 11) ^ sb_rotr(e, 25);
        choice = (e & f) ^ ((~e) & g);
        second = h + first + choice + constants[index] + words[index];
        first = sb_rotr(a, 2) ^ sb_rotr(a, 13) ^ sb_rotr(a, 22);
        majority = (a & b) ^ (a & c) ^ (b & c);
        h = g; g = f; f = e; e = d + second; d = c; c = b; b = a; a = second + first + majority;
    }
    context->state[0] += a; context->state[1] += b; context->state[2] += c; context->state[3] += d;
    context->state[4] += e; context->state[5] += f; context->state[6] += g; context->state[7] += h;
}

static void sb_sha256_init(struct sb_sha256 *context) {
    static const uint32_t initial[8] = {
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U
    };
    uint32_t index;
    for (index = 0; index < 8; index++) {
        context->state[index] = initial[index];
    }
    context->total_bytes = 0;
    context->block_bytes = 0;
}

static void sb_sha256_update(struct sb_sha256 *context, const uint8_t *value, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        size_t available = 64U - context->block_bytes;
        size_t count = size - offset < available ? size - offset : available;
        size_t index;
        for (index = 0; index < count; index++) {
            context->block[context->block_bytes + index] = value[offset + index];
        }
        context->block_bytes += (uint32_t)count;
        context->total_bytes += count;
        offset += count;
        if (context->block_bytes == 64U) {
            sb_sha256_transform(context, context->block);
            context->block_bytes = 0;
        }
    }
}

static void sb_sha256_final(struct sb_sha256 *context, uint8_t output[32]) {
    uint64_t bits = context->total_bytes * 8U;
    uint32_t index;
    context->block[context->block_bytes++] = 0x80U;
    if (context->block_bytes > 56U) {
        while (context->block_bytes < 64U) {
            context->block[context->block_bytes++] = 0;
        }
        sb_sha256_transform(context, context->block);
        context->block_bytes = 0;
    }
    while (context->block_bytes < 56U) {
        context->block[context->block_bytes++] = 0;
    }
    for (index = 0; index < 8; index++) {
        context->block[63U - index] = (uint8_t)(bits >> (index * 8U));
    }
    sb_sha256_transform(context, context->block);
    for (index = 0; index < 8; index++) {
        sb_store_be32(output + index * 4U, context->state[index]);
    }
}

static int sb_hex_nibble(char value) {
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    return -1;
}

static void sb_decode_expected_digest(uint8_t output[32]) {
    uint32_t index;
    if (sb_strlen(sb_expected_manifest_hex) != 64U) {
        sb_fail(11, "manifest digest capability is invalid");
    }
    for (index = 0; index < 32; index++) {
        int high = sb_hex_nibble(sb_expected_manifest_hex[index * 2U]);
        int low = sb_hex_nibble(sb_expected_manifest_hex[index * 2U + 1U]);
        if (high < 0 || low < 0) {
            sb_fail(11, "manifest digest capability is invalid");
        }
        output[index] = (uint8_t)((high << 4) | low);
    }
}

static uint32_t sb_read_u32(struct sb_parser *parser) {
    uint32_t value;
    if ((size_t)(parser->end - parser->cursor) < 4U) {
        sb_fail(12, "manifest is truncated");
    }
    value = (uint32_t)parser->cursor[0] | ((uint32_t)parser->cursor[1] << 8) |
            ((uint32_t)parser->cursor[2] << 16) | ((uint32_t)parser->cursor[3] << 24);
    parser->cursor += 4;
    return value;
}

static uint64_t sb_read_u64(struct sb_parser *parser) {
    uint64_t value = 0;
    uint32_t index;
    if ((size_t)(parser->end - parser->cursor) < 8U) {
        sb_fail(12, "manifest is truncated");
    }
    for (index = 0; index < 8; index++) {
        value |= (uint64_t)parser->cursor[index] << (index * 8U);
    }
    parser->cursor += 8;
    return value;
}

static struct sb_string sb_read_string(struct sb_parser *parser) {
    struct sb_string result;
    uint32_t index;
    result.size = sb_read_u32(parser);
    if (result.size == 0 || result.size > 4095U ||
        (size_t)(parser->end - parser->cursor) < (size_t)result.size + 1U) {
        sb_fail(12, "manifest string is invalid");
    }
    result.value = (char *)parser->cursor;
    for (index = 0; index < result.size; index++) {
        if (parser->cursor[index] == 0) {
            sb_fail(12, "manifest string contains NUL");
        }
    }
    if (parser->cursor[result.size] != 0) {
        sb_fail(12, "manifest string is unterminated");
    }
    parser->cursor += result.size + 1U;
    return result;
}

static struct sb_identity sb_read_identity(struct sb_parser *parser) {
    struct sb_identity result;
    result.device_major = sb_read_u64(parser);
    result.device_minor = sb_read_u64(parser);
    result.inode = sb_read_u64(parser);
    result.mode = sb_read_u64(parser);
    result.uid = sb_read_u64(parser);
    result.gid = sb_read_u64(parser);
    result.link_count = sb_read_u64(parser);
    result.size_bytes = sb_read_u64(parser);
    result.modified_ns = (int64_t)sb_read_u64(parser);
    result.changed_ns = (int64_t)sb_read_u64(parser);
    return result;
}

static void sb_read_bytes(struct sb_parser *parser, uint8_t *destination, size_t size) {
    size_t index;
    if ((size_t)(parser->end - parser->cursor) < size) {
        sb_fail(12, "manifest is truncated");
    }
    for (index = 0; index < size; index++) {
        destination[index] = parser->cursor[index];
    }
    parser->cursor += size;
}

static void sb_parse_chain(struct sb_parser *parser, struct sb_chain *chain) {
    uint32_t index;
    chain->path = sb_read_string(parser);
    chain->count = sb_read_u32(parser);
    if (chain->count == 0 || chain->count > SB_MAX_CHAIN) {
        sb_fail(12, "manifest directory chain is invalid");
    }
    for (index = 0; index < chain->count; index++) {
        chain->expected[index] = sb_read_identity(parser);
        chain->fds[index] = -1;
    }
}

static void sb_parse_manifest(uint8_t *value, size_t size, struct sb_manifest *manifest) {
    static const uint8_t magic[16] = {
        'L', 'P', 'C', 'B', 'O', 'O', 'T', 'S', 'T', 'R', 'A', 'P', 0, 1, 0, 0
    };
    struct sb_parser parser;
    uint32_t root_count, file_count, arg_count, env_count, reserved, index;
    if (size < 36U || !sb_memory_equal(value, magic, sizeof(magic))) {
        sb_fail(12, "manifest capability/version is invalid");
    }
    parser.cursor = value + sizeof(magic);
    parser.end = value + size;
    root_count = sb_read_u32(&parser);
    file_count = sb_read_u32(&parser);
    arg_count = sb_read_u32(&parser);
    env_count = sb_read_u32(&parser);
    reserved = sb_read_u32(&parser);
    if (root_count == 0 || root_count > SB_MAX_ROOTS || file_count == 0 || file_count > SB_MAX_FILES ||
        arg_count == 0 || arg_count > SB_MAX_ARGS || env_count > SB_MAX_ENV || reserved != 0) {
        sb_fail(12, "manifest counts are invalid");
    }
    manifest->root_count = root_count;
    manifest->file_count = file_count;
    manifest->arg_count = arg_count;
    manifest->env_count = env_count;
    sb_parse_chain(&parser, &manifest->launch_directory);
    manifest->executable = sb_read_string(&parser);
    manifest->cwd = sb_read_string(&parser);
    for (index = 0; index < root_count; index++) {
        sb_parse_chain(&parser, &manifest->roots[index]);
    }
    for (index = 0; index < file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        file->root_index = sb_read_u32(&parser);
        file->source = sb_read_string(&parser);
        file->destination = sb_read_string(&parser);
        file->expected = sb_read_identity(&parser);
        sb_read_bytes(&parser, file->digest, sizeof(file->digest));
        file->mode = sb_read_u32(&parser);
        file->source_fd = -1;
        file->destination_fd = -1;
        if (file->root_index >= root_count || file->mode > 0555U || (file->mode & 0222U) != 0) {
            sb_fail(12, "manifest file entry is invalid");
        }
    }
    for (index = 0; index < arg_count; index++) {
        manifest->args[index] = sb_read_string(&parser).value;
    }
    manifest->args[arg_count] = 0;
    for (index = 0; index < env_count; index++) {
        manifest->environment[index] = sb_read_string(&parser).value;
    }
    manifest->environment[env_count] = 0;
    if (parser.cursor != parser.end) {
        sb_fail(12, "manifest has trailing bytes");
    }
}

static int64_t sb_timestamp_ns(const struct statx_timestamp *value) {
    return value->tv_sec * 1000000000LL + value->tv_nsec;
}

static struct sb_identity sb_stat_fd(int fd) {
    struct statx value;
    struct sb_identity result;
    sb_require(sb_syscall5(SB_SYS_statx, fd, (long)"", SB_AT_EMPTY_PATH, SB_STATX_BASIC_STATS, (long)&value), 13, "descriptor identity is unavailable");
    result.device_major = value.stx_dev_major;
    result.device_minor = value.stx_dev_minor;
    result.inode = value.stx_ino;
    result.mode = value.stx_mode;
    result.uid = value.stx_uid;
    result.gid = value.stx_gid;
    result.link_count = value.stx_nlink;
    result.size_bytes = value.stx_size;
    result.modified_ns = sb_timestamp_ns(&value.stx_mtime);
    result.changed_ns = sb_timestamp_ns(&value.stx_ctime);
    return result;
}

static struct sb_identity sb_stat_at(int parent_fd, const char *name) {
    struct statx value;
    struct sb_identity result;
    sb_require(sb_syscall5(SB_SYS_statx, parent_fd, (long)name, SB_AT_SYMLINK_NOFOLLOW, SB_STATX_BASIC_STATS, (long)&value), 13, "directory entry identity is unavailable");
    result.device_major = value.stx_dev_major;
    result.device_minor = value.stx_dev_minor;
    result.inode = value.stx_ino;
    result.mode = value.stx_mode;
    result.uid = value.stx_uid;
    result.gid = value.stx_gid;
    result.link_count = value.stx_nlink;
    result.size_bytes = value.stx_size;
    result.modified_ns = sb_timestamp_ns(&value.stx_mtime);
    result.changed_ns = sb_timestamp_ns(&value.stx_ctime);
    return result;
}

static int sb_same_object(const struct sb_identity *first, const struct sb_identity *second) {
    return first->device_major == second->device_major && first->device_minor == second->device_minor &&
           first->inode == second->inode && (first->mode & SB_S_IFMT) == (second->mode & SB_S_IFMT);
}

static int sb_same_identity(const struct sb_identity *first, const struct sb_identity *second) {
    return sb_same_object(first, second) && first->mode == second->mode && first->uid == second->uid &&
           first->gid == second->gid && first->link_count == second->link_count &&
           first->size_bytes == second->size_bytes && first->modified_ns == second->modified_ns &&
           first->changed_ns == second->changed_ns;
}

static int sb_same_mapped_identity(const struct sb_identity *first, const struct sb_identity *second) {
    return sb_same_object(first, second) && first->mode == second->mode &&
           first->link_count == second->link_count && first->size_bytes == second->size_bytes &&
           first->modified_ns == second->modified_ns && first->changed_ns == second->changed_ns;
}

static int sb_same_directory(const struct sb_identity *first, const struct sb_identity *second) {
    return sb_same_object(first, second) && first->mode == second->mode && first->uid == second->uid &&
           first->gid == second->gid;
}

static void sb_hash_memory(const uint8_t *value, size_t size, uint8_t output[32]) {
    struct sb_sha256 context;
    sb_sha256_init(&context);
    sb_sha256_update(&context, value, size);
    sb_sha256_final(&context, output);
}

static void sb_hash_fd(int fd, uint64_t size, uint8_t output[32]) {
    struct sb_sha256 context;
    uint8_t buffer[65536];
    uint64_t offset = 0;
    sb_sha256_init(&context);
    while (offset < size) {
        size_t requested = size - offset < sizeof(buffer) ? (size_t)(size - offset) : sizeof(buffer);
        long count = sb_syscall4(SB_SYS_pread64, fd, (long)buffer, (long)requested, (long)offset);
        if (count <= 0) {
            sb_fail(14, "authenticated file changed while hashing");
        }
        sb_sha256_update(&context, buffer, (size_t)count);
        offset += (uint64_t)count;
    }
    sb_sha256_final(&context, output);
}

static void sb_verify_hashed_fd(int fd, const struct sb_identity *identity, const uint8_t digest[32]) {
    uint8_t actual[32];
    struct sb_identity before = sb_stat_fd(fd);
    struct sb_identity after;
    if (!sb_same_identity(&before, identity) || (before.mode & SB_S_IFMT) != SB_S_IFREG) {
        sb_fail(14, "authenticated file identity drifted");
    }
    sb_hash_fd(fd, before.size_bytes, actual);
    after = sb_stat_fd(fd);
    if (!sb_memory_equal(actual, digest, sizeof(actual)) || !sb_same_identity(&after, identity)) {
        sb_fail(14, "authenticated file content drifted");
    }
}

static void sb_verify_mapped_hashed_fd(int fd, const struct sb_identity *identity, const uint8_t digest[32]) {
    uint8_t actual[32];
    struct sb_identity before = sb_stat_fd(fd);
    struct sb_identity after;
    if (!sb_same_mapped_identity(&before, identity) || (before.mode & SB_S_IFMT) != SB_S_IFREG) {
        sb_fail(14, "authenticated file identity drifted after namespace mapping");
    }
    sb_hash_fd(fd, before.size_bytes, actual);
    after = sb_stat_fd(fd);
    if (!sb_memory_equal(actual, digest, sizeof(actual)) || !sb_same_mapped_identity(&after, identity)) {
        sb_fail(14, "authenticated file content drifted after namespace mapping");
    }
}

static void sb_require_runtime_elf(int fd, const struct sb_identity *identity) {
    uint8_t header[64];
    long count = sb_syscall4(SB_SYS_pread64, fd, (long)header, sizeof(header), 0);
    uint64_t program_offset;
    uint16_t entry_size;
    uint16_t entry_count;
    if (count != (long)sizeof(header) ||
        header[0] != 0x7fU || header[1] != 'E' || header[2] != 'L' || header[3] != 'F' ||
        header[4] != 2U || header[5] != 1U || header[6] != 1U ||
        (sb_le16(header + 16) != 2U && sb_le16(header + 16) != 3U) ||
        sb_le16(header + 18) != 62U || sb_le32(header + 20) != 1U ||
        sb_le16(header + 52) < 64U) {
        sb_fail(14, "execute-bit source is not a Linux x86-64 ELF binary");
    }
    program_offset = sb_le64(header + 32);
    entry_size = sb_le16(header + 54);
    entry_count = sb_le16(header + 56);
    if (entry_size < 56U || entry_count == 0U || entry_count > 128U ||
        program_offset > identity->size_bytes ||
        entry_count > (identity->size_bytes - program_offset) / entry_size) {
        sb_fail(14, "execute-bit source ELF program-header table is invalid");
    }
}

static int sb_copy_component(const char **cursor, char output[256]) {
    size_t size = 0;
    while (**cursor == '/') {
        (*cursor)++;
    }
    if (**cursor == 0) {
        return 0;
    }
    while (**cursor != 0 && **cursor != '/') {
        if (size == 255U) {
            sb_fail(15, "path component is too long");
        }
        output[size++] = **cursor;
        (*cursor)++;
    }
    output[size] = 0;
    if ((size == 1U && output[0] == '.') ||
        (size == 2U && output[0] == '.' && output[1] == '.')) {
        sb_fail(15, "path traversal is forbidden");
    }
    return 1;
}

static void sb_require_absolute(const struct sb_string *path) {
    if (path->size < 2U || path->value[0] != '/' || path->value[path->size - 1U] == '/') {
        sb_fail(15, "absolute manifest path is invalid");
    }
}

static void sb_require_leaf(const struct sb_string *path) {
    uint32_t index;
    if (path->size == 0 || path->value[0] == '/') {
        sb_fail(15, "source leaf is invalid");
    }
    for (index = 0; index < path->size; index++) {
        if (path->value[index] == '/') {
            sb_fail(15, "source root must be the exact file parent");
        }
    }
    if (sb_string_equal(path->value, ".") || sb_string_equal(path->value, "..")) {
        sb_fail(15, "source leaf is invalid");
    }
}

static void sb_bind_chain(struct sb_chain *chain) {
    const char *cursor;
    char component[256];
    uint32_t index = 0;
    struct sb_identity actual;
    if (chain->path.size == 0 || chain->path.value[0] != '/') {
        sb_fail(15, "bound directory path is not absolute");
    }
    chain->fds[0] = (int)sb_syscall4(SB_SYS_openat, SB_AT_FDCWD, (long)"/", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0);
    sb_require(chain->fds[0], 15, "filesystem root cannot be bound");
    actual = sb_stat_fd(chain->fds[0]);
    if (!sb_same_directory(&actual, &chain->expected[0]) || (actual.mode & SB_S_IFMT) != SB_S_IFDIR) {
        sb_fail(15, "directory anchor identity drifted");
    }
    cursor = chain->path.value;
    while (sb_copy_component(&cursor, component)) {
        int fd;
        index++;
        if (index >= chain->count) {
            sb_fail(15, "directory chain has too few identities");
        }
        fd = (int)sb_syscall4(SB_SYS_openat, chain->fds[index - 1U], (long)component,
                             SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0);
        sb_require(fd, 15, "directory component cannot be bound");
        chain->fds[index] = fd;
        actual = sb_stat_fd(fd);
        if (!sb_same_directory(&actual, &chain->expected[index]) || (actual.mode & SB_S_IFMT) != SB_S_IFDIR) {
            sb_fail(15, "directory component identity drifted");
        }
    }
    if (index + 1U != chain->count) {
        sb_fail(15, "directory chain has too many identities");
    }
}

static void sb_validate_chain(struct sb_chain *chain) {
    const char *cursor = chain->path.value;
    char component[256];
    uint32_t index = 0;
    struct sb_identity actual = sb_stat_fd(chain->fds[0]);
    if (!sb_same_object(&actual, &chain->expected[0]) || actual.mode != chain->expected[0].mode) {
        sb_fail(15, "directory anchor drifted after binding");
    }
    while (sb_copy_component(&cursor, component)) {
        struct sb_identity descriptor_identity;
        struct sb_identity entry_identity;
        index++;
        descriptor_identity = sb_stat_fd(chain->fds[index]);
        entry_identity = sb_stat_at(chain->fds[index - 1U], component);
        if (!sb_same_object(&descriptor_identity, &chain->expected[index]) ||
            descriptor_identity.mode != chain->expected[index].mode ||
            !sb_same_object(&entry_identity, &descriptor_identity)) {
            sb_fail(15, "directory attachment drifted after binding");
        }
    }
}

static int sb_final_fd(const struct sb_chain *chain) {
    return chain->fds[chain->count - 1U];
}

static int sb_forbidden_environment_name(const char *entry) {
    static const char *exact[] = {
        "BASH_ENV=", "BASHOPTS=", "CDPATH=", "CLASSPATH=", "ENV=", "GCONV_PATH=",
        "IFS=", "LOCPATH=", "NODE_OPTIONS=", "PERL5OPT=", "RUBYOPT=", "SHELLOPTS=",
        "GLIBC_TUNABLES=", "JAVA_TOOL_OPTIONS="
    };
    static const char *prefixes[] = {"LD_", "PYTHON"};
    size_t index;
    for (index = 0; index < sizeof(exact) / sizeof(exact[0]); index++) {
        if (sb_prefix(entry, exact[index])) {
            return 1;
        }
    }
    for (index = 0; index < sizeof(prefixes) / sizeof(prefixes[0]); index++) {
        if (sb_prefix(entry, prefixes[index])) {
            return 1;
        }
    }
    return 0;
}

static void sb_validate_environment(char **ambient, struct sb_manifest *manifest) {
    uint32_t index, other;
    if (ambient[0] != 0) {
        sb_fail(17, "ambient environment is not empty");
    }
    for (index = 0; index < manifest->env_count; index++) {
        char *equals = manifest->environment[index];
        if (sb_forbidden_environment_name(equals)) {
            sb_fail(17, "sealed loader or startup environment is forbidden");
        }
        while (*equals != 0 && *equals != '=') {
            equals++;
        }
        if (equals == manifest->environment[index] || *equals != '=') {
            sb_fail(12, "sealed environment entry is invalid");
        }
        for (other = index + 1U; other < manifest->env_count; other++) {
            char *left = manifest->environment[index];
            char *right = manifest->environment[other];
            while (*left != 0 && *left != '=' && *left == *right) {
                left++;
                right++;
            }
            if (*left == '=' && *right == '=') {
                sb_fail(12, "sealed environment names are duplicated");
            }
        }
    }
}

static void sb_validate_paths(struct sb_manifest *manifest) {
    uint32_t index, other;
    int executable_found = 0;
    sb_require_absolute(&manifest->executable);
    sb_require_absolute(&manifest->cwd);
    for (index = 0; index < manifest->root_count; index++) {
        if (manifest->roots[index].path.value[0] != '/') {
            sb_fail(12, "source root path is invalid");
        }
    }
    for (index = 0; index < manifest->file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        sb_require_leaf(&file->source);
        sb_require_absolute(&file->destination);
        if (sb_string_equal(file->destination.value, manifest->executable.value)) {
            if ((file->mode & 0111U) == 0 || executable_found) {
                sb_fail(12, "sealed executable entry is invalid");
            }
            executable_found = 1;
        }
        for (other = index + 1U; other < manifest->file_count; other++) {
            if (sb_string_equal(file->destination.value, manifest->files[other].destination.value)) {
                sb_fail(12, "runtime destination is duplicated");
            }
        }
    }
    if (!executable_found) {
        sb_fail(12, "sealed executable is absent");
    }
}

static void sb_validate_current_directory(struct sb_chain *launch_directory) {
    char value[4096];
    int fd = (int)sb_syscall4(SB_SYS_openat, SB_AT_FDCWD, (long)".", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0);
    struct sb_identity actual;
    long size;
    sb_require(fd, 15, "current directory cannot be bound");
    actual = sb_stat_fd(fd);
    if (!sb_same_object(&actual, &launch_directory->expected[launch_directory->count - 1U])) {
        sb_fail(15, "current directory identity drifted");
    }
    size = sb_syscall2(SB_SYS_getcwd, (long)value, sizeof(value));
    if (size <= 0 || !sb_string_equal(value, launch_directory->path.value)) {
        sb_fail(15, "current directory path drifted");
    }
    sb_require(sb_syscall1(SB_SYS_close, fd), 15, "current directory close failed");
}

static void sb_bind_sources(struct sb_manifest *manifest) {
    uint32_t index;
    sb_bind_chain(&manifest->launch_directory);
    sb_validate_current_directory(&manifest->launch_directory);
    for (index = 0; index < manifest->root_count; index++) {
        sb_bind_chain(&manifest->roots[index]);
    }
    sb_test_pause(2);
    for (index = 0; index < manifest->file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        struct sb_identity entry;
        file->source_fd = (int)sb_syscall4(
            SB_SYS_openat,
            sb_final_fd(&manifest->roots[file->root_index]),
            (long)file->source.value,
            SB_O_RDONLY | SB_O_NONBLOCK | SB_O_NOFOLLOW | SB_O_CLOEXEC,
            0
        );
        sb_require(file->source_fd, 14, "authenticated source file cannot be opened");
#if defined(SB_TEST_PAUSE_PHASE) && defined(SB_TEST_PAUSE_FILE_INDEX)
        if (index == SB_TEST_PAUSE_FILE_INDEX) {
            sb_test_pause(3);
        }
#endif
        sb_verify_hashed_fd(file->source_fd, &file->expected, file->digest);
        if ((file->mode & 0111U) != 0U) {
            sb_require_runtime_elf(file->source_fd, &file->expected);
        }
        entry = sb_stat_at(sb_final_fd(&manifest->roots[file->root_index]), file->source.value);
        if (!sb_same_object(&entry, &file->expected)) {
            sb_fail(14, "source file attachment drifted while binding");
        }
    }
}

static void sb_validate_sources(struct sb_manifest *manifest) {
    uint32_t index;
    sb_validate_chain(&manifest->launch_directory);
    sb_validate_current_directory(&manifest->launch_directory);
    for (index = 0; index < manifest->root_count; index++) {
        sb_validate_chain(&manifest->roots[index]);
    }
    for (index = 0; index < manifest->file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        struct sb_identity entry = sb_stat_at(
            sb_final_fd(&manifest->roots[file->root_index]), file->source.value
        );
        if (!sb_same_object(&entry, &file->expected)) {
            sb_fail(14, "source file attachment drifted after binding");
        }
        sb_verify_mapped_hashed_fd(file->source_fd, &file->expected, file->digest);
    }
}

#if defined(SB_TEST_PAUSE_PHASE)
static void sb_test_pause(int phase) {
    char byte = (char)phase;
    if (phase == SB_TEST_PAUSE_PHASE) {
        sb_require(sb_syscall3(SB_SYS_write, 198, (long)&byte, 1), 19, "test notification failed");
        sb_require(sb_syscall3(SB_SYS_read, 199, (long)&byte, 1), 19, "test resume failed");
    }
}
#else
static void sb_test_pause(int phase) {
    (void)phase;
}
#endif

static size_t sb_decimal(uint32_t value, char output[16]) {
    char reverse[16];
    size_t size = 0;
    size_t index;
    do {
        reverse[size++] = (char)('0' + value % 10U);
        value /= 10U;
    } while (value != 0);
    for (index = 0; index < size; index++) {
        output[index] = reverse[size - index - 1U];
    }
    return size;
}

static void sb_write_map(int proc_self_fd, const char *name, uint32_t outside_id) {
    char value[64];
    char number[16];
    size_t number_size = sb_decimal(outside_id, number);
    size_t offset = 0;
    int fd;
    size_t index;
    value[offset++] = '0'; value[offset++] = ' ';
    for (index = 0; index < number_size; index++) {
        value[offset++] = number[index];
    }
    value[offset++] = ' '; value[offset++] = '1'; value[offset++] = '\n';
    fd = (int)sb_syscall4(SB_SYS_openat, proc_self_fd, (long)name, SB_O_WRONLY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0);
    sb_require(fd, 16, "user namespace identity map cannot be opened");
    if (sb_syscall3(SB_SYS_write, fd, (long)value, (long)offset) != (long)offset) {
        sb_fail(16, "user namespace identity map cannot be written");
    }
    sb_require(sb_syscall1(SB_SYS_close, fd), 16, "identity map close failed");
}

static void sb_require_ptrace_guard(int proc_fd) {
    char value[4];
    int fd = (int)sb_syscall4(
        SB_SYS_openat,
        proc_fd,
        (long)"sys/kernel/yama/ptrace_scope",
        SB_O_RDONLY | SB_O_NOFOLLOW | SB_O_CLOEXEC,
        0
    );
    long count;
    sb_require(fd, 16, "same-uid ptrace guard is unavailable");
    count = sb_syscall3(SB_SYS_read, fd, (long)value, sizeof(value));
    sb_require(sb_syscall1(SB_SYS_close, fd), 16, "ptrace guard close failed");
    if (count < 1 || value[0] < '1' || value[0] > '3') {
        sb_fail(16, "same-uid ptrace guard is disabled");
    }
}

static int sb_binfmt_value_is_persistent(const uint8_t *value, size_t size) {
    size_t index;
    int flags_found = 0;
    for (index = 0; index + 6U <= size; index++) {
        size_t cursor;
        if (index != 0U && value[index - 1U] != '\n') {
            continue;
        }
        if (!sb_memory_equal(value + index, "flags:", 6U)) {
            continue;
        }
        flags_found = 1;
        cursor = index + 6U;
        while (cursor < size && (value[cursor] == ' ' || value[cursor] == '\t')) {
            cursor++;
        }
        while (cursor < size && value[cursor] != '\n') {
            if (value[cursor] == 'F') {
                return 1;
            }
            cursor++;
        }
    }
    if (!flags_found) {
        sb_fail(16, "binfmt_misc handler flags cannot be inspected");
    }
    return 0;
}

static int sb_binfmt_handler_is_persistent(int fd) {
    uint8_t value[4096];
    uint8_t extra;
    size_t size = 0;
    while (size < sizeof(value)) {
        long count = sb_syscall3(SB_SYS_read, fd, (long)(value + size), sizeof(value) - size);
        if (count < 0) {
            sb_fail(16, "binfmt_misc handler cannot be read");
        }
        if (count == 0) {
            break;
        }
        size += (size_t)count;
    }
    if (size == sizeof(value)) {
        long count = sb_syscall3(SB_SYS_read, fd, (long)&extra, 1);
        if (count != 0) {
            sb_fail(16, "binfmt_misc handler exceeds the inspection limit");
        }
    }
    return sb_binfmt_value_is_persistent(value, size);
}

static void sb_reject_persistent_binfmt_handlers(int proc_fd) {
    struct sb_statfs filesystem;
    uint8_t directory_buffer[4096];
    int directory_fd = (int)sb_syscall4(
        SB_SYS_openat,
        proc_fd,
        (long)"sys/fs/binfmt_misc",
        SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC,
        0
    );
    sb_require(directory_fd, 16, "binfmt_misc registry is unavailable");
    sb_require(
        sb_syscall2(SB_SYS_fstatfs, directory_fd, (long)&filesystem),
        16,
        "binfmt_misc identity cannot be checked"
    );
    if ((unsigned long)filesystem.type != SB_BINFMTFS_MAGIC) {
        sb_fail(16, "executable-format registry is not binfmt_misc");
    }
#if defined(SB_TEST_BINFMT_FLAGS)
    {
        static const uint8_t synthetic_handler[] = "enabled\nflags: " SB_TEST_BINFMT_FLAGS "\n";
        if (sb_binfmt_value_is_persistent(synthetic_handler, sizeof(synthetic_handler) - 1U)) {
            sb_fail(16, "persistent binfmt_misc interpreters are forbidden");
        }
    }
#endif
    for (;;) {
        long count = sb_syscall3(
            SB_SYS_getdents64, directory_fd, (long)directory_buffer, sizeof(directory_buffer)
        );
        size_t offset = 0;
        if (count < 0) {
            sb_fail(16, "binfmt_misc registry cannot be enumerated");
        }
        if (count == 0) {
            break;
        }
        while (offset < (size_t)count) {
            struct sb_linux_dirent64 *entry =
                (struct sb_linux_dirent64 *)(directory_buffer + offset);
            size_t name_bytes;
            size_t name_index;
            int terminated = 0;
            int handler_fd;
            struct sb_identity handler_identity;
            if (entry->record_size < offsetof(struct sb_linux_dirent64, name) + 1U ||
                entry->record_size > (size_t)count - offset) {
                sb_fail(16, "binfmt_misc directory record is invalid");
            }
            name_bytes = entry->record_size - offsetof(struct sb_linux_dirent64, name);
            for (name_index = 0; name_index < name_bytes; name_index++) {
                if (entry->name[name_index] == 0) {
                    terminated = 1;
                    break;
                }
            }
            if (!terminated) {
                sb_fail(16, "binfmt_misc handler name is invalid");
            }
            offset += entry->record_size;
            if (sb_string_equal(entry->name, ".") || sb_string_equal(entry->name, "..") ||
                sb_string_equal(entry->name, "status") ||
                sb_string_equal(entry->name, "register")) {
                continue;
            }
            handler_fd = (int)sb_syscall4(
                SB_SYS_openat,
                directory_fd,
                (long)entry->name,
                SB_O_RDONLY | SB_O_NONBLOCK | SB_O_NOFOLLOW | SB_O_CLOEXEC,
                0
            );
            sb_require(handler_fd, 16, "binfmt_misc handler cannot be opened");
            handler_identity = sb_stat_fd(handler_fd);
            if ((handler_identity.mode & SB_S_IFMT) != SB_S_IFREG) {
                sb_fail(16, "binfmt_misc handler is not a regular procfs entry");
            }
            if (sb_binfmt_handler_is_persistent(handler_fd)) {
                sb_fail(16, "persistent binfmt_misc interpreters are forbidden");
            }
            sb_require(sb_syscall1(SB_SYS_close, handler_fd), 16, "binfmt_misc handler close failed");
        }
    }
    sb_require(sb_syscall1(SB_SYS_close, directory_fd), 16, "binfmt_misc registry close failed");
}

static void sb_require_no_initial_capabilities(void) {
    struct __user_cap_header_struct header;
    struct __user_cap_data_struct data[2];
    header.version = _LINUX_CAPABILITY_VERSION_3;
    header.pid = 0;
    data[0].effective = 0; data[0].permitted = 0; data[0].inheritable = 0;
    data[1].effective = 0; data[1].permitted = 0; data[1].inheritable = 0;
    sb_require(sb_syscall2(SB_SYS_capget, (long)&header, (long)&data), 17, "initial capabilities cannot be inspected");
    if (data[0].effective != 0U || data[0].permitted != 0U || data[0].inheritable != 0U ||
        data[1].effective != 0U || data[1].permitted != 0U || data[1].inheritable != 0U) {
        sb_fail(17, "initial capabilities are forbidden");
    }
}

static int sb_prepare_user_namespace(uint32_t uid, uint32_t gid) {
    struct sb_statfs filesystem;
    char pid_name[16];
    uint32_t pid = (uint32_t)sb_syscall1(SB_SYS_getpid, 0);
    int proc_fd = (int)sb_syscall4(
        SB_SYS_openat, SB_AT_FDCWD, (long)"/proc", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_CLOEXEC, 0
    );
    int proc_self_fd;
    int setgroups_fd;
    sb_require(proc_fd, 16, "authentic procfs is required");
    sb_require(sb_syscall2(SB_SYS_fstatfs, proc_fd, (long)&filesystem), 16, "procfs identity cannot be checked");
    if ((unsigned long)filesystem.type != SB_PROC_SUPER_MAGIC) {
        sb_fail(16, "identity map path is not procfs");
    }
    sb_reject_persistent_binfmt_handlers(proc_fd);
    sb_require_ptrace_guard(proc_fd);
    sb_require(sb_syscall1(SB_SYS_unshare, SB_CLONE_NEWUSER), 16, "unprivileged user namespace is unavailable");
    sb_require(sb_syscall5(SB_SYS_prctl, SB_PR_SET_DUMPABLE, 1, 0, 0, 0), 16, "identity map ownership cannot be enabled");
    pid_name[sb_decimal(pid, pid_name)] = 0;
    proc_self_fd = (int)sb_syscall4(
        SB_SYS_openat, proc_fd, (long)pid_name, SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0
    );
    sb_require(proc_self_fd, 16, "authentic procfs process directory is required");
    setgroups_fd = (int)sb_syscall4(
        SB_SYS_openat, proc_self_fd, (long)"setgroups", SB_O_WRONLY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0
    );
    if (setgroups_fd >= 0) {
        if (sb_syscall3(SB_SYS_write, setgroups_fd, (long)"deny\n", 5) != 5) {
            sb_fail(16, "setgroups policy cannot be sealed");
        }
        sb_require(sb_syscall1(SB_SYS_close, setgroups_fd), 16, "setgroups policy close failed");
    }
    sb_write_map(proc_self_fd, "uid_map", uid);
    sb_write_map(proc_self_fd, "gid_map", gid);
    sb_require(sb_syscall1(SB_SYS_close, proc_self_fd), 16, "procfs identity directory close failed");
    sb_require(sb_syscall1(SB_SYS_close, proc_fd), 16, "procfs close failed");
    sb_require(sb_syscall3(SB_SYS_setresgid, 0, 0, 0), 16, "sealed gid cannot be selected");
    sb_require(sb_syscall3(SB_SYS_setresuid, 0, 0, 0), 16, "sealed uid cannot be selected");
    sb_require(sb_syscall5(SB_SYS_prctl, SB_PR_SET_DUMPABLE, 0, 0, 0, 0), 16, "process inspection cannot be disabled after mapping");
    sb_require(sb_syscall1(SB_SYS_unshare, SB_CLONE_NEWNS), 16, "private mount namespace is unavailable");
    return 0;
}

static void sb_create_detached_root(int *mount_result, int *root_result) {
    int filesystem_fd = (int)sb_syscall2(SB_SYS_fsopen, (long)"tmpfs", SB_FSOPEN_CLOEXEC);
    int mount_fd;
    int root_fd;
    sb_require(filesystem_fd, 16, "detached tmpfs creation is unavailable");
    sb_require(sb_syscall5(SB_SYS_fsconfig, filesystem_fd, SB_FSCONFIG_CMD_CREATE, 0, 0, 0), 16, "detached tmpfs cannot be configured");
    mount_fd = (int)sb_syscall3(SB_SYS_fsmount, filesystem_fd, SB_FSMOUNT_CLOEXEC, 0);
    sb_require(mount_fd, 16, "detached tmpfs cannot be materialized");
    sb_require(sb_syscall1(SB_SYS_close, filesystem_fd), 16, "tmpfs context close failed");
    root_fd = (int)sb_syscall4(SB_SYS_openat, mount_fd, (long)".", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_CLOEXEC, 0);
    sb_require(root_fd, 16, "detached tmpfs root cannot be opened");
    *mount_result = mount_fd;
    *root_result = root_fd;
}

static int sb_open_directory(int parent_fd, const char *name) {
    int fd = (int)sb_syscall4(
        SB_SYS_openat, parent_fd, (long)name, SB_O_RDONLY | SB_O_DIRECTORY | SB_O_NOFOLLOW | SB_O_CLOEXEC, 0
    );
    struct sb_identity identity;
    sb_require(fd, 16, "sealed directory cannot be opened");
    identity = sb_stat_fd(fd);
    if ((identity.mode & SB_S_IFMT) != SB_S_IFDIR) {
        sb_fail(16, "sealed path component is not a directory");
    }
    return fd;
}

static int sb_open_or_create_directory(int parent_fd, const char *name) {
    long created = sb_syscall3(SB_SYS_mkdirat, parent_fd, (long)name, 0755);
    if (created < 0 && created != -SB_EEXIST) {
        sb_fail(16, "sealed directory cannot be created");
    }
    return sb_open_directory(parent_fd, name);
}

static int sb_destination_parent(int root_fd, const struct sb_string *path, char leaf[256]) {
    const char *cursor = path->value;
    char component[256];
    int current_fd = root_fd;
    sb_require_absolute(path);
    while (sb_copy_component(&cursor, component)) {
        const char *remaining = cursor;
        while (*remaining == '/') {
            remaining++;
        }
        if (*remaining == 0) {
            size_t index = 0;
            while (component[index] != 0) {
                leaf[index] = component[index];
                index++;
            }
            leaf[index] = 0;
            return current_fd;
        }
        {
            int next_fd = sb_open_or_create_directory(current_fd, component);
            if (current_fd != root_fd) {
                sb_require(sb_syscall1(SB_SYS_close, current_fd), 16, "sealed directory close failed");
            }
            current_fd = next_fd;
        }
    }
    sb_fail(16, "sealed destination path is invalid");
}

static void sb_create_cwd(int root_fd, const struct sb_string *path) {
    const char *cursor = path->value;
    char component[256];
    int current_fd = root_fd;
    sb_require_absolute(path);
    while (sb_copy_component(&cursor, component)) {
        int next_fd = sb_open_or_create_directory(current_fd, component);
        if (current_fd != root_fd) {
            sb_require(sb_syscall1(SB_SYS_close, current_fd), 16, "sealed cwd parent close failed");
        }
        current_fd = next_fd;
    }
    if (current_fd != root_fd) {
        sb_require(sb_syscall1(SB_SYS_close, current_fd), 16, "sealed cwd close failed");
    }
}

static void sb_copy_file(struct sb_file *file, int root_fd) {
    char leaf[256];
    uint8_t buffer[65536];
    uint64_t offset = 0;
    int parent_fd = sb_destination_parent(root_fd, &file->destination, leaf);
    file->destination_fd = (int)sb_syscall4(
        SB_SYS_openat,
        parent_fd,
        (long)leaf,
        SB_O_RDWR | SB_O_CREAT | SB_O_EXCL | SB_O_NOFOLLOW | SB_O_CLOEXEC,
        0600
    );
    sb_require(file->destination_fd, 16, "sealed file cannot be created");
    while (offset < file->expected.size_bytes) {
        size_t requested = file->expected.size_bytes - offset < sizeof(buffer) ?
            (size_t)(file->expected.size_bytes - offset) : sizeof(buffer);
        long count = sb_syscall4(SB_SYS_pread64, file->source_fd, (long)buffer, (long)requested, (long)offset);
        size_t written = 0;
        if (count <= 0) {
            sb_fail(14, "source changed while copied");
        }
        while (written < (size_t)count) {
            long output = sb_syscall3(
                SB_SYS_write,
                file->destination_fd,
                (long)(buffer + written),
                count - (long)written
            );
            if (output <= 0) {
                sb_fail(16, "sealed file write failed");
            }
            written += (size_t)output;
        }
        offset += (uint64_t)count;
    }
    sb_require(sb_syscall2(SB_SYS_fchmod, file->destination_fd, file->mode), 16, "sealed file mode cannot be set");
    sb_require(sb_syscall1(SB_SYS_fsync, file->destination_fd), 16, "sealed file cannot be synchronized");
    file->destination_identity = sb_stat_fd(file->destination_fd);
    sb_verify_hashed_fd(file->destination_fd, &file->destination_identity, file->digest);
    if (parent_fd != root_fd) {
        sb_require(sb_syscall1(SB_SYS_close, parent_fd), 16, "sealed file parent close failed");
    }
}

static void sb_materialize(struct sb_manifest *manifest, int root_fd) {
    uint32_t index;
    sb_create_cwd(root_fd, &manifest->cwd);
    for (index = 0; index < manifest->file_count; index++) {
        sb_copy_file(&manifest->files[index], root_fd);
    }
    sb_require(sb_syscall2(SB_SYS_fchmod, root_fd, 0555), 16, "sealed root mode cannot be set");
    sb_require(sb_syscall1(SB_SYS_fsync, root_fd), 16, "sealed root cannot be synchronized");
}

static void sb_seal_mount(int mount_fd) {
    struct mount_attr attributes;
    long result;
    size_t index;
    uint8_t *bytes = (uint8_t *)&attributes;
    for (index = 0; index < sizeof(attributes); index++) {
        bytes[index] = 0;
    }
    attributes.attr_set = MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV;
    result = sb_syscall5(
        SB_SYS_mount_setattr,
        mount_fd,
        (long)"",
        SB_AT_EMPTY_PATH | SB_AT_RECURSIVE,
        (long)&attributes,
        sizeof(attributes)
    );
    sb_require(result, 16, "detached runtime cannot be sealed read-only");
}

static void sb_close_destination_writers(struct sb_manifest *manifest) {
    uint32_t index;
    for (index = 0; index < manifest->file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        struct sb_identity current = sb_stat_fd(file->destination_fd);
        if (!sb_same_identity(&current, &file->destination_identity)) {
            sb_fail(16, "sealed destination identity drifted");
        }
        sb_verify_hashed_fd(file->destination_fd, &file->destination_identity, file->digest);
        sb_require(sb_syscall1(SB_SYS_close, file->destination_fd), 16, "sealed destination close failed");
        file->destination_fd = -1;
    }
}

static void sb_validate_destination(struct sb_manifest *manifest, int root_fd) {
    uint32_t index;
    for (index = 0; index < manifest->file_count; index++) {
        struct sb_file *file = &manifest->files[index];
        struct sb_identity current;
        file->destination_fd = (int)sb_syscall4(
            SB_SYS_openat,
            root_fd,
            (long)(file->destination.value + 1),
            SB_O_RDONLY | SB_O_NOFOLLOW | SB_O_CLOEXEC,
            0
        );
        sb_require(file->destination_fd, 16, "sealed destination cannot be reopened");
        current = sb_stat_fd(file->destination_fd);
        if (!sb_same_identity(&current, &file->destination_identity)) {
            sb_fail(16, "sealed destination identity drifted after mount seal");
        }
        sb_verify_hashed_fd(file->destination_fd, &file->destination_identity, file->digest);
    }
}

static void sb_reject_directory_stdio(void) {
    int fd;
    for (fd = 1; fd < 3; fd++) {
        struct sb_identity identity = sb_stat_fd(fd);
        uint64_t kind = identity.mode & SB_S_IFMT;
        if (kind != SB_S_IFCHR && kind != SB_S_IFIFO) {
            sb_fail(17, "inherited standard descriptor can expose executable storage");
        }
        if ((sb_syscall3(SB_SYS_fcntl, fd, SB_F_GETFL, 0) & SB_O_ACCMODE) != SB_O_WRONLY) {
            sb_fail(17, "standard output descriptors must be write-only");
        }
    }
}

static void sb_drop_capabilities(void) {
    struct __user_cap_header_struct header;
    struct __user_cap_data_struct data[2];
    int capability;
    int range_ended = 0;
    header.version = _LINUX_CAPABILITY_VERSION_3;
    header.pid = 0;
    data[0].effective = 0; data[0].permitted = 0; data[0].inheritable = 0;
    data[1].effective = 0; data[1].permitted = 0; data[1].inheritable = 0;
    sb_require(
        sb_syscall5(
            SB_SYS_prctl,
            SB_PR_SET_SECUREBITS,
            SB_SECBIT_NOROOT | SB_SECBIT_NOROOT_LOCKED | SB_SECBIT_NO_SETUID_FIXUP |
                SB_SECBIT_NO_SETUID_FIXUP_LOCKED | SB_SECBIT_KEEP_CAPS_LOCKED,
            0,
            0,
            0
        ),
        17,
        "secure identity policy cannot be installed"
    );
    for (capability = 0; capability < 256; capability++) {
        long result = sb_syscall5(SB_SYS_prctl, SB_PR_CAPBSET_DROP, capability, 0, 0, 0);
        if (result == -SB_EINVAL) {
            range_ended = 1;
            break;
        }
        sb_require(result, 17, "capability bounding set cannot be dropped");
    }
    if (!range_ended) {
        sb_fail(17, "kernel capability range exceeds the launcher limit");
    }
    sb_require(
        sb_syscall5(SB_SYS_prctl, SB_PR_CAP_AMBIENT, SB_PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0),
        17,
        "ambient capabilities cannot be cleared"
    );
    sb_require(sb_syscall2(SB_SYS_capset, (long)&header, (long)&data), 17, "capabilities cannot be dropped");
}

#define SB_DENY(number) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (number), 0, 1), \
    BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM)

static void sb_install_runtime_policy(void) {
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000U, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_KILL_PROCESS),
        SB_DENY(SB_SYS_execveat),
        SB_DENY(SB_SYS_memfd_create),
        SB_DENY(SB_SYS_memfd_secret),
        SB_DENY(SB_SYS_recvmsg),
        SB_DENY(SB_SYS_recvmmsg),
        SB_DENY(SB_SYS_socket),
        SB_DENY(SB_SYS_socketpair),
        SB_DENY(SB_SYS_connect),
        SB_DENY(SB_SYS_accept),
        SB_DENY(SB_SYS_accept4),
        SB_DENY(SB_SYS_bind),
        SB_DENY(SB_SYS_listen),
        SB_DENY(SB_SYS_sendto),
        SB_DENY(SB_SYS_recvfrom),
        SB_DENY(SB_SYS_sendmsg),
        SB_DENY(SB_SYS_pidfd_getfd),
        SB_DENY(SB_SYS_shmat),
        SB_DENY(SB_SYS_bpf),
        SB_DENY(SB_SYS_userfaultfd),
        SB_DENY(SB_SYS_io_uring_setup),
        SB_DENY(SB_SYS_io_uring_enter),
        SB_DENY(SB_SYS_io_uring_register),
        SB_DENY(SB_SYS_clone3),
        SB_DENY(SB_SYS_unshare),
        SB_DENY(SB_SYS_setns),
        SB_DENY(SB_SYS_fchdir),
        SB_DENY(SB_SYS_mount),
        SB_DENY(SB_SYS_umount2),
        SB_DENY(SB_SYS_pivot_root),
        SB_DENY(SB_SYS_chroot),
        SB_DENY(SB_SYS_open_by_handle_at),
        SB_DENY(SB_SYS_name_to_handle_at),
        SB_DENY(SB_SYS_move_mount),
        SB_DENY(SB_SYS_open_tree),
        SB_DENY(SB_SYS_fsopen),
        SB_DENY(SB_SYS_fsconfig),
        SB_DENY(SB_SYS_fsmount),
        SB_DENY(SB_SYS_mount_setattr),
        SB_DENY(SB_SYS_ptrace),
        SB_DENY(SB_SYS_prctl),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_SYS_mmap, 0, 7),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[2])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 4U, 0, 5),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 2U, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[3])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, SB_MAP_ANONYMOUS, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_SYS_mprotect, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[2])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 4U, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_SYS_pkey_mprotect, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[2])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 4U, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_SYS_clone, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x7e020080U, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ERRNO | SB_EPERM),
        BPF_STMT(BPF_RET | BPF_K, SB_SECCOMP_RET_ALLOW)
    };
    struct sock_fprog program;
    program.len = (unsigned short)(sizeof(instructions) / sizeof(instructions[0]));
    program.filter = instructions;
    sb_require(
        sb_syscall3(
            SB_SYS_seccomp,
            SB_SECCOMP_SET_MODE_FILTER,
            SB_SECCOMP_FILTER_FLAG_TSYNC,
            (long)&program
        ),
        17,
        "runtime syscall policy cannot be installed"
    );
}

static uint8_t *sb_load_manifest(const char *path, size_t *size_result, int *fd_result) {
    struct sb_identity expected;
    struct sb_identity before;
    struct sb_identity after;
    uint8_t expected_digest[32];
    uint8_t actual_digest[32];
    uint8_t *value;
    size_t offset = 0;
    int fd;
    if (path[0] != '/') {
        sb_fail(10, "manifest invocation path must be absolute");
    }
    fd = (int)sb_syscall4(
        SB_SYS_openat,
        SB_AT_FDCWD,
        (long)path,
        SB_O_RDONLY | SB_O_NONBLOCK | SB_O_NOFOLLOW | SB_O_CLOEXEC,
        0
    );
    sb_require(fd, 11, "sealed manifest cannot be opened");
    expected.device_major = SB_MANIFEST_DEV_MAJOR;
    expected.device_minor = SB_MANIFEST_DEV_MINOR;
    expected.inode = SB_MANIFEST_INODE;
    expected.mode = SB_MANIFEST_MODE;
    expected.uid = SB_MANIFEST_UID;
    expected.gid = SB_MANIFEST_GID;
    expected.link_count = SB_MANIFEST_NLINK;
    expected.size_bytes = SB_MANIFEST_SIZE;
    expected.modified_ns = SB_MANIFEST_MTIME_NS;
    expected.changed_ns = SB_MANIFEST_CTIME_NS;
    before = sb_stat_fd(fd);
    if (!sb_same_identity(&before, &expected) || (before.mode & SB_S_IFMT) != SB_S_IFREG ||
        before.size_bytes == 0 || before.size_bytes > SB_MAX_MANIFEST_BYTES) {
        sb_fail(11, "manifest descriptor identity drifted");
    }
    value = (uint8_t *)sb_syscall6(
        SB_SYS_mmap,
        0,
        (long)((before.size_bytes + SB_PAGE_BYTES - 1U) & ~(SB_PAGE_BYTES - 1U)),
        SB_PROT_READ | SB_PROT_WRITE,
        SB_MAP_PRIVATE | SB_MAP_ANONYMOUS,
        -1,
        0
    );
    if (value == SB_MAP_FAILED) {
        sb_fail(11, "manifest memory cannot be allocated");
    }
    while (offset < before.size_bytes) {
        long count = sb_syscall3(SB_SYS_read, fd, (long)(value + offset), (long)(before.size_bytes - offset));
        if (count <= 0) {
            sb_fail(11, "manifest read failed");
        }
        offset += (size_t)count;
    }
    after = sb_stat_fd(fd);
    if (!sb_same_identity(&before, &after)) {
        sb_fail(11, "manifest identity drifted while read");
    }
    sb_decode_expected_digest(expected_digest);
    sb_hash_memory(value, before.size_bytes, actual_digest);
    if (!sb_memory_equal(expected_digest, actual_digest, sizeof(actual_digest))) {
        sb_fail(11, "manifest authentication failed");
    }
    *size_result = (size_t)before.size_bytes;
    *fd_result = fd;
    sb_manifest_identity = expected;
    return value;
}

static void sb_validate_manifest_descriptor(void) {
    uint8_t expected_digest[32];
    uint8_t actual_digest[32];
    struct sb_identity before = sb_stat_fd(sb_manifest_fd);
    struct sb_identity after;
    if (!sb_same_mapped_identity(&before, &sb_manifest_identity) ||
        (before.mode & SB_S_IFMT) != SB_S_IFREG) {
        sb_fail(11, "manifest descriptor identity drifted after authentication");
    }
    sb_decode_expected_digest(expected_digest);
    sb_hash_fd(sb_manifest_fd, before.size_bytes, actual_digest);
    after = sb_stat_fd(sb_manifest_fd);
    if (!sb_same_mapped_identity(&after, &sb_manifest_identity) ||
        !sb_memory_equal(expected_digest, actual_digest, sizeof(actual_digest))) {
        sb_fail(11, "manifest descriptor authentication drifted after copy");
    }
}

__attribute__((noreturn)) static void sb_enter_runtime(
    struct sb_manifest *manifest,
    int mount_fd,
    int root_fd,
    uint32_t initial_uid,
    uint32_t initial_gid
) {
    struct sb_identity root_identity = sb_stat_fd(root_fd);
    struct sb_identity root_after;
    int root_now;
    int cwd_now;
    (void)initial_uid;
    (void)initial_gid;
    sb_require(sb_syscall1(SB_SYS_fchdir, root_fd), 17, "sealed root cannot become current");
    sb_require(sb_syscall1(SB_SYS_chroot, (long)"."), 17, "sealed root cannot become process root");
    sb_require(sb_syscall1(SB_SYS_chdir, (long)manifest->cwd.value), 17, "sealed cwd cannot be selected");
    root_now = (int)sb_syscall4(SB_SYS_openat, SB_AT_FDCWD, (long)"/", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_CLOEXEC, 0);
    sb_require(root_now, 17, "sealed process root cannot be verified");
    root_after = sb_stat_fd(root_now);
    if (!sb_same_object(&root_identity, &root_after)) {
        sb_fail(17, "sealed process root identity drifted");
    }
    cwd_now = (int)sb_syscall4(SB_SYS_openat, SB_AT_FDCWD, (long)".", SB_O_RDONLY | SB_O_DIRECTORY | SB_O_CLOEXEC, 0);
    sb_require(cwd_now, 17, "sealed process cwd cannot be verified");
    sb_reject_directory_stdio();
    sb_drop_capabilities();
    sb_install_runtime_policy();
    sb_test_pause(5);
    sb_require(sb_syscall1(SB_SYS_close, 0), 17, "ambient standard input cannot be closed");
    sb_require(sb_syscall3(SB_SYS_close_range, 3, ~0U, SB_CLOSE_RANGE_UNSHARE), 17, "inherited descriptors cannot be closed");
    (void)mount_fd;
    sb_syscall3(SB_SYS_execve, (long)manifest->executable.value, (long)manifest->args, (long)manifest->environment);
    sb_fail(18, "sealed interpreter execution failed");
}

__attribute__((used, noreturn)) static void sb_main(uint64_t *stack) {
    uint64_t argc = stack[0];
    char **argv = (char **)&stack[1];
    char **ambient = &argv[argc + 1U];
    uint32_t uid = (uint32_t)sb_syscall1(SB_SYS_getuid, 0);
    uint32_t gid = (uint32_t)sb_syscall1(SB_SYS_getgid, 0);
    uint32_t euid = (uint32_t)sb_syscall1(SB_SYS_geteuid, 0);
    uint32_t egid = (uint32_t)sb_syscall1(SB_SYS_getegid, 0);
    uint8_t *manifest_value;
    size_t manifest_size;
    int manifest_fd;
    int mount_fd;
    int root_fd;
    if (argc != 2U) {
        sb_fail(10, "usage: sealed-launcher ABSOLUTE_MANIFEST");
    }
    if (uid != euid || gid != egid) {
        sb_fail(17, "elevated or drifting process identity is forbidden");
    }
    sb_require_no_initial_capabilities();
    sb_require(sb_syscall5(SB_SYS_prctl, SB_PR_SET_DUMPABLE, 0, 0, 0, 0), 17, "process inspection cannot be disabled");
    sb_require(sb_syscall5(SB_SYS_prctl, SB_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), 17, "no-new-privileges cannot be installed");
    manifest_value = sb_load_manifest(argv[1], &manifest_size, &manifest_fd);
    sb_manifest_fd = manifest_fd;
    sb_test_pause(1);
    sb_parse_manifest(manifest_value, manifest_size, &sb_manifest);
    sb_validate_environment(ambient, &sb_manifest);
    sb_validate_paths(&sb_manifest);
    sb_bind_sources(&sb_manifest);
    sb_prepare_user_namespace(uid, gid);
    sb_create_detached_root(&mount_fd, &root_fd);
    sb_syscall1(SB_SYS_umask, 0);
    sb_materialize(&sb_manifest, root_fd);
    sb_test_pause(4);
    sb_validate_manifest_descriptor();
    sb_validate_sources(&sb_manifest);
    sb_close_destination_writers(&sb_manifest);
    sb_seal_mount(mount_fd);
    sb_validate_destination(&sb_manifest, root_fd);
    sb_enter_runtime(&sb_manifest, mount_fd, root_fd, uid, gid);
}

__attribute__((naked, noreturn, section(".text.start"))) void _start(void) {
    __asm__ volatile(
        "mov %rsp, %rdi\n"
        "and $-16, %rsp\n"
        "call sb_main\n"
    );
}
