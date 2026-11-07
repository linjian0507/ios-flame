#!/usr/bin/env python3
"""Compile and exercise the reset classifier without a phone or Apple SDK.

The stub enum models the IOPlatformExpert interface. This is a policy/unit
test, not an XNU build or a hardware reset test.
"""
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

NAMES = (
    "kPEHaltCPU", "kPERestartCPU", "kPEHangCPU", "kPEUPSDelayHaltCPU",
    "kPEPanicRestartCPU", "kPEPanicSync", "kPEPagingOff", "kPEPanicBegin",
    "kPEPanicEnd", "kPEPanicRestartCPUNoCallouts", "kPEPanicDiagnosticsDone",
    "kPEPanicDiagnosticsInProgress",
)
TERMINAL = set(NAMES[:5]) | {"kPEPanicRestartCPUNoCallouts"}


def check_sources(root):
    pe = (root / "iokit/Kernel/IOPlatformExpert.cpp").read_text()
    shutdown = (root / "bsd/kern/kern_shutdown.c").read_text()
    debug = (root / "osfmk/kern/debug.c").read_text()
    platform = (root / "iokit/Kernel/arm/SweetPlatformExpert.cpp").read_text()
    assert "sweet_reset_diag_should_stop(type)" in pe
    assert "if (type != kPEUPSDelayHaltCPU)" not in pe
    assert '"SWEET HALT: PEHaltRestartInternal' not in pe
    assert "int sweet_reset_diag_enabled = 0;" in debug
    assert 'PE_parse_boot_argn("sweetresetdiag"' in debug
    assert "if (sweet_reset_diag_enabled) {\n\t\tpanicDebugging = TRUE;" in debug
    start = shutdown.index("reboot_kernel(int howto, char *message)")
    diag = shutdown[start:shutdown.index("#endif", start)]
    assert "proc_selfpid()" in diag and "proc_selfname" in diag
    assert "kdb_printf_unbuffered" in diag
    assert "sync(" not in diag and '"wfe"' not in diag
    assert "if ((howto & (RB_PANIC | RB_QUICK)) != (RB_PANIC | RB_QUICK))" in diag
    assert 'panic("SWEET RESET DIAG: reboot pid=' in diag
    assert 'panic("SWEET RESET DIAG: intentional self-test panic")' in platform
    assert 'PE_parse_boot_argn("sweetdiagpanic"' in platform


def run(root):
    check_sources(root)
    clang = shutil.which("clang")
    if not clang:
        raise SystemExit("clang is required for the classifier test")
    header = root / "iokit/IOKit/SweetResetDiagnostics.h"
    checks = 0
    with tempfile.TemporaryDirectory(prefix="reset-policy-") as tmp:
        tmp = Path(tmp)
        kit = tmp / "IOKit"
        kit.mkdir()
        shutil.copyfile(header, kit / header.name)
        for permuted in (False, True):
            # Also permute the interface values: classification must use names,
            # not the incorrect magic numbers from the previous diagnostic.
            values = [100 + i * 7 for i in range(len(NAMES))] if permuted else range(len(NAMES))
            enum = "enum {\n" + ",\n".join(f"{name} = {value}" for name, value in zip(NAMES, values)) + "\n};\n"
            (kit / "IOPlatformExpert.h").write_text(enum)
            for language, suffix, standard in (("c", "c", "c11"), ("c++", "cpp", "c++17")):
                lines = ['#include <IOKit/SweetResetDiagnostics.h>', 'int sweet_reset_diag_enabled = 0;', 'int main(void) {']
                for enabled in (0, 1):
                    lines.append(f"sweet_reset_diag_enabled = {enabled};")
                    for i, name in enumerate(NAMES):
                        expected = int(bool(enabled) and name in TERMINAL)
                        lines.append(f"if (sweet_reset_diag_should_stop({name}) != {expected}) return {i + 1};")
                        checks += 1
                    lines.append("if (sweet_reset_diag_should_stop(0xdead)) return 99;")
                    checks += 1
                lines.append("return 0; }")
                source = tmp / ("test." + suffix)
                source.write_text("\n".join(lines))
                binary = tmp / "test-bin"
                subprocess.run([clang, "-x", language, "-std=" + standard, "-Wall", "-Wextra", "-Werror", "-I", str(tmp), str(source), "-o", str(binary)], check=True)
                subprocess.run([str(binary)], check=True)
    print(f"PASS: source contract and {checks} compiled policy checks (C/C++, normal/permuted enums)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("work/dxb/xnu"))
    run(parser.parse_args().source)
