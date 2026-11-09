"""Offline checks for the PCLM10 PMIC/display diagnostic patch.

The compiled host harnesses exercise the C read allowlist and the actual C++
pixel comparison routine with fake buffers; they do not simulate phone MMIO.
Run after generating 0161 and before publishing any image.
"""
import os
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import hashlib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "references/ios-flame-fork/work/dxb/xnu"
PATCH = ROOT / "references/ios-flame-fork/xnu/patches/0187-flame-pclm10-ufs-lun.patch"
SOURCE_FLAG = "--source" in sys.argv


def extract_function(source: str, name: str) -> str:
    """Return an actual source function including its return-type line."""
    at = source.index("\n" + name + "(") + 1
    start = source.rfind("\n", 0, at - 1) + 1
    left = source.index("{", at)
    depth = 0
    for i in range(left, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise ValueError(f"unclosed function {name}")


def source_root() -> Path:
    """Use --source for CI's applied XNU tree; default to the local checkout."""
    import sys
    if "--source" in sys.argv:
        index = sys.argv.index("--source")
        source = Path(sys.argv[index + 1]).resolve()
        del sys.argv[index:index + 2]
        return source
    return SOURCE


ACTIVE_SOURCE = source_root()


def compile_and_run(cpp: bool, code: str) -> None:
    compiler = shutil.which("clang++" if cpp else "clang")
    if not compiler:
        raise unittest.SkipTest("clang/clang++ not available")
    preferred = Path(os.environ.get("OPENCODE_TEST_TMP", "/mnt/c/Users/renny/AppData/Local/Temp/opencode"))
    parent = str(preferred) if preferred.is_dir() else None
    with tempfile.TemporaryDirectory(prefix="pclm10-tests-", dir=parent) as tmp:
        source = Path(tmp) / ("test.cpp" if cpp else "test.c")
        binary = Path(tmp) / "test"
        source.write_text(code, encoding="utf-8")
        env = dict(os.environ, TMPDIR=tmp)
        subprocess.run([compiler, "-std=c++17" if cpp else "-std=c11", "-Wall",
                        "-Wextra", "-Werror", str(source), "-o", str(binary)],
                       check=True, env=env)
        subprocess.run([str(binary)], check=True, env=env)


class PMICDisplayTests(unittest.TestCase):
    @unittest.skipIf(SOURCE_FLAG, "local private DTBO not part of CI checkout")
    def test_overlay_resolves_local_pmic_gpio(self):
        overlay = (ROOT / "dtbo-overlay-0.dts").read_text(encoding="utf-8")
        self.assertIn('enable1v8_gpio = <0x63 0x05 0x01>;', overlay)
        controller = overlay[overlay.index('qcom,pm8150l@4 {'):overlay.index('qcom,pm8150l@5 {')]
        self.assertIn('compatible = "qcom,spmi-gpio";', controller)
        self.assertIn('reg = <0xc000 0xc00>;', controller)
        self.assertIn('phandle = <0x63>;', controller)
        self.assertIn('touchpanel_1v8_enable', controller)
        self.assertIn('pins = "gpio5";', controller)
        self.assertIn('enable1v8_gpio = <0x00>;', overlay[overlay.index('__local_fixups__ {'):])

    def test_read_allowlist_exhaustive(self):
        src = (ACTIVE_SOURCE / "bsd/dev/sweet_spmi.c").read_text(encoding="utf-8")
        fn = extract_function(src, "sp_read_allowed")
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
''' + fn + r'''
int main(void) {
    const unsigned base[] = {0x08, 0x10, 0x11, 0x12, 0x13, 0x48, 0x60};
    for (unsigned sid = 0; sid < 16; sid++) {
        for (unsigned addr = 0; addr < 0x10000; addr++) {
            bool expect = false;
            for (unsigned i = 0; i < sizeof(base)/sizeof(base[0]); i++)
                if (sid == 0 && (addr >> 8) == base[i]) expect = true;
            unsigned off = addr & 255;
            if (sid == 4 && (addr >> 8) == 0xc4 &&
                (off == 4 || off == 5 || off == 0x10 || (off >= 0x40 && off <= 0x46)))
                expect = true;
            if (sp_read_allowed(sid, addr) != expect) {
                fprintf(stderr, "unexpected sid=%u addr=%x\n", sid, addr); return 1;
            }
        }
    }
    if (sp_read_allowed(0, 0x10008) || sp_read_allowed(4, 0x1c404)) return 2;
    return 0;
}
'''
        compile_and_run(False, harness)

    def test_touch_gpio_write_is_restricted(self):
        now = (ACTIVE_SOURCE / "bsd/dev/sweet_spmi.c").read_text(encoding="utf-8")
        writer = extract_function(now, "sweet_spmi_write")
        self.assertIn("sid != 0", writer)
        self.assertIn("peripheral != 0x08", writer)
        owner = extract_function(now, "sweet_spmi_touch_gpio_owner")
        self.assertIn("IOLockLock(sp_lock)", owner)
        self.assertIn("(4u << 8) | 0xc4u", owner)
        self.assertNotIn("SP_CHNLS_PA", owner)
        touch = (ACTIVE_SOURCE / "bsd/dev/sweet_s3706.c").read_text(encoding="utf-8")
        self.assertNotIn("TP_PIN_EN1V8", touch)
        self.assertNotIn("sweet_spmi_write(", touch)
        setter = extract_function(now, "sweet_spmi_touch_gpio_set")
        self.assertIn("owner != 0", setter)
        self.assertIn("type != 0x10 || subtype != 0x10", setter)
        self.assertIn("sp_read_locked(4, 0xc404", setter)
        order = ["0xc441", "0xc442", "0xc445", "0xc443", "0xc440", "0xc444", "0xc446"]
        self.assertEqual([setter.index(x) for x in order], sorted(setter.index(x) for x in order))
        self.assertIn("sweet_spmi_touch_gpio_set(true)", touch)
        self.assertIn("tp_pmic_gpio_diagnostic();", touch[touch.index("sweet_spmi_touch_gpio_set(true)"):])

    @unittest.skipIf(SOURCE_FLAG, "local PCLM10 profiles not part of CI checkout")
    def test_surf10_profile_is_one_argument(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("isolation", ROOT / "scripts/test_pclm10_driver_isolation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base = module.args("unixnu-p4-touch-pmicdiag.toml")
        candidate = module.args("unixnu-p4-touch-surf10.toml")
        self.assertEqual(sorted(set(candidate) - set(base)), ["sweetsurf10=2"])
        self.assertEqual(sorted(set(base) - set(candidate)), [])

    def test_rndis_uses_standard_cdc_interfaces_and_dhcp(self):
        src = (ACTIVE_SOURCE / "bsd/dev/sweet_usb.c").read_text(encoding="utf-8")
        # Linux f_rndis / stock Android: the IAD is EF/04/01 (rndiscmp.inf),
        # but the control and data interfaces keep the CDC classes.
        self.assertIn("9, 4, 0, 0, 1, 0x02, 0x02, 0x01, 5", src)
        self.assertIn("9, 4, 1, 0, 2, 0x0a, 0x00, 0x00, 0", src)
        self.assertNotIn("9, 4, 0, 0, 1, 0xef, 0x04, 0x01, 5", src)
        self.assertNotIn("9, 4, 1, 0, 2, 0xef, 0x04, 0x01, 0", src)
        # The host has no administrator and this PC routes 169.254/16
        # through Tailscale; the kernel DHCP server gives it 192.168.7.1/24.
        self.assertIn("htonl(0xc0a80702)", src)
        self.assertIn("htonl(0xffffff00)", src)
        self.assertIn("htonl(0xc0a807ff)", src)
        self.assertIn("ud_dhcp_start();", src)
        self.assertIn("SWEET DHCP: listening on udp 67", src)
        self.assertIn("SWEET DHCP: %s for %02x", src)
        # The client still has APIPA, so only the limited broadcast is
        # recognised by its IP stack; 192.168.7.255 is sent as fallback.
        self.assertIn("tx watchdog after %u ms", src)
        # The DHCP reply is a raw unicast Ethernet/IPv4/UDP frame with IP
        # destination 255.255.255.255, so it does not depend on the host's
        # APIPA broadcast rules or a phone route to the limited broadcast.
        self.assertIn("ud_tx_frame_raw(frame, total + 14);", src)
        self.assertIn("(raw %u B)", src)
        self.assertIn("ud_ip_csum(ip, 20)", src)
        self.assertIn("a->sin_addr.s_addr = htonl(0xc0a807ff);", src)
        # 0183 queues MEDIA_CONNECT after the filter SET but starts the ep2
        # notification from a separate thread; 0178's EP0-side notify
        # stalled the shared event thread and killed the serial console.
        self.assertIn("ud_media_start();", src)
        self.assertIn("media connect notify (thread)", src)
        self.assertIn("media_pending", src)
        # DWC3 EP0 OUT only completes a short data stage when the TRB
        # covers a full 64-byte max-packet slot (0173's 24-byte INIT
        # never produced XferComplete).
        self.assertIn("uint32_t xlen = ud.encmd_len ? ((ud.encmd_len + 63) & ~63u) : 64;", src)
        # The completed TRB's residual is relative to that rounded length,
        # so a 32-byte SEND left residual 32 and was read as zero bytes.
        self.assertIn("uint32_t    encmd_xlen;", src)
        self.assertIn("got = ud.encmd_xlen ? ud.encmd_xlen - (t[2] & 0xffffff) : ud.encmd_len;", src)

    def test_ufs_partition_read_is_read_only(self):
        files = (ACTIVE_SOURCE / "bsd/dev/sweet_files.c").read_text(encoding="utf-8")
        ufs = (ACTIVE_SOURCE / "bsd/dev/sweet_ufs.c").read_text(encoding="utf-8")
        self.assertIn("READUFS ", files)
        self.assertIn("LISTUFS", files)
        self.assertIn("READLUN ", files)
        self.assertIn("#define SF_BUF  (1024 * 1024)", files)
        self.assertIn("sweet_ufs_read_partition", files)
        self.assertIn("sweet_ufs_list_partitions", files)
        self.assertIn("sweet_ufs_read_lun", files)
        self.assertIn("sweet_ufs_read_partition", ufs)
        self.assertIn("sweet_ufs_list_partitions", ufs)
        self.assertIn("sweet_ufs_read_lun", ufs)
        body = extract_function(ufs, "sweet_ufs_read_partition")
        self.assertIn("ufs_find_partition(name)", body)
        self.assertIn("ufs_read_blocks(first + lba", body)
        self.assertIn("lck_mtx_lock(&sweet_ufs_lock);", body)
        self.assertNotIn("ufs_write_blocks", body)

    def test_source_map_is_opt_in(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        # 0181: dump SpringBoard's source IOSurface, not the panel copy,
        # so an App Library split can be attributed before guessing.
        self.assertIn('PE_parse_boot_argn("sweetfb_map_src"', src)
        self.assertIn("SWEET FB: source map surface", src)
        self.assertIn("src_elapsed >= 25 * 1000000000ULL", src)

    def test_map_quiesces_usb_setup_logs(self):
        fb = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        usb = (ACTIVE_SOURCE / "bsd/dev/sweet_usb.c").read_text(encoding="utf-8")
        self.assertIn("int sweet_map_quiet;", fb)
        self.assertIn("sweet_map_quiet = 1;", fb)
        self.assertIn("sweet_map_quiet = 0;", fb)
        self.assertIn("extern int sweet_map_quiet;", usb)
        self.assertIn("if (!sweet_map_quiet) {", usb)
        self.assertIn("SWEET USB: setup %02x", usb)

    def test_high_colour_map_is_bounded(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        fn = extract_function(src, "sweet_fb_dump_map_hd")
        emit = extract_function(src, "sweet_fb_map_hd_emit")
        self.assertIn("SWEET_FB_HD_W 56", src)
        self.assertIn("SWEET_FB_HD_H 120", src)
        self.assertIn("sweet_fb_hd_pixels[SWEET_FB_HD_W * SWEET_FB_HD_H]", src)
        self.assertIn("SWEET FB HDMAP %u %s", emit)
        self.assertIn("< 8", emit)
        self.assertIn("(uint16_t)((((r / n) >> 3) << 11)", fn)
        self.assertIn("PE_parse_boot_argn(\"sweetfb_map_hd\"", src)
        self.assertIn("elapsed_ns >= 80 * 1000000000ULL", src)

    def test_framebuffer_map_capture_is_bounded(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        fn = extract_function(src, "sweet_fb_dump_map")
        self.assertIn("48x106", src)
        self.assertIn("r += p[2]", fn)
        self.assertIn("g += p[1]", fn)
        self.assertIn("b += p[0]", fn)
        self.assertIn("y += 2", fn)
        self.assertIn("x += 2", fn)
        self.assertIn("gy < 106", fn)
        self.assertIn("gx < 48", fn)
        self.assertIn("PE_parse_boot_argn(\"sweetfb_map\"", fn)
        emit = extract_function(src, "sweet_fb_map_emit")
        self.assertIn("SWEET FB MAP %u %u %s", emit)
        self.assertIn("++sweet_fb_map_tick < 2", emit)
        self.assertIn("sweet_fb_map_emit();", src)
        self.assertIn("static const uint64_t marks[] = {75, 150, 210};", src)
        self.assertIn("elapsed_ns >= marks[i] * 1000000000ULL", src)
        self.assertNotIn("if (slot >= 3)", src)

    def test_synthetic_swipe_is_opt_in(self):
        src = (ACTIVE_SOURCE / "bsd/dev/sweet_s3706.c").read_text(encoding="utf-8")
        fn = extract_function(src, "tp_swipe")
        self.assertIn("sweet_hid_touch(0, 1, 540", fn)
        self.assertIn("sweet_hid_touch(0, 0, 540", fn)
        self.assertIn("950 - (int)i * 32", fn)
        self.assertIn("PE_parse_boot_argn(\"s3706swipe\"", src)
        self.assertIn("++sw_tick >= swipe_s * 100u", src)
        self.assertIn("tp_swipe(sw_count++ == 0 ? 0 : 1)", src)

    def test_touch_gpio_register_values(self):
        src = (ACTIVE_SOURCE / "bsd/dev/sweet_spmi.c").read_text(encoding="utf-8")
        fn = extract_function(src, "sp_touch_gpio_reg_value")
        harness = r'''
#include <stdint.h>
#include <stdio.h>
''' + fn + r'''
int main(void) {
    if (sp_touch_gpio_reg_value(0xc441, 1) != 0x00) return 1;
    if (sp_touch_gpio_reg_value(0xc442, 1) != 0x05) return 2;
    if (sp_touch_gpio_reg_value(0xc445, 1) != 0x03) return 3;
    if (sp_touch_gpio_reg_value(0xc443, 1) != 0x00) return 4;
    if (sp_touch_gpio_reg_value(0xc440, 1) != 0x01) return 5;
    if (sp_touch_gpio_reg_value(0xc444, 1) != 0x80) return 6;
    if (sp_touch_gpio_reg_value(0xc444, 0) != 0x00) return 7;
    if (sp_touch_gpio_reg_value(0xc446, 1) != 0x80) return 8;
    return 0;
}
'''
        compile_and_run(False, harness)

    @unittest.skipIf(SOURCE_FLAG, "original local CI build tree not present in CI")
    def test_original_writer_byte_identical(self):
        now = (ACTIVE_SOURCE / "bsd/dev/sweet_spmi.c").read_text(encoding="utf-8")
        before = (ROOT / "work/buildtree-0149/xnu/bsd/dev/sweet_spmi.c").read_text(encoding="utf-8")
        self.assertEqual(extract_function(now, "sweet_spmi_write"),
                         extract_function(before, "sweet_spmi_write"))

    @unittest.skipIf(SOURCE_FLAG, "local CI preimage hashes not present in CI checkout")
    def test_ci_preimage_hashes(self):
        expected = {
            "bsd/dev/sweet_s3706.c": "50e99f76c02ef2edee813583c19b9f43959ed747eab76d94a175a4638ddd9c76",
            "bsd/dev/sweet_spmi.c": "6ffa6a1da6071deeb195968ef7740236242a7810b1d85aa03b49cc794c07f6be",
            "bsd/dev/sweet_spmi.h": "555d0e4500320b0c3c8df36d498fc4262e70a36eb67f30e009e3be32a0554303",
            "iokit/Kernel/arm/SweetFramebuffer.cpp": "b1840e07a1baf6606c25ce70817ca961fbfa6975bdfe060f25f9519787a705d0",
        }
        # Work/buildtree-0160 was edited after extracting the exact CI files;
        # use the immutable baseline committed in the local XNU checkout.
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        for path, digest in expected.items():
            result = subprocess.run([git, "show", f"af249f8d5da5e4663a32a889f633f82262943b99:{path}"],
                                    cwd=SOURCE, check=True, stdout=subprocess.PIPE)
            self.assertEqual(hashlib.sha256(result.stdout).hexdigest(), digest, path)

    def test_display_comparison_against_synthetic_frames(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        fn = extract_function(src, "sweet_fb_compare_cpu")
        harness = r'''
#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
static char last_log[1024];
static void PE_parse_boot_argn(const char *, int *v, unsigned long) { *v = 1; }
static void IOLog(const char *format, ...) {
    va_list a; va_start(a, format); vsnprintf(last_log, sizeof(last_log), format, a); va_end(a);
}
''' + fn + r'''
int main() {
    const uint32_t W=1080, H=2400, SW=1125, SH=2436, STRIDE=4608;
    std::vector<uint8_t> source((size_t)STRIDE*SH), panel((size_t)W*H*4);
    for (uint32_t y=0; y<SH; y++) for (uint32_t x=0; x<SW; x++) {
        uint8_t *p=&source[(size_t)y*STRIDE+4*x];
        p[0]=197; p[1]=131; p[2]=13; p[3]=255;
    }
    // A small foreground pattern makes this a nonuniform frame; x=22
    // and y=594 align with the diagnostic's sparse grid after cropping.
    for (uint32_t y=594; y<626; y++) for (uint32_t x=22; x<54; x++) {
        uint8_t *p=&source[(size_t)y*STRIDE+4*x]; p[0]=80; p[1]=20; p[2]=200;
    }
    for (uint32_t y=0; y<H; y++)
        memcpy(&panel[(size_t)y*W*4], &source[(size_t)(y+18)*STRIDE+22*4], W*4);
    const auto original=panel;
    sweet_fb_compare_cpu(source.data(), panel.data(), STRIDE, W*4,
                         22,18,0,0,W,H,W,H,20);
    if (!strstr(last_log,"mismatches=0") || !strstr(last_log,"crop=22,18") || panel!=original) return 1;
    for (uint32_t y=0; y<H; y++) for (uint32_t x=0; x<W; x++) {
        uint8_t *p=&panel[((size_t)y*W+x)*4]; std::swap(p[0],p[2]);
    }
    const auto altered=panel;
    for (int i=0; i<15; i++)
        sweet_fb_compare_cpu(source.data(), panel.data(), STRIDE, W*4,
                             22,18,0,0,W,H,W,H,20);
    if (strstr(last_log,"mismatches=0") || strstr(last_log,"rb-swapped=0") || panel!=altered) return 2;
    return 0;
}
'''
        compile_and_run(True, harness)

    def test_display_comparison_disabled_and_uniform(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        fn = extract_function(src, "sweet_fb_compare_cpu")
        harness = r'''
#include <cstdint>
#include <cstdio>
#include <vector>
static int on, logs;
static void PE_parse_boot_argn(const char *, int *v, unsigned long) { *v = on; }
static void IOLog(const char *, ...) { logs++; }
''' + fn + r'''
int main() {
    const uint32_t W=64, H=64;
    std::vector<uint8_t> pixels(W*H*4, 0);
    sweet_fb_compare_cpu(pixels.data(), pixels.data(), W*4, W*4,
                         0,0,0,0,W,H,W,H,0);
    if (logs) return 1;  // boot argument defaults to off
    on=1;
    sweet_fb_compare_cpu(pixels.data(), pixels.data(), W*4, W*4,
                         0,0,0,0,W,H,W,H,0);
    if (logs) return 2;  // cached disabled state must remain disabled
    return 0;
}
'''
        compile_and_run(True, harness)

    def test_display_comparison_rejects_uniform_when_enabled(self):
        src = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        fn = extract_function(src, "sweet_fb_compare_cpu")
        harness = r'''
#include <cstdint>
#include <cstdio>
#include <vector>
static int logs;
static void PE_parse_boot_argn(const char *, int *v, unsigned long) { *v = 1; }
static void IOLog(const char *, ...) { logs++; }
''' + fn + r'''
int main() {
    const uint32_t W=64, H=64;
    std::vector<uint8_t> pixels(W*H*4, 23);
    sweet_fb_compare_cpu(pixels.data(), pixels.data(), W*4, W*4,
                         0,0,0,0,W,H,W,H,0);
    if (logs) return 1;
    pixels[((size_t)H/4*W)*4] = 55;
    sweet_fb_compare_cpu(pixels.data(), pixels.data(), W*4, W*4,
                         0,0,0,0,W,H,W,H,0);
    return logs == 1 ? 0 : 2;
}
'''
        compile_and_run(True, harness)

    @unittest.skipIf(SOURCE_FLAG, "local PCLM10 profiles not part of CI checkout")
    def test_profile_is_one_diagnostic_argument(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("isolation", ROOT / "scripts/test_pclm10_driver_isolation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(sorted(set(module.args("unixnu-p4-touch-pmicdiag.toml")) -
                                set(module.args("unixnu-p4-touch.toml"))),
                         ["s3706swipe=35", "sweetfb_compare=1", "sweetfb_map_hd=1"])

    @unittest.skipIf(SOURCE_FLAG, "requires the board's DT and on-device log")
    def test_identifies_cpu_center_crop_without_claiming_correct_output(self):
        text = (ROOT / "work/usb-log-20261108-222720/live-log.txt").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("AppleCLCD 1080x2400", text)
        self.assertIn("presented as 1125x2436", text)
        self.assertIn("surface 3 1125x2436 fmt 0x42475241", text)
        source = (ACTIVE_SOURCE / "iokit/Kernel/arm/SweetFramebuffer.cpp").read_text(encoding="utf-8")
        self.assertIn("fOffX = (int32_t)(fLogicalW - fWidth) / 2", source)
        self.assertIn("dx -= fOffX", source)
        self.assertEqual(((1125 - 1080) // 2, (2436 - 2400) // 2), (22, 18))

    @unittest.skipIf(SOURCE_FLAG, "local CI preimage not present in CI checkout")
    def test_patch_applies_to_exact_ci_sources(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        originals = {
            "bsd/dev/sweet_s3706.c": ROOT / "work/buildtree-0171-r42545717811/xnu/bsd/dev/sweet_s3706.c",
            "bsd/dev/sweet_spmi.c": ROOT / "work/buildtree-0171-r42545717811/xnu/bsd/dev/sweet_spmi.c",
            "bsd/dev/sweet_spmi.h": ROOT / "work/buildtree-0171-r42545717811/xnu/bsd/dev/sweet_spmi.h",
            "bsd/dev/sweet_usb.c": ROOT / "work/patch-0183/post/bsd/dev/sweet_usb.c",
            "bsd/dev/sweet_files.c": ROOT / "work/patch-0186/post/bsd/dev/sweet_files.c",
            "bsd/dev/sweet_ufs.c": ROOT / "work/patch-0186/post/bsd/dev/sweet_ufs.c",
            # 0173's preimage includes 0172's framebuffer changes.
            "iokit/Kernel/arm/SweetFramebuffer.cpp": ROOT / "work/patch-0182/post/iokit/Kernel/arm/SweetFramebuffer.cpp",
        }
        preferred = Path(os.environ.get("OPENCODE_TEST_TMP", "/mnt/c/Users/renny/AppData/Local/Temp/opencode"))
        with tempfile.TemporaryDirectory(prefix="pclm10-apply-", dir=str(preferred) if preferred.is_dir() else None) as tmp:
            root = Path(tmp)
            for relative, original in originals.items():
                dest = root / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, dest)
                dest.chmod(0o644)
            subprocess.run([git, "-c", "core.filemode=false", "apply", "--check", str(PATCH)], cwd=root, check=True)
            subprocess.run([git, "-c", "core.filemode=false", "apply", str(PATCH)], cwd=root, check=True)
            for relative in originals:
                self.assertEqual((root / relative).read_bytes(), (SOURCE / relative).read_bytes(), relative)


if __name__ == "__main__":
    unittest.main(verbosity=2)
