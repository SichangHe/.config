from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from lifecycle_platform._bindings import Identity, digest_fd
from lifecycle_platform.sealed_bootstrap import (
    BootstrapSpec,
    EnvironmentEntry,
    FileSpec,
    ManifestSeal,
    RootSpec,
    _build_launcher,
    _ProvisionPhase,
    build_launcher,
    create_manifest,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "runtime_probe.c"
_EXTENSION_FIXTURE = Path(__file__).parent / "fixtures" / "runtime_extension.c"
_INTERPRETER_PATTERN = re.compile(r"Requesting program interpreter: ([^]]+)")


@dataclass(frozen=True, slots=True)
class Runtime:
    base: Path
    launch_directory: Path
    manifest: ManifestSeal
    launcher: Path
    source_paths: tuple[Path, ...]
    destination_paths: tuple[PurePosixPath, ...]


def _dependencies(binary: Path) -> tuple[tuple[Path, PurePosixPath], ...]:
    program_headers = subprocess.run(
        ["/usr/bin/readelf", "-l", binary],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    match = _INTERPRETER_PATTERN.search(program_headers)
    assert match is not None
    interpreter_destination = PurePosixPath(match.group(1))
    entries: dict[PurePosixPath, Path] = {
        interpreter_destination: Path(interpreter_destination).resolve(strict=True)
    }
    output = subprocess.run(
        ["/usr/bin/ldd", binary],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={"LC_ALL": "C"},
    ).stdout
    for line in output.splitlines():
        words = line.strip().split()
        candidate = words[2] if len(words) >= 3 and words[1] == "=>" else words[0]
        if candidate.startswith("/"):
            destination = PurePosixPath(candidate)
            entries[destination] = Path(candidate).resolve(strict=True)
    return tuple((source, destination) for destination, source in entries.items())


def _runtime(
    tmp_path: Path,
    *,
    argument: str = "module",
    include_child: bool = True,
    pause_phase: int | None = None,
    pause_file_index: int = 0,
    test_binfmt_flags: str | None = None,
) -> Runtime:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source-ancestor" / "sources"
    artifacts = tmp_path / "artifacts"
    launch_directory = tmp_path / "launch-ancestor" / "launch"
    source.mkdir(parents=True)
    artifacts.mkdir()
    launch_directory.mkdir(parents=True)
    binary = source / "main"
    subprocess.run(
        [
            "/usr/bin/gcc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            _FIXTURE,
            "-ldl",
            "-o",
            binary,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    closure_dir = source / "closure"
    closure_dir.mkdir()
    sources: list[Path] = [binary]
    destinations: list[PurePosixPath] = [PurePosixPath("/bin/main")]
    for index, (dependency, destination) in enumerate(_dependencies(binary)):
        copied = closure_dir / f"dependency-{index}"
        shutil.copyfile(dependency, copied)
        sources.append(copied)
        destinations.append(destination)
    module = source / "module.so"
    startup = source / "startup.so"
    child_data = source / "child.dat"
    blocked_data = source / "blocked.dat"
    for extension, value in (
        (module, "module-original\\n"),
        (startup, "startup-original\\n"),
    ):
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                f'-DEXTENSION_VALUE="{value}"',
                _EXTENSION_FIXTURE,
                "-o",
                extension,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    child_data.write_text("child-original\n")
    blocked_data.write_text("blocked\n")
    sources.extend((module, startup, child_data, blocked_data))
    destinations.extend(
        (
            PurePosixPath("/app/module.so"),
            PurePosixPath("/app/startup.so"),
            PurePosixPath("/app/child.dat"),
            PurePosixPath("/app/blocked.dat"),
        )
    )
    if include_child:
        sources.append(binary)
        destinations.append(PurePosixPath("/bin/child"))
    roots: list[RootSpec] = []
    root_indexes: dict[Path, int] = {}
    files: list[FileSpec] = []
    for item, destination in zip(sources, destinations, strict=True):
        parent = item.parent
        if parent not in root_indexes:
            root_indexes[parent] = len(roots)
            roots.append(RootSpec(parent))
        mode = (
            0o555
            if destination in (PurePosixPath("/bin/main"), PurePosixPath("/bin/child"))
            or "ld-linux" in destination.name
            else 0o444
        )
        files.append(FileSpec(root_indexes[parent], PurePosixPath(item.name), destination, mode))
    spec = BootstrapSpec(
        launch_directory=launch_directory,
        roots=tuple(roots),
        files=tuple(files),
        executable=PurePosixPath("/bin/main"),
        cwd=PurePosixPath("/work"),
        argv=("/bin/main", argument),
        environment=(EnvironmentEntry("SEALED", "yes"),),
    )
    manifest = create_manifest(spec, artifacts / "manifest")
    launcher = artifacts / "launcher"
    if pause_phase is None and test_binfmt_flags is None:
        build_launcher(manifest, launcher)
    else:
        _build_launcher(
            manifest,
            launcher,
            test_pause_phase=pause_phase,
            test_pause_file_index=pause_file_index,
            test_binfmt_flags=test_binfmt_flags,
        )
    return Runtime(
        tmp_path, launch_directory, manifest, launcher, tuple(sources), tuple(destinations)
    )


def _run(
    runtime: Runtime, *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [runtime.launcher, runtime.manifest.path],
        cwd=runtime.launch_directory,
        env={} if environment is None else environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _paused(runtime: Runtime) -> tuple[subprocess.Popen[str], int, int]:
    notify_read, notify_write = os.pipe()
    resume_read, resume_write = os.pipe()
    os.dup2(notify_write, 198, inheritable=True)
    os.dup2(resume_read, 199, inheritable=True)
    try:
        process = subprocess.Popen(
            [runtime.launcher, runtime.manifest.path],
            cwd=runtime.launch_directory,
            env={},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(198, 199),
        )
    finally:
        os.close(198)
        os.close(199)
        os.close(notify_write)
        os.close(resume_read)
    notification = os.read(notify_read, 1)
    if not notification:
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"launcher did not pause: {process.returncode=} {stdout=} {stderr=}")
    return process, notify_read, resume_write


def _resume(
    process: subprocess.Popen[str], notify_read: int, resume_write: int
) -> tuple[int, str, str]:
    os.write(resume_write, b"x")
    os.close(resume_write)
    os.close(notify_read)
    stdout, stderr = process.communicate(timeout=20)
    return process.returncode, stdout, stderr


def _minimal_spec(tmp_path: Path) -> BootstrapSpec:
    source = tmp_path / "source"
    launch_directory = tmp_path / "launch"
    source.mkdir(parents=True)
    launch_directory.mkdir()
    shutil.copyfile("/usr/bin/true", source / "main")
    return BootstrapSpec(
        launch_directory=launch_directory,
        roots=(RootSpec(source),),
        files=(FileSpec(0, PurePosixPath("main"), PurePosixPath("/bin/main"), 0o555),),
        executable=PurePosixPath("/bin/main"),
        cwd=PurePosixPath("/work"),
        argv=("/bin/main",),
    )


def test_sealed_dynamic_interpreter_dependency_and_module_closure(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    result = _run(runtime)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "module-original\n"


def test_sealed_subprocess_runtime_executes_and_missing_runtime_fails_closed(
    tmp_path: Path,
) -> None:
    allowed = _runtime(tmp_path / "allowed", argument="child")
    missing = _runtime(tmp_path / "missing", argument="missing", include_child=False)

    allowed_result = _run(allowed)
    missing_result = _run(missing)

    assert allowed_result.returncode == 0, allowed_result.stderr
    assert allowed_result.stdout == "child-original\n"
    assert missing_result.returncode == 41
    assert missing_result.stdout == ""


def test_environment_and_startup_hook_injection_fail_before_interpreter(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    ordinary = _run(runtime, environment={"UNTRUSTED": "1"})
    startup = _run(runtime, environment={"PYTHONSTARTUP": "/tmp/hook"})
    loader = _run(runtime, environment={"LD_PRELOAD": "/tmp/loader"})

    assert ordinary.returncode == 17
    assert startup.returncode == 17
    assert loader.returncode == 17
    assert ordinary.stdout == startup.stdout == loader.stdout == ""


def test_readable_output_descriptor_fails_before_interpreter(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output_fd = os.open("/dev/null", os.O_RDWR | os.O_CLOEXEC)
    try:
        result = subprocess.run(
            [runtime.launcher, runtime.manifest.path],
            cwd=runtime.launch_directory,
            env={},
            stdin=subprocess.DEVNULL,
            stdout=output_fd,
            stderr=output_fd,
            check=False,
            timeout=20,
        )
    finally:
        os.close(output_fd)

    assert result.returncode == 17


def test_read_only_root_and_descriptor_execution_escape_are_blocked(tmp_path: Path) -> None:
    write_runtime = _runtime(tmp_path / "write", argument="write")
    memfd_runtime = _runtime(tmp_path / "memfd", argument="memfd")
    anonymous_runtime = _runtime(tmp_path / "anonymous", argument="anonexec")
    mprotect_runtime = _runtime(tmp_path / "mprotect", argument="mprotect")
    file_wx_runtime = _runtime(tmp_path / "file-wx", argument="filewx")
    socket_runtime = _runtime(tmp_path / "socket", argument="socket")

    write_result = _run(write_runtime)
    memfd_result = _run(memfd_runtime)
    anonymous_result = _run(anonymous_runtime)
    mprotect_result = _run(mprotect_runtime)
    file_wx_result = _run(file_wx_runtime)
    socket_result = _run(socket_runtime)

    assert write_result.returncode == 0, write_result.stderr
    assert memfd_result.returncode == 0, memfd_result.stderr
    assert anonymous_result.returncode == 0, anonymous_result.stderr
    assert mprotect_result.returncode == 0, mprotect_result.stderr
    assert file_wx_result.returncode == 0, file_wx_result.stderr
    assert socket_result.returncode == 0, socket_result.stderr
    assert (
        write_result.stdout
        == memfd_result.stdout
        == anonymous_result.stdout
        == mprotect_result.stdout
        == file_wx_result.stdout
        == socket_result.stdout
        == "blocked\n"
    )


def test_runtime_cannot_reenable_dumpability_or_ptrace_permission(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, argument="prctl")

    result = _run(runtime)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "blocked\n"


def test_combined_persistent_binfmt_flags_fail_before_interpreter(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, test_binfmt_flags="CF")

    result = _run(runtime)

    assert result.returncode == 16
    assert result.stdout == ""
    assert "persistent binfmt_misc" in result.stderr


@pytest.mark.parametrize(
    "target_kind", ["interpreter", "dependency", "loader", "module", "startup"]
)
def test_in_place_pre_copy_drift_fails_before_interpreter(tmp_path: Path, target_kind: str) -> None:
    runtime = _runtime(tmp_path, pause_phase=3)
    indexes = {
        "interpreter": 0,
        "dependency": next(
            index
            for index, destination in enumerate(runtime.destination_paths)
            if destination.name.startswith("libc.so")
        ),
        "loader": next(
            index
            for index, destination in enumerate(runtime.destination_paths)
            if "ld-linux" in destination.name
        ),
        "module": runtime.destination_paths.index(PurePosixPath("/app/module.so")),
        "startup": runtime.destination_paths.index(PurePosixPath("/app/startup.so")),
    }
    if indexes[target_kind] != 0:
        runtime = _runtime(
            tmp_path / target_kind, pause_phase=3, pause_file_index=indexes[target_kind]
        )
    process, notify, resume = _paused(runtime)
    target = runtime.source_paths[indexes[target_kind]]
    target.write_bytes(b"drift".ljust(target.stat().st_size, b"x"))

    returncode, stdout, stderr = _resume(process, notify, resume)

    assert returncode == 14
    assert stdout == ""
    assert "drifted" in stderr


def test_final_source_replacement_after_binding_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pause_phase=3)
    process, notify, resume = _paused(runtime)
    source = runtime.source_paths[0]
    displaced = source.with_name("displaced")
    source.rename(displaced)
    shutil.copyfile(displaced, source)

    returncode, stdout, _stderr = _resume(process, notify, resume)

    assert returncode == 14
    assert stdout == ""


def test_fifo_source_replacement_fails_closed_without_blocking(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pause_phase=2)
    process, notify, resume = _paused(runtime)
    source = runtime.source_paths[0]
    source.rename(source.with_name("retained-main"))
    os.mkfifo(source)

    returncode, stdout, stderr = _resume(process, notify, resume)

    assert returncode == 14
    assert stdout == ""
    assert "identity drifted" in stderr


def test_source_and_current_directory_final_component_swaps_fail_closed(tmp_path: Path) -> None:
    root_runtime = _runtime(tmp_path / "root", pause_phase=2)
    root_process, root_notify, root_resume = _paused(root_runtime)
    source_root = root_runtime.source_paths[0].parent
    source_root.rename(source_root.with_name("sources-retained"))
    source_root.mkdir()
    root_result = _resume(root_process, root_notify, root_resume)

    cwd_runtime = _runtime(tmp_path / "cwd", pause_phase=2)
    cwd_process, cwd_notify, cwd_resume = _paused(cwd_runtime)
    cwd_runtime.launch_directory.rename(cwd_runtime.launch_directory.with_name("launch-retained"))
    cwd_runtime.launch_directory.mkdir()
    cwd_result = _resume(cwd_process, cwd_notify, cwd_resume)

    assert root_result[0] in (14, 15)
    assert cwd_result[0] == 15
    assert root_result[1] == cwd_result[1] == ""


def test_source_and_current_directory_ancestor_swaps_fail_closed(tmp_path: Path) -> None:
    root_runtime = _runtime(tmp_path / "root", pause_phase=2)
    root_process, root_notify, root_resume = _paused(root_runtime)
    source_ancestor = root_runtime.source_paths[0].parent.parent
    source_ancestor.rename(source_ancestor.with_name("source-ancestor-retained"))
    source_ancestor.mkdir()
    (source_ancestor / "sources").mkdir()
    root_result = _resume(root_process, root_notify, root_resume)

    cwd_runtime = _runtime(tmp_path / "cwd", pause_phase=2)
    cwd_process, cwd_notify, cwd_resume = _paused(cwd_runtime)
    cwd_ancestor = cwd_runtime.launch_directory.parent
    cwd_ancestor.rename(cwd_ancestor.with_name("launch-ancestor-retained"))
    cwd_ancestor.mkdir()
    cwd_runtime.launch_directory.mkdir()
    cwd_result = _resume(cwd_process, cwd_notify, cwd_resume)

    assert root_result[0] in (14, 15)
    assert cwd_result[0] == 15
    assert root_result[1] == cwd_result[1] == ""


def test_wrong_initial_current_directory_fails_before_interpreter(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    result = subprocess.run(
        [runtime.launcher, runtime.manifest.path],
        cwd=runtime.base,
        env={},
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 15
    assert result.stdout == ""


def test_manifest_identity_and_content_substitutions_fail_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original = runtime.manifest.path.with_name("original-manifest")
    runtime.manifest.path.rename(original)
    shutil.copyfile(original, runtime.manifest.path)

    result = _run(runtime)

    assert result.returncode == 11
    assert result.stdout == ""


def test_fifo_manifest_replacement_fails_closed_without_blocking(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.manifest.path.rename(runtime.manifest.path.with_name("retained-manifest"))
    os.mkfifo(runtime.manifest.path)

    result = _run(runtime)

    assert result.returncode == 11
    assert result.stdout == ""


def test_manifest_path_replacement_after_binding_fails_on_retained_identity_drift(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, pause_phase=1)
    process, notify, resume = _paused(runtime)
    original = runtime.manifest.path.with_name("bound-manifest")
    runtime.manifest.path.rename(original)
    runtime.manifest.path.write_bytes(b"replacement")

    returncode, stdout, stderr = _resume(process, notify, resume)

    assert returncode == 11
    assert stdout == ""
    assert "manifest descriptor" in stderr


def test_in_place_manifest_drift_after_binding_fails_before_interpreter(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pause_phase=1)
    process, notify, resume = _paused(runtime)
    original_size = runtime.manifest.path.stat().st_size
    runtime.manifest.path.chmod(0o600)
    runtime.manifest.path.write_bytes(b"x" * original_size)

    returncode, stdout, stderr = _resume(process, notify, resume)

    assert returncode == 11
    assert stdout == ""
    assert "manifest descriptor" in stderr


def test_runtime_source_substitution_before_seal_fails_but_after_seal_is_irrelevant(
    tmp_path: Path,
) -> None:
    child_index = -1
    before = _runtime(tmp_path / "before", argument="child", pause_phase=4)
    child_index = before.destination_paths.index(PurePosixPath("/bin/child"))
    before_process, before_notify, before_resume = _paused(before)
    before.source_paths[child_index].write_bytes(
        b"x" * before.source_paths[child_index].stat().st_size
    )
    before_result = _resume(before_process, before_notify, before_resume)

    after = _runtime(tmp_path / "after", argument="child", pause_phase=5)
    child_index = after.destination_paths.index(PurePosixPath("/bin/child"))
    after_process, after_notify, after_resume = _paused(after)
    after.source_paths[child_index].write_bytes(
        b"x" * after.source_paths[child_index].stat().st_size
    )
    after_result = _resume(after_process, after_notify, after_resume)

    assert before_result[0] == 14
    assert before_result[1] == ""
    assert after_result[0] == 0, after_result[2]
    assert after_result[1] == "child-original\n"


def test_manifest_builder_rejects_loader_startup_environment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "main"
    binary.write_bytes(b"not executed")
    binary.chmod(0o755)
    spec = BootstrapSpec(
        launch_directory=tmp_path,
        roots=(RootSpec(source),),
        files=(FileSpec(0, PurePosixPath("main"), PurePosixPath("/bin/main"), 0o555),),
        executable=PurePosixPath("/bin/main"),
        cwd=PurePosixPath("/work"),
        argv=("/bin/main",),
        environment=(EnvironmentEntry("LD_PRELOAD", "/app/module"),),
    )

    with pytest.raises(ValueError, match="forbidden"):
        create_manifest(spec, tmp_path / "manifest")


def test_manifest_builder_rejects_glibc_loader_tunables(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "main"
    binary.write_bytes(b"not executed")
    binary.chmod(0o755)
    spec = BootstrapSpec(
        launch_directory=tmp_path,
        roots=(RootSpec(source),),
        files=(FileSpec(0, PurePosixPath("main"), PurePosixPath("/bin/main"), 0o555),),
        executable=PurePosixPath("/bin/main"),
        cwd=PurePosixPath("/work"),
        argv=("/bin/main",),
        environment=(EnvironmentEntry("GLIBC_TUNABLES", "glibc.rtld.optional_static_tls=1"),),
    )

    with pytest.raises(ValueError, match="forbidden"):
        create_manifest(spec, tmp_path / "manifest")


def test_manifest_builder_rejects_non_elf_execute_bit_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main").write_bytes(b"#!/bin/sh\nexit 0\n")
    spec = BootstrapSpec(
        launch_directory=tmp_path,
        roots=(RootSpec(source),),
        files=(FileSpec(0, PurePosixPath("main"), PurePosixPath("/bin/main"), 0o555),),
        executable=PurePosixPath("/bin/main"),
        cwd=PurePosixPath("/work"),
        argv=("/bin/main",),
    )

    with pytest.raises(ValueError, match="x86-64 ELF"):
        create_manifest(spec, tmp_path / "manifest")


def test_provisioning_rejects_relative_output_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _minimal_spec(tmp_path / "runtime")
    manifest = create_manifest(spec, tmp_path / "manifest")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="absolute path"):
        create_manifest(spec, Path("relative-manifest"))
    with pytest.raises(ValueError, match="absolute path"):
        _build_launcher(manifest, Path("relative-launcher"))

    assert not (tmp_path / "relative-manifest").exists()
    assert not (tmp_path / "relative-launcher").exists()


def test_manifest_publication_ancestor_swap_preserves_public_replacement(
    tmp_path: Path,
) -> None:
    spec = _minimal_spec(tmp_path / "runtime")
    ancestor = tmp_path / "output-ancestor"
    parent = ancestor / "artifacts"
    retained = tmp_path / "retained-output-ancestor"
    parent.mkdir(parents=True)
    output = parent / "manifest"

    def swap(phase: _ProvisionPhase) -> None:
        if phase is _ProvisionPhase.MANIFEST_OUTPUT_BOUND:
            ancestor.rename(retained)
            parent.mkdir(parents=True)
            output.write_bytes(b"public replacement\n")

    with pytest.raises(OSError, match="output binding drifted"):
        create_manifest(spec, output, _phase_hook=swap)

    assert output.read_bytes() == b"public replacement\n"
    assert (retained / "artifacts" / "manifest").exists()


def test_manifest_publication_final_swap_preserves_public_replacement(tmp_path: Path) -> None:
    spec = _minimal_spec(tmp_path / "runtime")
    parent = tmp_path / "artifacts"
    parent.mkdir()
    output = parent / "manifest"
    retained = parent / "retained-manifest"

    def swap(phase: _ProvisionPhase) -> None:
        if phase is _ProvisionPhase.MANIFEST_OUTPUT_DURABLE:
            output.rename(retained)
            output.write_bytes(b"public replacement\n")

    with pytest.raises(OSError, match="output binding drifted"):
        create_manifest(spec, output, _phase_hook=swap)

    assert output.read_bytes() == b"public replacement\n"
    assert retained.stat().st_mode & 0o777 == 0o400


def test_launcher_publication_ancestor_swap_preserves_public_replacement(
    tmp_path: Path,
) -> None:
    spec = _minimal_spec(tmp_path / "runtime")
    manifest_parent = tmp_path / "manifest-artifacts"
    manifest_parent.mkdir()
    manifest = create_manifest(spec, manifest_parent / "manifest")
    ancestor = tmp_path / "output-ancestor"
    parent = ancestor / "artifacts"
    retained = tmp_path / "retained-output-ancestor"
    parent.mkdir(parents=True)
    output = parent / "launcher"

    def swap(phase: _ProvisionPhase) -> None:
        if phase is _ProvisionPhase.LAUNCHER_OUTPUT_BOUND:
            ancestor.rename(retained)
            parent.mkdir(parents=True)
            output.write_bytes(b"public replacement\n")

    with pytest.raises(OSError, match="output binding drifted"):
        _build_launcher(manifest, output, _phase_hook=swap)

    assert output.read_bytes() == b"public replacement\n"
    assert (retained / "artifacts" / "launcher").exists()


def test_launcher_publication_final_swap_preserves_public_replacement(tmp_path: Path) -> None:
    spec = _minimal_spec(tmp_path / "runtime")
    manifest_parent = tmp_path / "manifest-artifacts"
    manifest_parent.mkdir()
    manifest = create_manifest(spec, manifest_parent / "manifest")
    parent = tmp_path / "launcher-artifacts"
    parent.mkdir()
    output = parent / "launcher"
    retained = parent / "retained-launcher"

    def swap(phase: _ProvisionPhase) -> None:
        if phase is _ProvisionPhase.LAUNCHER_OUTPUT_DURABLE:
            output.rename(retained)
            output.write_bytes(b"public replacement\n")

    with pytest.raises(OSError, match="output binding drifted"):
        _build_launcher(manifest, output, _phase_hook=swap)

    assert output.read_bytes() == b"public replacement\n"
    assert retained.stat().st_mode & 0o777 == 0o500


def test_launcher_seal_is_static_and_bound_to_manifest_identity(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fd = os.open(runtime.launcher, os.O_RDONLY | os.O_CLOEXEC)
    try:
        launcher_identity = Identity.from_fd(fd)
        launcher_digest = digest_fd(fd).hex()
    finally:
        os.close(fd)
    forged = ManifestSeal(
        runtime.manifest.path,
        "0" * 64,
        runtime.manifest.identity,
    )

    assert launcher_identity.mode & 0o777 == 0o500
    assert len(launcher_digest) == 64
    with pytest.raises(OSError, match="manifest seal"):
        build_launcher(forged, tmp_path / "forged-launcher")
