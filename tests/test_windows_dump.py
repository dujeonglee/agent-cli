"""windows-dump 내장 스킬의 kdump.py — 합성 덤프 테스트는 항상, 공개 샘플은 ``KDUMP_TESTDATA`` 가 있을 때만.

샘플 배치 (``KDUMP_TESTDATA`` 아래):
  bmp.dmp full.dmp kerneldump.dmp kerneluserdump.dmp completedump.dmp fulllivekernelmemory.dmp
      ← https://github.com/0vercl0k/kdmp-parser/releases/download/v0.1/testdatas.7z
  dumplib/pagedu64.dmp dumplib/pagedump.dmp   ← https://github.com/nforest/dumplib/tree/master/tests
  mdmp/test.dmp                               ← https://github.com/rust-minidump/rust-minidump/tree/main/testdata
  sym/ntoskrnl.exe/5507A73C5E8000/ntoskrnl.exe, sym/ntkrnlmp.pdb/9E22A5947A15489895CE716436B45BE02/ntkrnlmp.pdb
      ← msdl.microsoft.com (``kdump.py fetch-symbols dumplib/pagedu64.dmp --symdir sym --only ntoskrnl.exe``)

kdump.py 는 스킬 디렉토리 안의 독립 스크립트(패키지 모듈이 아니다 — 모델이 `python3 ${SKILL_DIR}/kdump.py` 로
부른다)라 importlib 로 읽는다.
"""

import importlib.util
import os
import struct
import sys
from pathlib import Path

import pytest

from agent_cli.skills.loader import _BUILTIN_DIR, _parse_skill_file

SKILL_DIR = _BUILTIN_DIR / "windows-dump"
_spec = importlib.util.spec_from_file_location("kdump", SKILL_DIR / "kdump.py")
kdump = importlib.util.module_from_spec(_spec)
sys.modules["kdump"] = (
    kdump  # dataclass 데코레이터가 cls.__module__ 을 sys.modules 에서 찾는다
)
_spec.loader.exec_module(kdump)

TD = os.environ.get("KDUMP_TESTDATA")


def _need(rel: str) -> str:
    if not TD or not (Path(TD) / rel).exists():
        pytest.skip(f"sample {rel} not available (set KDUMP_TESTDATA)")
    return str(Path(TD) / rel)


# ── 스킬 파일
def test_skill_parses_and_names_windows():
    skill = _parse_skill_file(SKILL_DIR / "SKILL.md")
    assert skill is not None and skill.name == "windows-dump"
    assert "Windows" in skill.description and "WinDbg" not in skill.description[:40]
    assert skill.disable_model_invocation is False  # 덤프가 나오면 모델이 스스로 부른다
    assert "shell" in skill.allowed_tools
    assert (
        "${SKILL_DIR}/kdump.py" in skill.prompt_template
        and "$ARGUMENTS" in skill.prompt_template
    )
    assert (SKILL_DIR / "references" / "verify-prompt.md").is_file()


def test_selftest_command_passes():
    import subprocess

    r = subprocess.run(
        [sys.executable, str(SKILL_DIR / "kdump.py"), "selftest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and r.stdout.startswith("selftest OK"), r.stdout + r.stderr


# ── 합성 덤프 (항상)
def test_selftest_synthetic(tmp_path):
    p = tmp_path / "s.dmp"
    p.write_bytes(kdump.build_synthetic_triage64())
    d = kdump.KernelDump(str(p))
    info = d.info()
    assert info["bugcheck"]["name"].startswith("DRIVER_IRQL")
    assert info["triage"]["valid_marker_ok"]
    assert [m.name for m in d.modules()] == ["ntoskrnl.exe", "mydrv.sys"]
    assert [(f.kind, f.module, f.rva) for f in kdump.scan_stack(d, d.modules())] == [
        ("ip", "mydrv.sys", 0xA010),
        ("fault-ip", "mydrv.sys", 0xA000),  # 0xD1 P4 = address that referenced memory
        ("slot", "ntoskrnl.exe", 0x46230),
        ("slot", "mydrv.sys", 0xA044),
    ]
    ps = info["bugcheck"]["parameters"]
    assert [p["kind"] for p in ps] == ["va", "irql", "access", "ip"]
    assert ps[3]["decoded"] == {"module": "mydrv.sys", "rva": "0xa000"}
    assert info["faulting_ip"]["source"] == "bugcheck parameter 4"


def test_bugcheck_tables_are_consistent():
    """표의 모양 — 항목마다 (설명, 종류) 넷, 종류는 KINDS 안, 서브타입은 셋씩, 서브타입이 있는 코드의 P1 은 sub."""
    for code, (name, spec) in kdump.BUGCHECKS.items():
        assert name and len(spec) == 4, code
        for desc, kind in spec:
            assert kind in kdump.KINDS, (code, kind)
    for code, subs in kdump.SUBTYPES.items():
        assert kdump.BUGCHECKS[code][1][0][1] == "sub", code
        for p1, rest in subs.items():
            assert len(rest) == 3 and all(k in kdump.KINDS for _, k in rest), (code, p1)
    for (code, idx), names in kdump.PARAM_NAMES.items():
        assert code in kdump.BUGCHECKS and 0 <= idx < 4 and names, (code, idx)
    assert (
        struct.calcsize("<Q")
        and kdump.KTRAP64["size"] == 0x190
        and kdump.KTRAP32["size"] == 0x8C
    )


def test_classify_va_and_where_on_synthetic(tmp_path):
    p = tmp_path / "s.dmp"
    p.write_bytes(kdump.build_synthetic_triage64())
    d = kdump.KernelDump(str(p))
    assert d.classify_va(0x18)["class"].startswith("null or near-null")
    assert d.classify_va(0x0000800000000000)["class"].startswith("non-canonical")
    assert d.classify_va(0x7FF600000000)["class"] == "user-mode address"
    assert d.classify_va(0xFFFFF80012300010)["module"] == "ntoskrnl.exe"
    assert (
        d.where(0xFFFFF80014000100)["unloaded_driver"] == "USBSTOR.SYS"
    )  # 언로드 범위
    assert d.classify_va(0xFFFFA000DEAD0010)["class"].startswith(
        "inside the crashing thread's captured stack"
    )


def test_idents():
    assert kdump.pe_ident(0x5507A73C, 0x5E8000) == "5507A73C5E8000"
    import uuid

    g = uuid.UUID("9e22a594-7a15-4898-95ce-716436b45be0")
    assert kdump.pdb_ident(g.bytes_le, 2) == "9E22A5947A15489895CE716436B45BE02"


def test_unknown_signature(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"NOPE" * 4)
    with pytest.raises(kdump.DumpError):
        kdump.open_dump(str(p))


# ── 공개 샘플
def test_triage64_header_and_drivers():
    d = kdump.KernelDump(_need("dumplib/pagedu64.dmp"))
    i = d.info()
    assert (
        i["bugcheck"]["code"] == "0x19"
        and i["machine"] == "x64"
        and i["os_build"] == 7601
    )
    assert i["triage"]["valid_marker_ok"]
    assert (
        i["context"]["Rip"] == "0xfffff800042d7a40"
        and i["context"]["Rsp"] == "0xfffff8800a2847a8"
    )
    mods = {m.name: m for m in d.modules()}
    nt = mods["ntoskrnl.exe"]
    assert (nt.base, nt.size, nt.timestamp, nt.checksum) == (
        0xFFFFF80004265000,
        0x5E8000,
        0x5507A73C,
        0x5502F9,
    )
    assert mods["hal.dll"].timestamp == 0x4CE7C669
    assert len(d.modules()) == 0xA7
    unl = d.unloaded()
    assert unl[0] == {
        "name": "USBSTOR.SYS",
        "start": "0xfffff8800350f000",
        "end": "0xfffff8800352a000",
    }
    frames = kdump.scan_stack(d, d.modules())
    assert (frames[0].module, frames[0].rva) == ("ntoskrnl.exe", 0x72A40)
    assert (frames[1].module, frames[1].rva) == ("QQSysMonX64.sys", 0x106F6)


def test_triage32_header_and_drivers():
    d = kdump.KernelDump(_need("dumplib/pagedump.dmp"))
    i = d.info()
    assert i["bugcheck"]["code"] == "0x1000008e" and i["machine"] == "x86"
    assert i["context"]["Eip"] == "0xac4517d7" and i["context"]["Esp"] == "0xd9f0eb60"
    assert i["triage"]["valid_marker_ok"]
    # 0x1000008E: P2 = 예외 주소(= 컨텍스트 Eip), P3 = KTRAP_FRAME*. 커널 모드 트랩 프레임은 Esp 아래에 놓여
    # 미니덤프에 실리지 않지만, &HardwareEsp(+0x74) 가 컨텍스트 Esp 와 같다 — KTRAP32 레이아웃의 실측 근거.
    ps = i["bugcheck"]["parameters"]
    assert (
        ps[0]["decoded"] == {"name": "STATUS_ACCESS_VIOLATION"}
        and ps[1]["kind"] == "ip"
    )
    trap = int(ps[2]["value"], 16)
    assert ps[2]["kind"] == "trap" and trap + kdump.KTRAP32["HardwareEsp"][0] == int(
        i["context"]["Esp"], 16
    )
    assert "error" in ps[2]["decoded"] or "partial" in ps[2]["decoded"]
    assert i["faulting_ip"]["value"] == "0xac4517d7"
    frames = kdump.scan_stack(d, d.modules())
    assert frames[0].kind == "ip" and all(
        f.kind != "fault-ip" for f in frames
    )  # P2 == 컨텍스트 Eip 라 중복 없음
    mods = {m.name: m for m in d.modules()}
    assert (
        mods["ntoskrnl.exe"].base,
        mods["ntoskrnl.exe"].size,
        mods["ntoskrnl.exe"].timestamp,
    ) == (
        0xE441F000,
        0x415000,
        0x5507A53D,
    )
    assert len(d.modules()) == 0xC1


@pytest.mark.parametrize(
    "name,ctx,pa,want",
    [
        (
            "bmp.dmp",
            (0xFFFFF805108776A0, 0xFFFFF805135684F8, 0x3, 0x52),
            0x6D4D22,
            "6d000000000a638875000000000a6398",
        ),
        (
            "full.dmp",
            (0xFFFFF805108776A0, 0xFFFFF805135684F8, 0x3, 0x52),
            0x6D4D22,
            "6d000000000a638875000000000a6398",
        ),
        (
            "kerneldump.dmp",
            (0xFFFFF803F2C35470, 0xFFFFF803F515EC28, 0x7A01, 0x1F),
            0x025892F0,
            "108c24500cc0ffffa01938510cc0ffff",
        ),
        (
            "kerneluserdump.dmp",
            (0xFFFFF803F2C35470, 0xFFFFF803F515EC28, 0x7A01, 0x1F),
            0x025892F0,
            "108c24500cc0ffffa01938510cc0ffff",
        ),
        (
            "completedump.dmp",
            (0xFFFFF803F2C35470, 0xFFFFF803F515EC28, 0x7A01, 0x1F),
            0x025892F0,
            "108c24500cc0ffffa01938510cc0ffff",
        ),
        (
            "fulllivekernelmemory.dmp",
            (0xFFFFF80750A98B6D, 0xFFFFFD8D6BCAED10, 0x4, 0x1),
            0x0D96A9000,
            "67d8b6dd0000000a67a81dd60000000a",
        ),
    ],
)
def test_full_dumps_match_kdmp_parser(name, ctx, pa, want):
    """kdmp-parser 의 tests_parser.cc 기대값 (레지스터 + 물리 읽기)."""
    d = kdump.KernelDump(_need(name))
    c = d.context()
    assert (c["Rip"], c["Rsp"], c["Rax"], c["R15"]) == ctx
    assert d.read_phys(pa, 16).hex() == want
    mods = d.modules()
    names = {m.name for m in mods}
    assert "ntoskrnl.exe" in names and "hal.dll" in names
    nt = next(m for m in mods if m.name == "ntoskrnl.exe")
    assert nt.pdb_name == "ntkrnlmp.pdb" and len(nt.pdb_ident) >= 33
    assert any(f.module == "ntoskrnl.exe" for f in kdump.scan_stack(d, mods))


def test_pdb_match_and_symbolize():
    pe = _need("sym/ntoskrnl.exe/5507A73C5E8000/ntoskrnl.exe")
    pdb = _need("sym/ntkrnlmp.pdb/9E22A5947A15489895CE716436B45BE02/ntkrnlmp.pdb")
    info = kdump.parse_pe_file(pe)
    p = kdump.Pdb(pdb)
    assert info["pdb_ident"] == "9E22A5947A15489895CE716436B45BE02"
    assert bytes(info["pdb_guid_le"]) == bytes(p.guid_le)
    assert (p.age, p.dbi_age) == (5, 2)  # 바로 그 "Age 가 다른데 같은 PDB" 사례
    assert "OmapToSrc" in p.dbg
    d = kdump.KernelDump(_need("dumplib/pagedu64.dmp"))
    mods = d.modules()
    frames = kdump.scan_stack(d, mods)
    kdump.symbolize(frames, mods, kdump.SymbolStore(str(Path(TD) / "sym")))
    assert frames[0].symbol == "KeBugCheckEx"
    named = {
        f.symbol.split("+")[0]
        for f in frames
        if f.module == "ntoskrnl.exe" and f.symbol
    }
    assert {
        "FsRtlUninitializeBaseMcb",
        "FsRtlTeardownPerStreamContexts",
        "ExpWorkerThread",
    } <= named


def test_mdmp():
    d = kdump.UserMinidump(_need("mdmp/test.dmp"))
    i = d.info()
    assert (
        i["machine"] == "x86"
        and i["exception"]["code"] == "0xc0000005"
        and i["exception"]["address"] == "0x40429e"
    )
    mods = d.modules()
    app = next(m for m in mods if m.name == "test_app.exe")
    assert (app.base, app.pdb_name, app.pdb_ident) == (
        0x400000,
        "test_app.pdb",
        "5A9832E5287241C1838ED98914E9B7FF1",
    )
    frames = kdump.scan_stack(d, mods)
    assert (frames[0].module, frames[0].rva) == ("test_app.exe", 0x429E)
    assert any(f.module == "test_app.exe" and f.slot for f in frames)


def test_synthetic_layout_matches_struct_sizes():
    """표의 크기 상수가 실제 pack 크기와 맞는지 — 상수를 고칠 때 깨지라고."""
    assert (
        struct.calcsize("<IIQQI") == 0x1C
    )  # EXCEPTION_RECORD64: code..NumberParameters
    assert kdump.DRV64["size"] == 0x90 and kdump.DRV32["size"] == 0x4C
    assert kdump.UNL64["size"] == 0x38 and kdump.UNL32["size"] == 0x28
