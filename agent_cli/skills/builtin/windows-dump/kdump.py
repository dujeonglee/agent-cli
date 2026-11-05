"""kdump — Windows 덤프 분류 도구 (Linux/macOS, 표준 라이브러리만).

지원 포맷
  PAGEDU64 / PAGEDUMP  커널 덤프: 미니(triage) · 전체(runs) · 비트맵(SDMP/FDMP) · RDMP(.dump /k /ka /f)
  MDMP                 사용자 모드 미니덤프: 헤더·모듈·예외만 (스택은 minidump-stackwalk 권장)

명령
  info        헤더: 버그체크 코드·파라미터·이름, OS 버전, 덤프 종류, 레지스터, 예외 레코드
  modules     적재 드라이버 목록 (이름·기준주소·크기·타임스탬프·체크섬·심볼서버 식별자)
  stack       스택 안에서 모듈 범위에 드는 값(복귀 주소 후보)을 `module+rva` 로
  symbolize   stack 결과를 PDB 로 이름까지 (중간값 전부 출력: addr → base → rva → pdb → symbol)
  pdbcheck    PE 와 PDB 가 짝인지 — GUID·Age 를 양쪽에서 읽어 판정 (손으로 비교하지 말 것)
  fetch-symbols  심볼 서버에서 모듈의 PE·PDB 를 symdir 로 (기본 msdl.microsoft.com)
  selftest    합성 덤프로 파서 자기 검사

설계 원칙
  - 레이아웃 상수는 전부 이 파일 머리의 표에 있다. 실제 WinDbg 출력과 다르면 거기만 고친다.
  - 모르는 것은 추정하지 않는다: PDB 가 없거나 GUID 가 다르면 `module+rva` 로 멈춘다.
  - 스택은 **스캔**이다(언와인딩 아님). 모듈 범위에 드는 8바이트 값을 후보로 보일 뿐이다.

검증 근거
  - PAGEDU64/PAGEDUMP 헤더·BMP·RDMP: 0vercl0k/kdmp-parser 의 구조체 정의와 테스트 덤프 6개로 대조.
  - TRIAGE 헤더·드라이버 항목·문자열 풀·언로드 목록: nforest/dumplib 샘플(x64·x86)과
    WinDbg 의 TRIAGE_DUMP64 출력 순서로 실측.
  - PDB(MSF7·DBI·S_PUB32·S_GPROC32·OMAP): 샘플 덤프의 ntkrnlmp.pdb(OMAP 있음)로 실측.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

__version__ = "0.1.0"

# ───────────────────────── 레이아웃 표 ─────────────────────────
# 커널 덤프 헤더 (64비트, 0x2000 바이트). kdmp-parser-structs.h 와 동일.
H64 = {
    "Signature": (0x000, "<4s"),
    "ValidDump": (0x004, "<4s"),
    "MajorVersion": (0x008, "<I"),
    "MinorVersion": (0x00C, "<I"),
    "DirectoryTableBase": (0x010, "<Q"),
    "PfnDataBase": (0x018, "<Q"),
    "PsLoadedModuleList": (0x020, "<Q"),
    "PsActiveProcessHead": (0x028, "<Q"),
    "MachineImageType": (0x030, "<I"),
    "NumberProcessors": (0x034, "<I"),
    "BugCheckCode": (0x038, "<I"),
    "BugCheckParameters": (0x040, "<4Q"),
    "VersionUser": (0x060, "<32s"),
    "KdDebuggerDataBlock": (0x080, "<Q"),
    "PhysicalMemoryBlock": (
        0x088,
        None,
    ),  # NumberOfRuns u32, pad, NumberOfPages u64, Run[]{BasePage,PageCount}
    "ContextRecord": (0x348, None),
    "Exception": (0xF00, None),
    "DumpType": (0xF98, "<I"),
    "RequiredDumpSpace": (0xFA0, "<q"),
    "SystemTime": (0xFA8, "<q"),
    "Comment": (0xFB0, "<128s"),
    "SystemUpTime": (0x1030, "<q"),
    "MiniDumpFields": (0x1038, "<I"),
    "SecondaryDataState": (0x103C, "<I"),
    "ProductType": (0x1040, "<I"),
    "SuiteMask": (0x1044, "<I"),
    "WriterStatus": (0x1048, "<I"),
    "KdSecondaryVersion": (0x104D, "<B"),
    "Attributes": (0x1050, "<I"),
    "BootId": (0x1054, "<I"),
}
H64_SIZE = 0x2000
# 커널 덤프 헤더 (32비트, 0x1000 바이트). dumplib DMPTemplate.bt + 샘플 실측.
H32 = {
    "Signature": (0x000, "<4s"),
    "ValidDump": (0x004, "<4s"),
    "MajorVersion": (0x008, "<I"),
    "MinorVersion": (0x00C, "<I"),
    "DirectoryTableBase": (0x010, "<I"),
    "PfnDataBase": (0x014, "<I"),
    "PsLoadedModuleList": (0x018, "<I"),
    "PsActiveProcessHead": (0x01C, "<I"),
    "MachineImageType": (0x020, "<I"),
    "NumberProcessors": (0x024, "<I"),
    "BugCheckCode": (0x028, "<I"),
    "BugCheckParameters": (0x02C, "<4I"),
    "VersionUser": (0x03C, "<32s"),
    "PaeEnabled": (0x05C, "<B"),
    "KdSecondaryVersion": (0x05D, "<B"),
    "KdDebuggerDataBlock": (0x060, "<I"),
    "PhysicalMemoryBlock": (0x064, None),
    "ContextRecord": (0x320, None),
    "Exception": (0x7D0, None),
    "Comment": (0x820, "<128s"),
    "DumpType": (0xF88, "<I"),
    "MiniDumpFields": (0xF8C, "<I"),
    "SecondaryDataState": (0xF90, "<I"),
    "ProductType": (0xF94, "<I"),
    "SuiteMask": (0xF98, "<I"),
    "WriterStatus": (0xF9C, "<I"),
    "RequiredDumpSpace": (0xFA0, "<q"),
    "SystemUpTime": (0xFB8, "<q"),
    "SystemTime": (0xFC0, "<q"),
}
H32_SIZE = 0x1000

# TRIAGE_DUMP64 (헤더 바로 뒤, 0x2000). WinDbg 출력 순서 + 샘플 실측.
T64 = {
    "ServicePackBuild": (0x00, "<I"),
    "SizeOfDump": (0x04, "<I"),
    "ValidOffset": (0x08, "<I"),
    "ContextOffset": (0x0C, "<I"),
    "ExceptionOffset": (0x10, "<I"),
    "MmOffset": (0x14, "<I"),
    "UnloadedDriversOffset": (0x18, "<I"),
    "PrcbOffset": (0x1C, "<I"),
    "ProcessOffset": (0x20, "<I"),
    "ThreadOffset": (0x24, "<I"),
    "CallStackOffset": (0x28, "<I"),
    "SizeOfCallStack": (0x2C, "<I"),
    "DriverListOffset": (0x30, "<I"),
    "DriverCount": (0x34, "<I"),
    "StringPoolOffset": (0x38, "<I"),
    "StringPoolSize": (0x3C, "<I"),
    "BrokenDriverOffset": (0x40, "<I"),
    "TriageOptions": (0x44, "<I"),
    "TopOfStack": (0x48, "<Q"),
    "BStoreOffset": (0x50, "<I"),
    "SizeOfBStore": (0x54, "<I"),
    "LimitOfBStore": (0x58, "<Q"),
    "DataPageAddress": (0x60, "<Q"),
    "DataPageOffset": (0x68, "<I"),
    "DataPageSize": (0x6C, "<I"),
    "DebuggerDataOffset": (0x70, "<I"),
    "DebuggerDataSize": (0x74, "<I"),
    "DataBlocksOffset": (0x78, "<I"),
    "DataBlocksCount": (0x7C, "<I"),
}
# TRIAGE_DUMP32 (0x1000).
T32 = {
    "ServicePackBuild": (0x00, "<I"),
    "SizeOfDump": (0x04, "<I"),
    "ValidOffset": (0x08, "<I"),
    "ContextOffset": (0x0C, "<I"),
    "ExceptionOffset": (0x10, "<I"),
    "MmOffset": (0x14, "<I"),
    "UnloadedDriversOffset": (0x18, "<I"),
    "PrcbOffset": (0x1C, "<I"),
    "ProcessOffset": (0x20, "<I"),
    "ThreadOffset": (0x24, "<I"),
    "CallStackOffset": (0x28, "<I"),
    "SizeOfCallStack": (0x2C, "<I"),
    "DriverListOffset": (0x30, "<I"),
    "DriverCount": (0x34, "<I"),
    "StringPoolOffset": (0x38, "<I"),
    "StringPoolSize": (0x3C, "<I"),
    "BrokenDriverOffset": (0x40, "<I"),
    "TriageOptions": (0x44, "<I"),
    "TopOfStack": (0x48, "<I"),
    "DataPageAddress": (0x4C, "<I"),
    "DataPageOffset": (0x50, "<I"),
    "DataPageSize": (0x54, "<I"),
    "DebuggerDataOffset": (0x58, "<I"),
    "DebuggerDataSize": (0x5C, "<I"),
    "DataBlocksOffset": (0x60, "<I"),
    "DataBlocksCount": (0x64, "<I"),
}
TRIAGE_VALID_MARK = b"TRGD"

# 드라이버 항목 = u32 NameOffset + pad + KLDR_DATA_TABLE_ENTRY (덤프 고정 레이아웃).
DRV64 = {
    "size": 0x90,
    "NameOffset": 0x00,
    "DllBase": 0x38,
    "EntryPoint": 0x40,
    "SizeOfImage": 0x48,
    "CheckSum": 0x80,
    "TimeDateStamp": 0x88,
}
DRV32 = {
    "size": 0x4C,
    "NameOffset": 0x00,
    "DllBase": 0x1C,
    "EntryPoint": 0x20,
    "SizeOfImage": 0x24,
    "CheckSum": 0x44,
    "TimeDateStamp": 0x48,
}
# 언로드 목록: u32 Count (+pad in 64) 뒤 항목들.
UNL64 = {
    "hdr": 8,
    "size": 0x38,
    "Length": 0x00,
    "Name": 0x10,
    "NameBytes": 24,
    "Start": 0x28,
    "End": 0x30,
    "ptr": "<Q",
}
UNL32 = {
    "hdr": 4,
    "size": 0x28,
    "Length": 0x00,
    "Name": 0x08,
    "NameBytes": 24,
    "Start": 0x20,
    "End": 0x24,
    "ptr": "<I",
}

# CONTEXT 레지스터 오프셋
CTX64 = {
    "SegCs": (0x38, "<H"),
    "EFlags": (0x44, "<I"),
    "Rax": (0x78, "<Q"),
    "Rcx": (0x80, "<Q"),
    "Rdx": (0x88, "<Q"),
    "Rbx": (0x90, "<Q"),
    "Rsp": (0x98, "<Q"),
    "Rbp": (0xA0, "<Q"),
    "Rsi": (0xA8, "<Q"),
    "Rdi": (0xB0, "<Q"),
    "R8": (0xB8, "<Q"),
    "R9": (0xC0, "<Q"),
    "R10": (0xC8, "<Q"),
    "R11": (0xD0, "<Q"),
    "R12": (0xD8, "<Q"),
    "R13": (0xE0, "<Q"),
    "R14": (0xE8, "<Q"),
    "R15": (0xF0, "<Q"),
    "Rip": (0xF8, "<Q"),
}
CTX32 = {
    "SegGs": (0x8C, "<I"),
    "SegFs": (0x90, "<I"),
    "Edi": (0x9C, "<I"),
    "Esi": (0xA0, "<I"),
    "Ebx": (0xA4, "<I"),
    "Edx": (0xA8, "<I"),
    "Ecx": (0xAC, "<I"),
    "Eax": (0xB0, "<I"),
    "Ebp": (0xB4, "<I"),
    "Eip": (0xB8, "<I"),
    "SegCs": (0xBC, "<I"),
    "EFlags": (0xC0, "<I"),
    "Esp": (0xC4, "<I"),
    "SegSs": (0xC8, "<I"),
}

# 전체 덤프의 커널 _KLDR_DATA_TABLE_ENTRY (x64, Win7~11 공통 부분만 쓴다)
KLDR64 = {
    "DllBase": 0x30,
    "EntryPoint": 0x38,
    "SizeOfImage": 0x40,
    "FullDllName": 0x48,
    "BaseDllName": 0x58,
    "CheckSum": 0x78,
}

DUMP_TYPES = {
    1: "FullDump",
    2: "KernelDump(summary)",
    4: "MiniDump(triage)",
    5: "BMPDump",
    6: "LiveKernelBitmapDump",
    8: "KernelMemoryDump(.dump /k)",
    9: "KernelAndUserMemoryDump(.dump /ka)",
    0xA: "CompleteMemoryDump(.dump /f)",
}
MACHINES = {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0x1C4: "arm"}
PAGE = 0x1000

# ───────────────────────── 버그체크 표 ─────────────────────────
# 출처: Microsoft bug check reference (learn.microsoft.com/windows-hardware/drivers/debugger/), 2026-11-05 대조.
# 0x24·0x34·0x44·0x7A·0x116·0x117·0x154·0xEA 는 기억에서 적은 것 — 레퍼런스와 아직 대조하지 않았다.
#
# 파라미터 종류(kind) — `info` 가 역참조·분류하는 근거. "" 는 숫자 그대로.
#   ip        범인 명령 주소 → module+rva (WinDbg `ln`)       va      참조된 데이터 주소 → 분류(널·모듈·언로드·풀)
#   irql      IRQL 이름                                       access  0 read 1 write 2/8 execute (0x50 은 access50)
#   ntstatus  예외 코드 이름                                   trap    KTRAP_FRAME* → 레지스터 (WinDbg `.trap`)
#   ctx       CONTEXT* → 레지스터 (WinDbg `.cxr`)              exr     EXCEPTION_RECORD* (WinDbg `.exr`)
#   prcb      KPRCB* → 멈춘 프로세서의 현재 스레드             ptr     커널 객체 포인터 (WinDbg 확장이 필요)
#   sub       P1 이 나머지 의미를 정한다 (SUBTYPES)            pte     PTE 내용
KINDS = frozenset(
    {
        "",
        "ip",
        "va",
        "irql",
        "access",
        "access50",
        "ntstatus",
        "trap",
        "ctx",
        "exr",
        "prcb",
        "ptr",
        "sub",
        "pte",
    }
)
_EXC = [("exception code", "ntstatus"), ("address where the exception occurred", "ip")]
_REF = [("referenced address", "va")]
_RW = ("0 read, 1 write", "access")
_RSV = ("reserved", "")
_SUBV = [("(depends on P1)", ""), ("(depends on P1)", ""), ("(depends on P1)", "")]
BUGCHECKS = {
    0x0A: (
        "IRQL_NOT_LESS_OR_EQUAL",
        _REF
        + [
            ("IRQL at fault", "irql"),
            ("bit0 write, bit3 execute", "access"),
            ("instruction pointer at fault", "ip"),
        ],
    ),
    0xD1: (
        "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
        _REF
        + [
            ("IRQL at reference", "irql"),
            ("0 read, 1 write, 2/8 execute", "access"),
            ("address that referenced memory", "ip"),
        ],
    ),
    0x50: (
        "PAGE_FAULT_IN_NONPAGED_AREA",
        _REF
        + [
            ("0 read, 2 write, 0x10 execute (pre-1507: 1 write)", "access50"),
            ("address that referenced memory (if known)", "ip"),
            ("fault type", ""),
        ],
    ),
    0x1E: (
        "KMODE_EXCEPTION_NOT_HANDLED",
        _EXC + [("exception info parameter 0", ""), ("exception info parameter 1", "")],
    ),
    0x7E: (
        "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
        _EXC + [("EXCEPTION_RECORD*", "exr"), ("CONTEXT*", "ctx")],
    ),
    0x1000007E: (
        "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED_M",
        _EXC + [("EXCEPTION_RECORD*", "exr"), ("CONTEXT*", "ctx")],
    ),
    0x8E: (
        "KERNEL_MODE_EXCEPTION_NOT_HANDLED",
        _EXC + [("KTRAP_FRAME*", "trap"), _RSV],
    ),
    0x1000008E: (
        "KERNEL_MODE_EXCEPTION_NOT_HANDLED_M",
        _EXC + [("KTRAP_FRAME*", "trap"), _RSV],
    ),
    0x3B: (
        "SYSTEM_SERVICE_EXCEPTION",
        [
            ("exception code", "ntstatus"),
            ("address of the instruction that caused the bug check", "ip"),
            ("CONTEXT*", "ctx"),
            ("not used", ""),
        ],
    ),
    0x139: (
        "KERNEL_SECURITY_CHECK_FAILURE",
        [
            ("type of corruption", "sub"),
            ("KTRAP_FRAME*", "trap"),
            ("EXCEPTION_RECORD*", "exr"),
            _RSV,
        ],
    ),
    0x7F: (
        "UNEXPECTED_KERNEL_MODE_TRAP",
        [("trap number", "sub"), ("", ""), ("", ""), ("", "")],
    ),
    0x19: ("BAD_POOL_HEADER", [("violation type", "sub")] + _SUBV),
    0xC2: ("BAD_POOL_CALLER", [("violation type", "sub")] + _SUBV),
    0xC4: (
        "DRIVER_VERIFIER_DETECTED_VIOLATION",
        [("violation type (!verifier)", "sub")] + _SUBV,
    ),
    0xC9: ("DRIVER_VERIFIER_IOMANAGER_VIOLATION", [("violation type", "sub")] + _SUBV),
    0xC5: (
        "DRIVER_CORRUPTED_EXPOOL",
        _REF
        + [
            ("IRQL at reference", "irql"),
            _RW,
            ("address that referenced memory", "ip"),
        ],
    ),
    0xCE: (
        "DRIVER_UNLOADED_WITHOUT_CANCELLING_PENDING_OPERATIONS",
        _REF + [_RW, ("address that referenced memory (if known)", "ip"), _RSV],
    ),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", [("violation type", "sub")] + _SUBV),
    0x133: (
        "DPC_WATCHDOG_VIOLATION",
        [("0 single DPC/ISR exceeded, 1 cumulative", "sub")] + _SUBV,
    ),
    0x1A: ("MEMORY_MANAGEMENT", [("subtype", "sub")] + _SUBV),
    0x24: (
        "NTFS_FILE_SYSTEM",
        [
            ("source file/line", ""),
            ("EXCEPTION_RECORD*", "exr"),
            ("CONTEXT*", "ctx"),
            ("", ""),
        ],
    ),
    0x34: (
        "CACHE_MANAGER",
        [
            ("source file/line", ""),
            ("EXCEPTION_RECORD*", "exr"),
            ("CONTEXT*", "ctx"),
            ("", ""),
        ],
    ),
    0x3D: ("INTERRUPT_EXCEPTION_NOT_HANDLED", [("", ""), ("", ""), ("", ""), ("", "")]),
    0x44: (
        "MULTIPLE_IRP_COMPLETE_REQUESTS",
        [("IRP* (!irp)", "ptr"), _RSV, ("", ""), ("", "")],
    ),
    0x4E: ("PFN_LIST_CORRUPT", [("subtype", "sub")] + _SUBV),
    0x77: (
        "KERNEL_STACK_INPAGE_ERROR",
        [("status / cause", ""), ("", ""), ("", ""), ("", "")],
    ),
    0x7A: (
        "KERNEL_DATA_INPAGE_ERROR",
        [
            ("lock type / PTE", ""),
            ("error status", "ntstatus"),
            ("current process / PTE", ""),
            ("virtual address that could not be paged in", "va"),
        ],
    ),
    0xBE: (
        "ATTEMPTED_WRITE_TO_READONLY_MEMORY",
        [
            ("virtual address of attempted write", "va"),
            ("PTE contents", "pte"),
            _RSV,
            _RSV,
        ],
    ),
    0xD5: (
        "DRIVER_PAGE_FAULT_IN_FREED_SPECIAL_POOL",
        _REF + [_RW, ("address that referenced memory (if known)", "ip"), _RSV],
    ),
    0xD6: (
        "DRIVER_PAGE_FAULT_BEYOND_END_OF_ALLOCATION",
        _REF + [_RW, ("address that referenced memory (if known)", "ip"), _RSV],
    ),
    0xDE: ("POOL_CORRUPTION_IN_FILE_AREA", [("", ""), ("", ""), ("", ""), ("", "")]),
    0xEF: (
        "CRITICAL_PROCESS_DIED",
        [
            ("process object (!process)", "ptr"),
            ("0 process terminated, 1 thread terminated", ""),
            _RSV,
            _RSV,
        ],
    ),
    0xF5: ("FLTMGR_FILE_SYSTEM", [("subtype", "sub")] + _SUBV),
    0xF7: (
        "DRIVER_OVERRAN_STACK_BUFFER",
        [
            ("actual security cookie", ""),
            ("expected security cookie", ""),
            ("bit-complement of the expected cookie", ""),
            ("0", ""),
        ],
    ),
    0xFC: (
        "ATTEMPTED_EXECUTE_OF_NOEXECUTE_MEMORY",
        [
            ("virtual address whose execution was attempted", "ip"),
            ("PTE contents", "pte"),
            _RSV,
            _RSV,
        ],
    ),
    0x101: (
        "CLOCK_WATCHDOG_TIMEOUT",
        [
            ("clock interrupt time-out, ticks", ""),
            ("0", ""),
            ("KPRCB* of the unresponsive processor", "prcb"),
            ("index of the hung processor", ""),
        ],
    ),
    0x109: (
        "CRITICAL_STRUCTURE_CORRUPTION",
        [_RSV, _RSV, _RSV, ("type of the corrupted region", "")],
    ),
    0x116: (
        "VIDEO_TDR_FAILURE",
        [
            ("TDR recovery context", "ptr"),
            ("driver callback address", "ip"),
            ("error code", "ntstatus"),
            ("internal", ""),
        ],
    ),
    0x117: (
        "VIDEO_TDR_TIMEOUT_DETECTED",
        [
            ("TDR recovery context", "ptr"),
            ("driver callback address", "ip"),
            ("", ""),
            ("", ""),
        ],
    ),
    0x124: (
        "WHEA_UNCORRECTABLE_ERROR",
        [
            ("error source type", "sub"),
            ("WHEA_ERROR_RECORD* (!errrec)", "ptr"),
            ("MCi_STATUS high 32 bits (source 0)", ""),
            ("MCi_STATUS low 32 bits (source 0)", ""),
        ],
    ),
    0x12B: ("FAULTY_HARDWARE_CORRUPTED_PAGE", [("", ""), ("", ""), ("", ""), ("", "")]),
    0x154: (
        "UNEXPECTED_STORE_EXCEPTION",
        [("store context", "ptr"), ("EXCEPTION_RECORD*", "exr"), ("", ""), ("", "")],
    ),
    0x1C8: ("MANUALLY_INITIATED_CRASH1", [("", ""), ("", ""), ("", ""), ("", "")]),
    0xE2: ("MANUALLY_INITIATED_CRASH", [("", ""), ("", ""), ("", ""), ("", "")]),
    0x100000EA: (
        "THREAD_STUCK_IN_DEVICE_DRIVER_M",
        [
            ("thread (!thread)", "ptr"),
            ("DEVICE_OBJECT* (!devobj)", "ptr"),
            ("", ""),
            ("", ""),
        ],
    ),
    0xEA: (
        "THREAD_STUCK_IN_DEVICE_DRIVER",
        [
            ("thread (!thread)", "ptr"),
            ("DEVICE_OBJECT* (!devobj)", "ptr"),
            ("", ""),
            ("", ""),
        ],
    ),
}
# P1 값이 P2~P4 의 의미를 정하는 코드 — 자주 보는 서브타입만. 없는 값은 "(subtype not in table)" 로 표시된다.
_POOL = ("address of pool", "va")
_IRP = ("IRP* (!irp)", "ptr")
_DEVOBJ = ("device object (!devobj)", "ptr")
SUBTYPES = {
    0x19: {
        0x2: [
            ("pool entry being checked", "va"),
            ("size of the pool block", ""),
            ("0", ""),
        ],
        0x3: [
            ("pool entry being checked", "va"),
            ("read-back flink", "va"),
            ("read-back blink", "va"),
        ],
        0x20: [
            ("pool entry that should have been found", "va"),
            ("next pool entry", "va"),
            _RSV,
        ],
        0x21: [
            ("pool pointer being freed", "va"),
            ("bytes allocated for the block", ""),
            ("corrupted value following the block", ""),
        ],
        0x22: [("address being freed", "va"), _RSV, _RSV],
    },
    0xC2: {
        0x1: [
            ("pointer to pool header", "va"),
            ("first part of pool header contents", ""),
            ("0", ""),
        ],
        0x2: [
            ("pointer to pool header", "va"),
            ("first part of pool header contents", ""),
            ("0", ""),
        ],
        0x4: [
            ("pointer to pool header", "va"),
            ("first part of pool header contents", ""),
            ("0", ""),
        ],
        0x6: [_RSV, ("pointer to pool header", "va"), ("pool header contents", "")],
        0x7: [
            _RSV,
            ("pool header contents", ""),
            ("address of the block being freed", "va"),
        ],
        0x8: [
            ("current IRQL", "irql"),
            ("pool type", ""),
            ("size of allocation, bytes", ""),
        ],
        0x9: [("current IRQL", "irql"), ("pool type", ""), _POOL],
        0xA: [_POOL, ("allocator's tag", ""), ("tag used in the attempted free", "")],
        0xB: [
            _POOL,
            ("pool allocation's tag", ""),
            ("bad quota process pointer", "ptr"),
        ],
        0xC: [
            _POOL,
            ("pool allocation's tag", ""),
            ("bad quota process pointer", "ptr"),
        ],
        0xD: [
            _POOL,
            ("pool allocation's tag", ""),
            ("bad quota process pointer", "ptr"),
        ],
        0x99: [("address being freed", "va"), ("0", ""), ("0", "")],
        0x9B: [
            ("pool type", ""),
            ("number of bytes requested", ""),
            ("caller's address", "ip"),
        ],
        0x9C: [
            ("pool type", ""),
            ("number of bytes requested", ""),
            ("caller's address", "ip"),
        ],
        0x9D: [
            ("incorrect pool tag used", ""),
            ("pool type", ""),
            ("caller's address", "ip"),
        ],
    },
    0xC4: {
        0x0: [("current IRQL", "irql"), ("pool type", ""), ("number of bytes", "")],
        0x1: [
            ("current IRQL", "irql"),
            ("pool type", ""),
            ("size of allocation, bytes", ""),
        ],
        0x2: [
            ("current IRQL", "irql"),
            ("pool type", ""),
            ("size of allocation, bytes", ""),
        ],
        0x10: [("bad address (freed but never allocated)", "va"), ("0", ""), ("0", "")],
        0x11: [("current IRQL", "irql"), ("pool type", ""), _POOL],
        0x12: [("current IRQL", "irql"), ("pool type", ""), _POOL],
        0x13: [
            _RSV,
            ("pointer to pool header (double free)", "va"),
            ("pool header contents", ""),
        ],
        0x14: [
            _RSV,
            ("pointer to pool header (double free)", "va"),
            ("pool header contents", ""),
        ],
        0x15: [
            ("timer entry", "va"),
            ("pool type", ""),
            ("pool address being freed", "va"),
        ],
        0x16: [_RSV, _POOL, ("0", "")],
        0xA0: [
            ("IRP making the request (!irp)", "ptr"),
            ("device object of the lower device", "ptr"),
            ("", ""),
        ],
        0xA1: [
            ("IRP making the request (!irp)", "ptr"),
            ("device object of the lower device", "ptr"),
            ("", ""),
        ],
        0xA2: [
            ("IRP making the request (!irp)", "ptr"),
            ("device object of the lower device", "ptr"),
            ("", ""),
        ],
        0xC1: [("address of the driver dispatch routine", "ip"), ("", ""), ("", "")],
        0xFC: [
            ("address inside the driver making the incorrect API call", "ip"),
            ("", ""),
            ("", ""),
        ],
        0x1004: [
            ("", ""),
            ("thread that acquired the resource (!thread)", "ptr"),
            ("current thread (!thread)", "ptr"),
        ],
    },
    0xC9: {
        0x1: [("IRP being freed (!irp)", "ptr"), ("0", ""), ("0", "")],
        0x2: [("IRP being freed (!irp)", "ptr"), ("0", ""), ("0", "")],
        0x3: [("IRP being sent (!irp)", "ptr"), ("0", ""), ("0", "")],
        0x4: [_DEVOBJ, ("0", ""), ("0", "")],
        0x5: [
            ("device object of the offending driver", "ptr"),
            ("IRQL before IoCallDriver", "irql"),
            ("IRQL after IoCallDriver", "irql"),
        ],
        0x6: [
            ("IRP status", "ntstatus"),
            ("IRP being completed (!irp)", "ptr"),
            ("0", ""),
        ],
        0x7: [
            ("address of the cancel routine", "ip"),
            ("IRP being completed (!irp)", "ptr"),
            ("0", ""),
        ],
        0x8: [
            _DEVOBJ,
            ("IRP major function code", ""),
            ("exception status", "ntstatus"),
        ],
        0x9: [_DEVOBJ, ("I/O control code", ""), ("exception status", "ntstatus")],
        0xA: [_DEVOBJ, ("0", ""), ("0", "")],
        0xE: [("current IRQL", "irql"), _IRP, ("0", "")],
        0xF: [_DEVOBJ, _IRP, ("file object", "ptr")],
        0x10: [("current IRQL", "irql"), _RSV, _RSV],
        0x11: [
            ("fast I/O dispatch routine", "ip"),
            ("IRQL before the call", "irql"),
            ("current IRQL", "irql"),
        ],
        0x12: [
            ("dispatch routine", "ip"),
            ("IRQL before the call", "irql"),
            ("current IRQL", "irql"),
        ],
    },
    0x9F: {
        0x1: [_DEVOBJ, _RSV, _RSV],
        0x2: [
            ("target device object, if available", "ptr"),
            _DEVOBJ,
            ("driver object, if available", "ptr"),
        ],
        0x3: [
            ("PDO of the stack (!devstack)", "ptr"),
            ("nt!_TRIAGE_9F_POWER*", "ptr"),
            ("blocked IRP (!irp)", "ptr"),
        ],
        0x4: [
            ("time-out, seconds", ""),
            ("thread holding the PnP lock (!thread)", "ptr"),
            ("nt!_TRIAGE_9F_PNP*", "ptr"),
        ],
        0x5: [("PDO of the stack", "ptr"), ("POP_FX_DEVICE*", "ptr"), _RSV],
        0x6: [
            ("POP_FX_DEVICE*", "ptr"),
            ("1 directed power down, 0 power up", ""),
            _RSV,
        ],
        0x500: [_RSV, ("target device object, if available", "ptr"), _DEVOBJ],
    },
    0x133: {
        0x0: [
            ("DPC time count, ticks", ""),
            ("DPC time allotment, ticks", ""),
            ("DPC_WATCHDOG_GLOBAL_TRIAGE_BLOCK*", "ptr"),
        ],
        0x1: [
            ("watchdog period", ""),
            ("DPC_WATCHDOG_GLOBAL_TRIAGE_BLOCK*", "ptr"),
            _RSV,
        ],
    },
    0x1A: {
        0x404: [("expected PTE", "pte"), ("PTE contents", "pte"), ("PFN's PTE", "pte")],
        0x411: [("address of the PTE", "va"), ("", ""), ("", "")],
        0x3300: [("faulting address", "va"), ("", ""), ("", "")],
        0x41287: [("referenced virtual address", "va"), ("", ""), ("", "")],
        0x61941: [
            ("pointer to the faulting virtual address", "va"),
            ("", ""),
            ("", ""),
        ],
    },
}
# 값 → 이름 (코드, 0 기준 파라미터 번호). 서브타입 이름과 열거값.
PARAM_NAMES = {
    (0x50, 3): {
        0x0: "FREED_PTE",
        0x2: "NOT_PRESENT_PAGE_TABLE",
        0x3: "WRONG_SESSION",
        0x4: "VA_NOT_CANONICAL",
        0xF: "USER_VA_ACCESS_INCONSISTENT",
    },
    (0x7F, 0): {
        0x0: "divide by zero",
        0x1: "debug trap",
        0x3: "INT3 breakpoint",
        0x4: "overflow",
        0x5: "bounds check",
        0x6: "invalid opcode",
        0x7: "NPX not available",
        0x8: "double fault (kernel stack overflow or IST corruption)",
        0xA: "invalid TSS",
        0xB: "segment not present",
        0xC: "stack fault",
        0xD: "general protection fault",
        0xF: "reserved trap",
        0x10: "NPX error",
        0x11: "alignment check",
    },
    (0x139, 0): {
        0: "stack-based buffer overrun (legacy /GS)",
        1: "VTGuard illegal vtable",
        2: "stack cookie (/GS) violation",
        3: "LIST_ENTRY corrupted",
        5: "invalid parameter to a fatal function",
        6: "stack cookie not initialized by loader",
        7: "fatal program exit requested",
        8: "array bounds check failure",
        10: "indirect call guard (CFG) violation",
        11: "write guard invalid write",
        12: "invalid fiber context switch",
        13: "invalid register context assignment",
        14: "invalid object reference count",
        18: "longjmp to invalid jmp_buf",
        19: "unsafe modification of read-only data",
        21: "invalid exception chain",
        28: "out-of-bounds buffer access",
        29: "RTL_BALANCED_NODE corrupted",
        37: "out-of-range switch jumptable entry",
        38: "longjmp to invalid target",
        39: "export-suppressed call target",
    },
    (0x19, 0): {
        0x2: "pool block size mismatch",
        0x3: "freelist flink/blink corrupt",
        0x20: "pool block header size corrupt",
        0x21: "corruption after a pool block",
        0x22: "freed address not a pool block",
    },
    (0xC2, 0): {
        0x7: "attempt to free pool already freed",
        0x8: "paged pool allocation at IRQL > APC_LEVEL",
        0x9: "paged pool free at IRQL > APC_LEVEL",
        0xA: "free with a different pool tag",
        0xB: "quota process pointer bad",
        0x99: "freed address not a pool allocation",
        0x9B: "paged pool allocation at IRQL > APC_LEVEL",
        0x9C: "nonpaged pool allocation at IRQL > DISPATCH_LEVEL",
        0x9D: "invalid pool tag",
    },
    (0xC4, 0): {
        0x0: "zero-byte pool allocation",
        0x1: "paged allocation at IRQL > APC_LEVEL",
        0x2: "nonpaged allocation at IRQL > DISPATCH_LEVEL",
        0x10: "free of an address never allocated",
        0x11: "paged pool free at IRQL > APC_LEVEL",
        0x12: "nonpaged pool free at IRQL > DISPATCH_LEVEL",
        0x13: "double free",
        0x14: "double free",
        0x15: "pool freed with an active timer inside",
        0x16: "bad pool address or parameters",
        0xA0: "IRP read/write with bad buffer",
        0xC1: "dispatch routine violation",
        0xFC: "incorrect API call from a driver",
    },
    (0xC9, 0): {
        0x1: "free of a non-IRP object as IRP",
        0x2: "free of an IRP still associated with a thread",
        0x3: "IoCallDriver with a non-IRP",
        0x4: "IoCallDriver with invalid device object",
        0x5: "IRQL changed during dispatch",
        0x6: "IoCompleteRequest with STATUS_PENDING",
        0x7: "IoCompleteRequest with cancel routine still set",
        0x8: "IoBuildAsynchronousFsdRequest bad buffer",
        0x9: "IoBuildDeviceIoControlRequest bad buffer",
        0xA: "IoInitializeTimer on an initialized timer",
        0xE: "IoCompleteRequest above DISPATCH_LEVEL",
        0xF: "create request with a closed file object",
        0x10: "IoCallDriver above DISPATCH_LEVEL",
        0x11: "fast I/O call raised IRQL",
        0x12: "dispatch call raised IRQL",
    },
    (0x9F, 0): {
        0x1: "device object freed with an outstanding power request",
        0x2: "power IRP completed without PoStartNextPowerIrp",
        0x3: "device blocked a power IRP too long",
        0x4: "PnP sync timeout",
        0x5: "directed power transition timeout",
        0x6: "directed power transition callback failed",
        0x500: "power IRP completed without PoStartNextPowerIrp",
    },
    (0x133, 0): {
        0: "a single DPC or ISR exceeded its time allotment",
        1: "cumulative time at DISPATCH_LEVEL or above exceeded",
    },
    (0xEF, 1): {0: "a process terminated", 1: "a thread terminated"},
    (0x109, 3): {
        0x0: "generic data region",
        0x1: "function modification",
        0x2: "IDT",
        0x3: "GDT",
        0x4: "process list corruption (type 1)",
        0x5: "process list corruption (type 2)",
        0x6: "debug routine modification",
        0x7: "critical MSR modification",
        0x8: "object type",
        0xA: "system service function modification",
        0x19: "loaded module list modification",
        0x1C: "driver object corruption",
        0x27: "pool corruption (type 1)",
        0x28: "pool corruption (type 2)",
        0x29: "pool corruption (type 3)",
        0x101: "general pool corruption",
        0x102: "win32k.sys modification",
    },
    (0x124, 0): {
        0x0: "machine check exception",
        0x1: "corrected machine check",
        0x2: "corrected platform error",
        0x3: "NMI error",
        0x4: "uncorrectable PCI Express error",
        0x5: "generic hardware error",
        0x6: "initialization error",
        0x7: "BOOT error",
        0x8: "SCI generic error",
        0xC: "other error source v2",
        0xD: "SCI GHESv2",
        0xE: "BMC error info",
        0x10: "device driver error source",
        0x11: "Arm synchronous external abort",
        0x12: "Arm SError interrupt",
    },
}
IRQL_NAMES = {0: "PASSIVE_LEVEL", 1: "APC_LEVEL", 2: "DISPATCH_LEVEL"}
ACCESS = {
    "access": {0: "read", 1: "write", 2: "execute", 8: "execute"},
    "access50": {
        0: "read",
        1: "write (pre-1507)",
        2: "write",
        0xA: "execute",
        0x10: "execute",
    },
}
# KTRAP_FRAME — WinDbg `dt nt!_KTRAP_FRAME` 의 공통 부분. x86 은 dumplib 샘플(0x1000008E P3)로 실측: Eip == P2,
# &HardwareEsp == 컨텍스트 Esp. x64 는 아직 샘플로 대조하지 않았다 (VERIFY_PROMPT Step 2 의 `.trap` 비교가 그 자리).
KTRAP64 = {
    "size": 0x190,
    "Rax": (0x30, "<Q"),
    "Rcx": (0x38, "<Q"),
    "Rdx": (0x40, "<Q"),
    "R8": (0x48, "<Q"),
    "R9": (0x50, "<Q"),
    "R10": (0x58, "<Q"),
    "R11": (0x60, "<Q"),
    "Rbx": (0x140, "<Q"),
    "Rdi": (0x148, "<Q"),
    "Rsi": (0x150, "<Q"),
    "Rbp": (0x158, "<Q"),
    "ErrorCode": (0x160, "<Q"),
    "Rip": (0x168, "<Q"),
    "SegCs": (0x170, "<H"),
    "EFlags": (0x178, "<I"),
    "Rsp": (0x180, "<Q"),
    "SegSs": (0x188, "<H"),
}
KTRAP32 = {
    "size": 0x8C,
    "Edx": (0x3C, "<I"),
    "Ecx": (0x40, "<I"),
    "Eax": (0x44, "<I"),
    "Edi": (0x54, "<I"),
    "Esi": (0x58, "<I"),
    "Ebx": (0x5C, "<I"),
    "Ebp": (0x60, "<I"),
    "ErrCode": (0x64, "<I"),
    "Eip": (0x68, "<I"),
    "SegCs": (0x6C, "<I"),
    "EFlags": (0x70, "<I"),
    "HardwareEsp": (0x74, "<I"),
    "HardwareSegSs": (0x78, "<I"),
}
# x64 KPRCB: +0x8 CurrentThread, +0x24 Number (Win7~11 공통). x86: +0x4 CurrentThread, +0x10 LegacyNumber.
KPRCB = {
    64: {"CurrentThread": (0x8, "<Q"), "Number": (0x24, "<I")},
    32: {"CurrentThread": (0x4, "<I"), "Number": (0x10, "<B")},
}
EXCEPTION_CODES = {
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0x80000003: "STATUS_BREAKPOINT",
    0xC000001D: "STATUS_ILLEGAL_INSTRUCTION",
    0xC0000094: "STATUS_INTEGER_DIVIDE_BY_ZERO",
    0xC0000096: "STATUS_PRIVILEGED_INSTRUCTION",
    0xC00000FD: "STATUS_STACK_OVERFLOW",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
    0x80000004: "STATUS_SINGLE_STEP",
    0xC000008C: "STATUS_ARRAY_BOUNDS_EXCEEDED",
    0xC0000374: "STATUS_HEAP_CORRUPTION",
}


# ───────────────────────── 유틸 ─────────────────────────
class DumpError(Exception):
    pass


def _rd(buf: bytes, off: int, fmt: str):
    v = struct.unpack_from(fmt, buf, off)
    return v if len(v) > 1 else v[0]


def _fields(buf: bytes, table: dict, base: int = 0) -> dict:
    out = {}
    for name, (off, fmt) in table.items():
        if fmt is None:
            continue
        out[name] = _rd(buf, base + off, fmt)
    return out


def _utf16(b: bytes) -> str:
    return b.decode("utf-16-le", errors="replace").split("\0")[0]


def _hex(v) -> str:
    return f"0x{v:x}"


def filetime_str(ft: int) -> str:
    """FILETIME(100ns since 1601) → ISO. 0 이거나 터무니없으면 그대로."""
    import datetime

    try:
        if ft <= 0:
            return str(ft)
        return (
            datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(microseconds=ft // 10)
        ).strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    except (OverflowError, ValueError):
        return _hex(ft)


def duration_str(ft: int) -> str:
    s = ft // 10_000_000
    return f"{s // 86400}d {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def windows_name(build: int) -> str:
    """빌드 번호 → 흔히 부르는 이름 (근사치, 참고용)."""
    for lo, name in [
        (26100, "Windows 11 24H2+"),
        (22621, "Windows 11 22H2/23H2"),
        (22000, "Windows 11 21H2"),
        (19041, "Windows 10 2004..22H2"),
        (18362, "Windows 10 1903/1909"),
        (17763, "Windows 10 1809 / Server 2019"),
        (17134, "Windows 10 1803"),
        (16299, "Windows 10 1709"),
        (15063, "Windows 10 1703"),
        (14393, "Windows 10 1607 / Server 2016"),
        (10240, "Windows 10 1507/1511"),
        (9600, "Windows 8.1 / Server 2012 R2"),
        (9200, "Windows 8 / Server 2012"),
        (7601, "Windows 7 SP1 / Server 2008 R2 SP1"),
        (7600, "Windows 7"),
        (6002, "Vista SP2 / Server 2008"),
        (3790, "Server 2003"),
        (2600, "XP"),
    ]:
        if build >= lo:
            return name
    return "unknown"


def pe_ident(tds: int, size: int) -> str:
    """심볼 서버의 PE 식별자: TimeDateStamp 8자리 대문자 + SizeOfImage 대문자(패딩 없음)."""
    return f"{tds:08X}{size:X}"


def pdb_ident(guid_le: bytes, age: int) -> str:
    """심볼 서버의 PDB 식별자: GUID(레지스트리 표기 순서, 대문자, 하이픈 없음) + Age(16진, 패딩 없음).
    GUID 는 PE 의 RSDS 와 PDB 스트림 1 에 **같은 바이트 순서**(Windows GUID 구조체)로 들어 있다.
    문자열로 바꿀 때 `uuid.UUID(bytes_le=...)` 를 써야 WinDbg/symchk 표기와 같아진다."""
    return f"{uuid.UUID(bytes_le=bytes(guid_le)).hex.upper()}{age:X}"


# ───────────────────────── 데이터 모델 ─────────────────────────
@dataclass
class Module:
    name: str
    base: int
    size: int
    timestamp: int | None = None
    checksum: int | None = None
    entry: int | None = None
    path: str = ""
    pdb_name: str = ""
    pdb_ident: str = ""
    note: str = ""

    @property
    def end(self) -> int:
        return self.base + self.size

    @property
    def ident(self) -> str:
        return pe_ident(self.timestamp, self.size) if self.timestamp else ""

    def contains(self, addr: int) -> bool:
        return self.base <= addr < self.end


@dataclass
class Frame:
    slot: int  # 스택 슬롯의 가상 주소
    value: int  # 슬롯의 값 (복귀 주소 후보)
    module: str = ""
    rva: int | None = None
    symbol: str = ""
    note: str = ""
    # "ip" 컨텍스트 RIP · "fault-ip" 버그체크 파라미터가 가리킨 범인 명령 · "slot" 스택 슬롯 · "marker" 구간 표시줄
    kind: str = "slot"


# ───────────────────────── 커널 덤프 ─────────────────────────
class KernelDump:
    """PAGEDU64 / PAGEDUMP."""

    def __init__(self, path: str):
        self.path = path
        # 덤프는 수 GB 일 수 있어 통째로 읽지 않고 명령이 끝날 때까지 열어 둔다.
        self.f = open(path, "rb")  # noqa: SIM115
        self.size = os.fstat(self.f.fileno()).st_size
        sig = self.read(0, 8)
        if sig == b"PAGEDU64":
            self.bits, self.H, self.hsize, self.T = 64, H64, H64_SIZE, T64
        elif sig == b"PAGEDUMP":
            self.bits, self.H, self.hsize, self.T = 32, H32, H32_SIZE, T32
        else:
            raise DumpError(f"not a kernel dump (signature {sig!r})")
        self.hdr_raw = self.read(0, self.hsize)
        self.hdr = _fields(self.hdr_raw, self.H)
        self.dump_type = self.hdr["DumpType"]
        self.is_triage = self.dump_type == 4
        self.triage = (
            _fields(self.read(self.hsize, 0x80), self.T) if self.is_triage else None
        )
        self._phys: dict[int, int] | None = None  # pfn → file offset
        self._phys_note = ""
        self._modules: list[Module] | None = None

    # ── 원시 읽기
    def read(self, off: int, n: int) -> bytes:
        if off < 0 or off + n > self.size:
            raise DumpError(
                f"read outside file: off={_hex(off)} n={n} size={_hex(self.size)}"
            )
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) != n:
            raise DumpError(f"short read at {_hex(off)}")
        return b

    # ── 헤더
    def info(self) -> dict:
        h = self.hdr
        code = h["BugCheckCode"]
        name = BUGCHECKS.get(
            code, ("(not in table — see Microsoft bug check reference)", None)
        )[0]
        d = {
            "file": self.path,
            "file_size": self.size,
            "format": "PAGEDU64" if self.bits == 64 else "PAGEDUMP",
            "dump_type": self.dump_type,
            "dump_type_name": DUMP_TYPES.get(self.dump_type, "unknown"),
            "machine": MACHINES.get(h["MachineImageType"], _hex(h["MachineImageType"])),
            # 헤더의 MajorVersion 은 Win7 이후 늘 15(0xf), MinorVersion 이 빌드 번호다 (WinDbg 와 같은 표기).
            "os_build": h["MinorVersion"],
            "os_guess": windows_name(h["MinorVersion"]),
            "header_major_minor": [h["MajorVersion"], h["MinorVersion"]],
            "processors": h["NumberProcessors"],
            "product_type": h["ProductType"],
            "suite_mask": _hex(h["SuiteMask"]),
            "bugcheck": {
                "code": _hex(code),
                "name": name,
                "parameters": self.params(),
            },
            "directory_table_base": _hex(h["DirectoryTableBase"]),
            "ps_loaded_module_list": _hex(h["PsLoadedModuleList"]),
            "ps_active_process_head": _hex(h["PsActiveProcessHead"]),
            "kd_debugger_data_block": _hex(h["KdDebuggerDataBlock"]),
            "system_time": filetime_str(h["SystemTime"]),
            "system_uptime": duration_str(h["SystemUpTime"])
            if 0 < h["SystemUpTime"] < (1 << 62)
            else _hex(h["SystemUpTime"]),
            "comment": h["Comment"].split(b"\0")[0].decode("latin-1")
            if not h["Comment"].startswith(b"PAGE")
            else "",
        }
        ctx = self.context()
        if ctx:
            d["context"] = {k: _hex(v) for k, v in ctx.items()}
        d.update(self.faulting())
        exc = self.exception()
        if exc:
            d["exception"] = exc
        if self.triage:
            t = self.triage
            valid = (
                self.read(t["ValidOffset"], 4)
                if t["ValidOffset"] + 4 <= self.size
                else b""
            )
            d["triage"] = {
                k: (_hex(v) if isinstance(v, int) else v) for k, v in t.items()
            }
            d["triage"]["valid_marker_ok"] = valid == TRIAGE_VALID_MARK
            if not d["triage"]["valid_marker_ok"]:
                d["warnings"] = [
                    "triage ValidOffset does not point at 'TRGD' — header layout or file truncated"
                ]
            d["triage"]["broken_driver"] = self.broken_driver()
        return d

    # ── 버그체크 파라미터 해석
    def params(self) -> list[dict]:
        """네 파라미터를 표의 종류(kind)대로 해석한다 — 모델이 손으로 하던 `ln`·`.trap`·`.cxr` 를 대신한다.
        포인터 종류는 덤프에서 역참조하고(미니덤프에 없으면 "not captured" 를 그대로 보인다), 주소 종류는
        모듈·언로드 범위·널·비정규로 분류한다. 역참조 결과는 `decoded` 에, 열거값 이름은 `name` 에."""
        if getattr(self, "_params", None) is None:
            code = self.hdr["BugCheckCode"]
            values = list(self.hdr["BugCheckParameters"])
            spec = list(BUGCHECKS.get(code, ("", [("", "")] * 4))[1])
            if spec[0][1] == "sub" and values[0] in SUBTYPES.get(code, {}):
                spec = [spec[0]] + list(SUBTYPES[code][values[0]])
            out = []
            for i, (val, (desc, kind)) in enumerate(zip(values, spec)):
                row = {"value": _hex(val), "meaning": desc, "kind": kind}
                name = PARAM_NAMES.get((code, i), {}).get(val)
                if name:
                    row["name"] = name
                elif kind == "sub":
                    row["name"] = "(subtype not in table)"
                dec = self._decode_param(kind, val)
                if dec:
                    row["decoded"] = dec
                out.append(row)
            self._params = out
        return self._params

    def _decode_param(self, kind: str, val: int) -> dict | None:
        if kind in ("trap", "ctx", "exr", "prcb", "ptr") and val == 0:
            return None
        if kind == "ip":
            return self.where(val)
        if kind == "va":
            return self.classify_va(val)
        if kind == "irql":
            return {"irql": IRQL_NAMES.get(val, f"DIRQL {val}" if val > 2 else "")}
        if kind in ACCESS:
            return {"access": ACCESS[kind].get(val, f"unknown {_hex(val)}")}
        if kind == "ntstatus":
            n = EXCEPTION_CODES.get(val)
            return {"name": n} if n else None
        if kind == "trap":
            return self.read_trap(val)
        if kind == "ctx":
            return self.read_context_at(val)
        if kind == "exr":
            return self.read_exception_at(val)
        if kind == "prcb":
            return self.read_prcb(val)
        if kind == "ptr":
            return {
                "note": "kernel object pointer — WinDbg extension needed (the meaning names it)"
            }
        return None

    def where(self, addr: int) -> dict:
        """주소 → 적재 모듈(module+rva) 또는 언로드 드라이버 범위 또는 없음."""
        m = find_module(self.modules(), addr)
        if m:
            return {"module": m.name, "rva": _hex(addr - m.base)}
        for u in self.unloaded():
            if "start" in u and int(u["start"], 16) <= addr < int(u["end"], 16):
                return {
                    "module": "",
                    "unloaded_driver": u["name"],
                    "note": "inside an UNLOADED driver's former range",
                }
        return {"module": "", "note": "not in any loaded module"}

    def classify_va(self, va: int) -> dict:
        """참조된 데이터 주소의 성격 — 널 근처·비정규·모듈 안·언로드 범위·스택·그 밖의 커널/사용자 주소."""
        if va < 0x10000:
            return {"class": f"null or near-null pointer (offset {_hex(va)} from NULL)"}
        if self.bits == 64:
            if not (va < (1 << 47) or va >= 0xFFFF800000000000):
                return {"class": "non-canonical address (corrupt pointer)"}
            kernel = va >= 0xFFFF800000000000
        else:
            kernel = va >= 0x80000000
        d = self.where(va)
        if d.get("module"):
            d["class"] = "inside the image of {} (code or static data)".format(
                d["module"]
            )
            return d
        if d.get("unloaded_driver"):
            d["class"] = "inside an UNLOADED driver's former range"
            return d
        t = self.triage
        if t and t["TopOfStack"] <= va < t["TopOfStack"] + t["SizeOfCallStack"]:
            return {"class": "inside the crashing thread's captured stack"}
        return {
            "class": "kernel address outside any module (pool, stack, MDL or freed memory)"
            if kernel
            else "user-mode address"
        }

    def _read_struct(self, va: int, table: dict) -> tuple[dict | None, str]:
        """구조체를 통째로 읽고, 안 되면 필드별로 읽는다 — 미니덤프는 Esp 아래(커널 모드 트랩 프레임이 놓이는
        자리)를 싣지 않아 일부 필드만 있을 수 있다. 하나도 못 읽으면 (None, 이유)."""
        fields = {k: v for k, v in table.items() if k != "size"}
        try:
            return _fields(self.read_virt(va, table["size"]), fields), ""
        except DumpError as ex:
            whole = str(ex)
        out = {}
        for k, (off, fmt) in fields.items():
            try:
                out[k] = _rd(self.read_virt(va + off, struct.calcsize(fmt)), 0, fmt)
            except DumpError:
                pass
        if not out:
            return None, whole
        out["partial"] = f"only {len(out)} of {len(fields)} fields captured"
        return out, ""

    def read_trap(self, va: int) -> dict:
        """KTRAP_FRAME 역참조 (WinDbg `.trap`). x86 커널 모드 트랩의 Esp 는 HardwareEsp 자리의 주소 자체다."""
        T = KTRAP64 if self.bits == 64 else KTRAP32
        f, err = self._read_struct(va, T)
        if f is None:
            return {"error": err}
        out = {k: (_hex(v) if isinstance(v, int) else v) for k, v in f.items()}
        cs = f.get("SegCs")
        if cs is None:
            out["warning"] = (
                "SegCs/Eip not captured — only the fields above the stack pointer are in the dump"
            )
            return out
        out["mode"] = "kernel" if (cs & 3) == 0 else "user"
        if self.bits == 32:
            out["Esp"] = (
                _hex(va + KTRAP32["HardwareEsp"][0])
                if (cs & 3) == 0
                else out.get("HardwareEsp", "")
            )
        ip = f.get("Rip") if self.bits == 64 else f.get("Eip")
        if ip is not None:
            out["ip_where"] = self.where(ip)
        if cs not in (0x8, 0x10, 0x1B, 0x23, 0x33):
            out["warning"] = (
                f"SegCs {_hex(cs)} is not a Windows code selector — probably not a trap frame"
            )
        return out

    def read_context_at(self, va: int) -> dict:
        """CONTEXT 역참조 (WinDbg `.cxr`)."""
        tab = CTX64 if self.bits == 64 else CTX32
        f, err = self._read_struct(
            va, dict(tab, size=0x100 if self.bits == 64 else 0xD0)
        )
        if f is None:
            return {"error": err}
        out = {k: (_hex(v) if isinstance(v, int) else v) for k, v in f.items()}
        ip = f.get("Rip") if self.bits == 64 else f.get("Eip")
        if ip is not None:
            out["ip_where"] = self.where(ip)
        return out

    def read_exception_at(self, va: int) -> dict:
        """EXCEPTION_RECORD 역참조 (WinDbg `.exr`)."""
        try:
            raw = self.read_virt(va, 0x98 if self.bits == 64 else 0x50)
        except DumpError as ex:
            return {"error": str(ex)}
        if self.bits == 64:
            code, flags, _rec, addr, n = _rd(raw, 0, "<IIQQI")
            info = list(_rd(raw, 0x20, "<15Q"))
        else:
            code, flags, _rec, addr, n = _rd(raw, 0, "<IIIII")
            info = list(_rd(raw, 0x14, "<15I"))
        return {
            "code": _hex(code),
            "code_name": EXCEPTION_CODES.get(code, ""),
            "flags": _hex(flags),
            "address": _hex(addr),
            "address_where": self.where(addr),
            "parameters": [_hex(x) for x in info[: min(n, 15)]],
        }

    def read_prcb(self, va: int) -> dict:
        """KPRCB 의 현재 스레드와 번호 — 0x101 의 멈춘 프로세서. 버그체크를 올린 CPU 와 다른 CPU 다."""
        f, err = self._read_struct(va, dict(KPRCB[self.bits], size=0x28))
        if f is None:
            return {"error": err}
        return {
            "processor_number": f["Number"],
            "current_thread": _hex(f["CurrentThread"]),
            "note": f"the hung processor's thread — WinDbg: ~{f['Number']} k, or !thread {_hex(f['CurrentThread'])}",
        }

    def faulting(self) -> dict:
        """파라미터에서 얻은 진짜 문제 지점: `faulting_ip`(ip 종류)와 `faulting_context`(trap/ctx 종류의 레지스터).
        KeBugCheckEx 시점의 컨텍스트와 구별해서 보여 준다 — `stack` 이 이 Rsp 를 경계로 표시한다."""
        out = {}
        for i, p in enumerate(self.params()):
            dec = p.get("decoded") or {}
            if p["kind"] == "ip" and int(p["value"], 16) and "faulting_ip" not in out:
                out["faulting_ip"] = dict(
                    {"source": f"bugcheck parameter {i + 1}", "value": p["value"]},
                    **dec,
                )
            if (
                p["kind"] in ("trap", "ctx")
                and dec
                and "error" not in dec
                and "faulting_context" not in out
            ):
                ip = dec.get("Rip") or dec.get("Eip")
                sp = dec.get("Rsp") or dec.get("Esp")
                out["faulting_context"] = {
                    "source": f"bugcheck parameter {i + 1} ({'KTRAP_FRAME' if p['kind'] == 'trap' else 'CONTEXT'})",
                    "ip": ip,
                    "sp": sp,
                    "ip_where": dec.get("ip_where"),
                    "registers": {
                        k: v
                        for k, v in dec.items()
                        if k not in ("ip_where", "mode", "warning")
                    },
                }
                if dec.get("warning"):
                    out["faulting_context"]["warning"] = dec["warning"]
        return out

    def _ctx_off(self) -> int:
        return (
            self.triage["ContextOffset"] if self.triage else self.H["ContextRecord"][0]
        )

    def context(self) -> dict | None:
        tab = CTX64 if self.bits == 64 else CTX32
        off = self._ctx_off()
        try:
            raw = self.read(off, 0x100 if self.bits == 64 else 0xD0)
        except DumpError:
            return None
        return _fields(raw, tab)

    def exception(self) -> dict | None:
        off = self.triage["ExceptionOffset"] if self.triage else self.H["Exception"][0]
        try:
            raw = self.read(off, 0x98 if self.bits == 64 else 0x50)
        except DumpError:
            return None
        if self.bits == 64:
            code, flags, _rec, addr, n = _rd(raw, 0, "<IIQQI")
            info = list(_rd(raw, 0x20, "<15Q"))
        else:
            code, flags, _rec, addr, n = _rd(raw, 0, "<IIIII")
            info = list(_rd(raw, 0x14, "<15I"))
        return {
            "code": _hex(code),
            "code_name": EXCEPTION_CODES.get(code, ""),
            "flags": _hex(flags),
            "address": _hex(addr),
            "parameters": [_hex(x) for x in info[: min(n, 15)]],
        }

    # ── 미니덤프: 드라이버 목록
    def _dump_string(self, off: int) -> str:
        n = _rd(self.read(off, 4), 0, "<I")
        if n > 1024:
            return f"<bad string length {n} at {_hex(off)}>"
        return _utf16(self.read(off + 4, n * 2))

    def modules(self) -> list[Module]:
        if self._modules is None:
            self._modules = (
                self._modules_triage() if self.is_triage else self._modules_full()
            )
        return self._modules

    def _modules_triage(self) -> list[Module]:
        t, L = self.triage, (DRV64 if self.bits == 64 else DRV32)
        q = "<Q" if self.bits == 64 else "<I"
        out = []
        for i in range(t["DriverCount"]):
            e = self.read(t["DriverListOffset"] + i * L["size"], L["size"])
            name_off = _rd(e, L["NameOffset"], "<I")
            path = (
                self._dump_string(name_off) if 0 < name_off < self.size else "<no name>"
            )
            m = Module(
                name=path.rsplit("\\", 1)[-1],
                path=path,
                base=_rd(e, L["DllBase"], q),
                size=_rd(e, L["SizeOfImage"], "<I"),
                entry=_rd(e, L["EntryPoint"], q),
                checksum=_rd(e, L["CheckSum"], "<I"),
                timestamp=_rd(e, L["TimeDateStamp"], "<I"),
            )
            out.append(m)
        return out

    def unloaded(self) -> list[dict]:
        if not self.triage or not self.triage["UnloadedDriversOffset"]:
            return []
        U = UNL64 if self.bits == 64 else UNL32
        off = self.triage["UnloadedDriversOffset"]
        count = _rd(self.read(off, 4), 0, "<I")
        if count > 256:
            return [{"error": f"implausible unloaded count {count}"}]
        out = []
        for i in range(count):
            e = self.read(off + U["hdr"] + i * U["size"], U["size"])
            ln = _rd(e, U["Length"], "<H")
            out.append(
                {
                    "name": _utf16(e[U["Name"] : U["Name"] + min(U["NameBytes"], ln)]),
                    "start": _hex(_rd(e, U["Start"], U["ptr"])),
                    "end": _hex(_rd(e, U["End"], U["ptr"])),
                }
            )
        return out

    def broken_driver(self) -> str:
        """커널이 크래시 때 지목한 드라이버 항목(BrokenDriverOffset) — WinDbg 의 힌트와 같은 출처."""
        off = self.triage["BrokenDriverOffset"] if self.triage else 0
        if not off:
            return ""
        L = DRV64 if self.bits == 64 else DRV32
        try:
            e = self.read(off, L["size"])
            name_off = _rd(e, L["NameOffset"], "<I")
            return (
                self._dump_string(name_off)
                if 0 < name_off < self.size
                else "<unreadable>"
            )
        except DumpError:
            return "<unreadable>"

    def data_blocks(self) -> list[tuple[int, int, int]]:
        """미니덤프에 실린 작은 메모리 조각들: (va, file_off, size)."""
        if not self.triage or not self.triage["DataBlocksOffset"]:
            return []
        out = []
        off, n = self.triage["DataBlocksOffset"], self.triage["DataBlocksCount"]
        for i in range(min(n, 4096)):
            va, fo, sz = _rd(self.read(off + i * 16, 16), 0, "<QII")
            out.append((va, fo, sz))
        return out

    # ── 전체 덤프: 물리 페이지 → 파일 오프셋
    def _build_phys(self) -> None:
        if self._phys is not None:
            return
        if self.bits != 64:
            raise DumpError(
                "physical memory mapping is implemented for 64-bit dumps only"
            )
        pm: dict[int, int] = {}
        sig = self.read(H64_SIZE, 4)
        if (
            self.dump_type in (8, 9, 0xA)
            or sig[1:4] == b"DMP"
            and self.read(H64_SIZE + 4, 4) == b"RDMP"
        ):
            # RDMP: 0x2000 Marker(0x40) 'RDMP' 'DUMP' pad | MetadataSize u64 | FirstPageOffset u64 | ...
            _marker, s1, _s2 = _rd(self.read(H64_SIZE, 12), 0, "<I4s4s")
            if s1 != b"RDMP":
                raise DumpError(
                    f"RDMP signature missing for dump type {self.dump_type}"
                )
            meta_size, first_page = _rd(self.read(H64_SIZE + 0x10, 16), 0, "<QQ")
            total = None
            if self.dump_type == 0xA:
                total = _rd(self.read(H64_SIZE + 0x28, 8), 0, "<Q")
            ranges = self.read(H64_SIZE + 0x30, meta_size)
            page_off, count = first_page, 0
            for i in range(0, len(ranges) - 15, 16):
                pfn, npages = _rd(ranges, i, "<QQ")
                if total is not None and count >= total:
                    break
                if pfn == 0:
                    break
                count += npages
                for j in range(npages):
                    pm[pfn + j] = page_off
                    page_off += PAGE
            self._phys_note = f"RDMP ranges={len(ranges) // 16} pages={len(pm)}"
        elif sig in (b"SDMP", b"FDMP"):
            first_page, present, nbits = _rd(self.read(H64_SIZE + 0x20, 24), 0, "<QQQ")
            bitmap = self.read(H64_SIZE + 0x38, (nbits + 7) // 8)
            idx = 0
            for byte_i, b in enumerate(bitmap):
                if not b:
                    continue
                for bit in range(8):
                    if b >> bit & 1:
                        pm[byte_i * 8 + bit] = first_page + idx * PAGE
                        idx += 1
            self._phys_note = (
                f"bitmap {sig.decode()} present={present} mapped={len(pm)}"
            )
        elif self.dump_type in (1, 2):
            nruns = _rd(self.hdr_raw, 0x88, "<I")
            if nruns == 0x45474150:
                raise DumpError(
                    "physical memory descriptor is 'PAGE' filler — no run table"
                )
            off = H64_SIZE
            for r in range(nruns):
                base, count = _rd(self.hdr_raw, 0x98 + r * 16, "<QQ")
                for j in range(count):
                    pm[base + j] = off
                    off += PAGE
            self._phys_note = f"runs={nruns} pages={len(pm)}"
        else:
            raise DumpError(
                f"no physical memory layout for dump type {self.dump_type} ({DUMP_TYPES.get(self.dump_type, '?')})"
            )
        self._phys = pm

    def read_phys(self, pa: int, n: int) -> bytes:
        self._build_phys()
        out = b""
        while n:
            pfn, inpage = pa >> 12, pa & 0xFFF
            fo = self._phys.get(pfn)
            if fo is None:
                raise DumpError(f"physical page {_hex(pfn << 12)} not in dump")
            take = min(n, PAGE - inpage)
            out += self.read(fo + inpage, take)
            pa += take
            n -= take
        return out

    def virt_to_phys(self, va: int, dtb: int | None = None) -> int:
        """x64 4단 페이지 테이블. 1GB/2MB 큰 페이지 처리."""
        cr3 = (dtb or self.hdr["DirectoryTableBase"]) & ~0xFFF
        pml4e = _rd(self.read_phys(cr3 + ((va >> 39) & 0x1FF) * 8, 8), 0, "<Q")
        if not pml4e & 1:
            raise DumpError(f"VA {_hex(va)}: PML4E not present")
        pdpte = _rd(
            self.read_phys(
                ((pml4e >> 12) & 0xFFFFFFFFF) << 12 | ((va >> 30) & 0x1FF) * 8, 8
            ),
            0,
            "<Q",
        )
        if not pdpte & 1:
            raise DumpError(f"VA {_hex(va)}: PDPTE not present")
        if pdpte & 0x80:
            return (((pdpte >> 30) & 0x3FFFF) << 30) | (va & 0x3FFFFFFF)
        pde = _rd(
            self.read_phys(
                ((pdpte >> 12) & 0xFFFFFFFFF) << 12 | ((va >> 21) & 0x1FF) * 8, 8
            ),
            0,
            "<Q",
        )
        if not pde & 1:
            raise DumpError(f"VA {_hex(va)}: PDE not present")
        if pde & 0x80:
            return (((pde >> 21) & 0x7FFFFFF) << 21) | (va & 0x1FFFFF)
        pte = _rd(
            self.read_phys(
                ((pde >> 12) & 0xFFFFFFFFF) << 12 | ((va >> 12) & 0x1FF) * 8, 8
            ),
            0,
            "<Q",
        )
        if not pte & 1:
            raise DumpError(f"VA {_hex(va)}: PTE not present (paged out / transition)")
        return (((pte >> 12) & 0xFFFFFFFFF) << 12) | (va & 0xFFF)

    def read_virt(self, va: int, n: int) -> bytes:
        """가상 주소 읽기. 미니덤프는 스택·데이터 블록 조각에서, 전체 덤프는 페이지 테이블로."""
        if self.is_triage:
            return self._read_virt_triage(va, n)
        out = b""
        while n:
            take = min(n, PAGE - (va & 0xFFF))
            out += self.read_phys(self.virt_to_phys(va), take)
            va += take
            n -= take
        return out

    def _read_virt_triage(self, va: int, n: int) -> bytes:
        t = self.triage
        pieces = [(t["TopOfStack"], t["CallStackOffset"], t["SizeOfCallStack"])]
        if t.get("DataPageAddress"):
            pieces.append(
                (t["DataPageAddress"], t["DataPageOffset"], t["DataPageSize"])
            )
        pieces += self.data_blocks()
        for bva, fo, sz in pieces:
            if bva <= va and va + n <= bva + sz:
                return self.read(fo + (va - bva), n)
        raise DumpError(f"VA {_hex(va)} not captured in this triage dump")

    # ── 전체 덤프: PsLoadedModuleList 순회
    def _unicode_string(self, va: int) -> str:
        raw = self.read_virt(va, 16)
        ln, _mx, buf = _rd(raw, 0, "<HH4xQ")
        if ln == 0 or ln > 1024 or not buf:
            return ""
        return _utf16(self.read_virt(buf, ln))

    def _modules_full(self) -> list[Module]:
        if self.bits != 64:
            raise DumpError("module list walk is implemented for 64-bit dumps only")
        head = self.hdr["PsLoadedModuleList"]
        out = []
        cur = _rd(self.read_virt(head, 8), 0, "<Q")  # Flink
        seen = set()
        while cur and cur != head and cur not in seen and len(out) < 4096:
            seen.add(cur)
            try:
                e = self.read_virt(cur, 0x80)
            except DumpError as ex:
                out.append(Module(name=f"<list broken: {ex}>", base=0, size=0))
                break
            base, entry, size = (
                _rd(e, KLDR64["DllBase"], "<Q"),
                _rd(e, KLDR64["EntryPoint"], "<Q"),
                _rd(e, KLDR64["SizeOfImage"], "<I"),
            )
            m = Module(
                name="",
                base=base,
                size=size,
                entry=entry,
                checksum=_rd(e, KLDR64["CheckSum"], "<I"),
            )
            try:
                m.name = self._unicode_string(cur + KLDR64["BaseDllName"])
                m.path = self._unicode_string(cur + KLDR64["FullDllName"])
            except DumpError:
                m.note = "name unreadable"
            # 타임스탬프·PDB 식별자는 메모리의 PE 헤더에서 — 구조체 버전 차이에 안 흔들린다.
            try:
                pe = parse_pe(
                    lambda off, n, b=base: self.read_virt(b + off, n), in_memory=True
                )
                m.timestamp, m.pdb_name, m.pdb_ident = (
                    pe["timestamp"],
                    pe["pdb_name"],
                    pe["pdb_ident"],
                )
                if pe["size_of_image"] and pe["size_of_image"] != size:
                    m.note = "SizeOfImage differs: list {} pe {}".format(
                        _hex(size), _hex(pe["size_of_image"])
                    )
            except (DumpError, struct.error, ValueError) as ex:
                m.note = (
                    m.note + "; " if m.note else ""
                ) + f"PE header not readable ({str(ex)[:60]})"
            out.append(m)
            cur = _rd(e, 0, "<Q")
        return out

    # ── 스택
    def stack_words(
        self, max_bytes: int = 0x10000, sp: int | None = None
    ) -> list[tuple[int, int]]:
        """(슬롯 VA, 값) 목록. 미니덤프는 실린 스택 전체(`sp` 를 주면 거기부터), 전체 덤프는 Rsp(또는 `sp`)
        부터 읽히는 데까지."""
        w = 8 if self.bits == 64 else 4
        fmt = "<Q" if w == 8 else "<I"
        if self.triage:
            t = self.triage
            raw = self.read(t["CallStackOffset"], t["SizeOfCallStack"])
            top = t["TopOfStack"]
            if sp is not None:
                if not (top <= sp < top + len(raw)):
                    raise DumpError(
                        f"sp {_hex(sp)} is outside the captured stack [{_hex(top)}, {_hex(top + len(raw))})"
                    )
                raw, top = raw[sp - top :], sp
        else:
            if sp is None:
                ctx = self.context() or {}
                sp = ctx.get("Rsp") or ctx.get("Esp")
            if not sp:
                return []
            top = sp & ~(w - 1)
            raw = b""
            va = top
            while len(raw) < max_bytes:
                take = PAGE - (va & 0xFFF)
                try:
                    raw += self.read_virt(va, take)
                except DumpError:
                    break
                va += take
        return [(top + i, _rd(raw, i, fmt)) for i in range(0, len(raw) - w + 1, w)]


# ───────────────────────── 사용자 모드 미니덤프 (MDMP) ─────────────────────────
class UserMinidump:
    STREAMS: ClassVar[dict[int, str]] = {
        3: "ThreadList",
        4: "ModuleList",
        5: "MemoryList",
        6: "Exception",
        7: "SystemInfo",
        9: "Memory64List",
        14: "UnloadedModuleList",
        15: "MiscInfo",
        21: "ThreadInfoList",
    }
    ARCH: ClassVar[dict[int, str]] = {0: "x86", 5: "arm", 9: "x64", 12: "arm64"}

    def __init__(self, path: str):
        self.path = path
        self.data = Path(path).read_bytes()
        if self.data[:4] != b"MDMP":
            raise DumpError("not an MDMP minidump")
        _ver, nstreams, dir_rva, _csum, tds, flags = _rd(self.data, 4, "<IIIIIQ")
        self.timestamp, self.flags = tds, flags
        self.streams = {}
        for i in range(nstreams):
            typ, sz, rva = _rd(self.data, dir_rva + i * 12, "<III")
            self.streams[typ] = (rva, sz)

    def _string(self, rva: int) -> str:
        n = _rd(self.data, rva, "<I")
        return _utf16(self.data[rva + 4 : rva + 4 + n])

    def info(self) -> dict:
        d = {
            "file": self.path,
            "file_size": len(self.data),
            "format": "MDMP",
            "dump_type_name": "user-mode minidump",
            "time": filetime_str(self.timestamp * 10_000_000 + 116444736000000000),
            "flags": _hex(self.flags),
            "streams": [self.STREAMS.get(t, str(t)) for t in self.streams],
        }
        if 7 in self.streams:
            rva, _ = self.streams[7]
            arch, _lvl, _rev, nproc, _ptype, maj, mn, build, _plat = _rd(
                self.data, rva, "<HHHBBIIII"
            )
            d["machine"] = self.ARCH.get(arch, str(arch))
            d["os_version"] = f"{maj}.{mn} build {build}"
            d["processors"] = nproc
        if 6 in self.streams:
            rva, _ = self.streams[6]
            tid, code, _flg, _rec, addr, n = _rd(self.data, rva, "<I4xIIQQI")
            info = list(_rd(self.data, rva + 8 + 0x20, "<15Q"))
            d["exception"] = {
                "thread_id": tid,
                "code": _hex(code),
                "code_name": EXCEPTION_CODES.get(code, ""),
                "address": _hex(addr),
                "parameters": [_hex(x) for x in info[: min(n, 15)]],
            }
        d["hint"] = (
            "for thread stacks use rust-minidump's minidump-stackwalk; "
            "this tool gives header, modules, exception and symbolize"
        )
        return d

    def modules(self) -> list[Module]:
        if 4 not in self.streams:
            return []
        rva, _ = self.streams[4]
        n = _rd(self.data, rva, "<I")
        out = []
        for i in range(n):
            e = rva + 4 + i * 108
            base, size, csum, tds, name_rva = _rd(self.data, e, "<QIIII")
            cv_sz, cv_rva = _rd(self.data, e + 24 + 52, "<II")
            path = self._string(name_rva)
            m = Module(
                name=path.rsplit("\\", 1)[-1],
                path=path,
                base=base,
                size=size,
                checksum=csum,
                timestamp=tds,
            )
            if cv_sz >= 24 and self.data[cv_rva : cv_rva + 4] == b"RSDS":
                m.pdb_name = (
                    self.data[cv_rva + 24 : cv_rva + cv_sz]
                    .split(b"\0")[0]
                    .decode("latin-1")
                    .rsplit("\\", 1)[-1]
                )
                m.pdb_ident = pdb_ident(
                    self.data[cv_rva + 4 : cv_rva + 20],
                    _rd(self.data, cv_rva + 20, "<I"),
                )
            out.append(m)
        return out

    def _arch_bits(self) -> int:
        if 7 in self.streams:
            arch = _rd(self.data, self.streams[7][0], "<H")
            return 32 if arch in (0, 5) else 64
        return 64

    @property
    def bits(self) -> int:
        return self._arch_bits()

    def crash_thread_id(self) -> int | None:
        if 6 in self.streams:
            return _rd(self.data, self.streams[6][0], "<I")
        return None

    def stack_words(self, max_bytes: int = 0x10000) -> list[tuple[int, int]]:
        """예외 스레드(없으면 첫 스레드)의 스택 메모리 — MINIDUMP_THREAD.Stack 이 가리키는 조각."""
        if 3 not in self.streams:
            return []
        rva, _ = self.streams[3]
        n = _rd(self.data, rva, "<I")
        want = self.crash_thread_id()
        chosen = None
        for i in range(n):
            e = rva + 4 + i * 48
            tid = _rd(self.data, e, "<I")
            if chosen is None or tid == want:
                chosen = e
                if tid == want:
                    break
        if chosen is None:
            return []
        start, size, mem_rva = _rd(self.data, chosen + 16, "<QII")
        raw = self.data[mem_rva : mem_rva + min(size, max_bytes)]
        w = 8 if self.bits == 64 else 4
        fmt = "<Q" if w == 8 else "<I"
        return [(start + i, _rd(raw, i, fmt)) for i in range(0, len(raw) - w + 1, w)]

    def context(self) -> dict | None:
        """예외 주소를 명령 포인터로 — 스레드 CONTEXT 스트림은 아키텍처별 레이아웃이라 여기선 안 푼다."""
        if 6 in self.streams:
            rva, _ = self.streams[6]
            addr = _rd(self.data, rva + 8 + 16, "<Q")
            return {"Rip" if self.bits == 64 else "Eip": addr}
        return None

    is_triage = False


def open_dump(path: str):
    with open(path, "rb") as fh:
        sig = fh.read(8)
    if sig[:4] == b"MDMP":
        return UserMinidump(path)
    if sig in (b"PAGEDU64", b"PAGEDUMP"):
        return KernelDump(path)
    if sig[:4] == b"HIBR" or sig[:4] == b"hibr":
        raise DumpError(
            "hiberfil.sys (HIBR) — not supported; use Volatility 3 / MemProcFS"
        )
    raise DumpError(f"unknown dump signature {sig!r}")


# ───────────────────────── PE ─────────────────────────
def parse_pe(reader, in_memory: bool = False) -> dict:
    """``reader(off, n)`` 로 PE 를 읽어 식별자와 RSDS 를 돌려준다.
    in_memory=True 면 오프셋을 RVA 로 취급(적재 이미지), False 면 파일 오프셋(디스크 PE)."""
    dos = reader(0, 0x40)
    if dos[:2] != b"MZ":
        raise ValueError("no MZ")
    e_lfanew = _rd(dos, 0x3C, "<I")
    if e_lfanew > 0x1000:
        raise ValueError("e_lfanew too large")
    hdr = reader(e_lfanew, 0x200)
    if hdr[:4] != b"PE\0\0":
        raise ValueError("no PE signature")
    machine, nsec, tds, _, _, optsz, _chars = _rd(hdr, 4, "<HHIIIHH")
    opt = 24
    magic = _rd(hdr, opt, "<H")
    is64 = magic == 0x20B
    size_of_image = _rd(hdr, opt + 56, "<I")
    checksum = _rd(hdr, opt + 64, "<I")
    ddir = opt + (112 if is64 else 96)
    dbg_rva, dbg_sz = _rd(hdr, ddir + 6 * 8, "<II")
    secs = []
    so = opt + optsz
    sec_raw = reader(e_lfanew + so, nsec * 40)
    for i in range(nsec):
        name, vs, va, rs, rp = _rd(sec_raw, i * 40, "<8sIIII")
        secs.append(
            {
                "name": name.rstrip(b"\0").decode("latin-1"),
                "va": va,
                "vsize": vs,
                "raw_ptr": rp,
                "raw_size": rs,
            }
        )

    def rva_to_off(r):
        if in_memory:
            return r
        for s in secs:
            if s["va"] <= r < s["va"] + max(s["vsize"], s["raw_size"]):
                return s["raw_ptr"] + (r - s["va"])
        raise ValueError(f"RVA {_hex(r)} in no section")

    out = {
        "machine": MACHINES.get(machine, _hex(machine)),
        "timestamp": tds,
        "size_of_image": size_of_image,
        "checksum": checksum,
        "sections": secs,
        "pe_ident": pe_ident(tds, size_of_image),
        "pdb_name": "",
        "pdb_guid_le": b"",
        "pdb_age": None,
        "pdb_ident": "",
        "is64": is64,
    }
    if dbg_rva and dbg_sz:
        dd = reader(rva_to_off(dbg_rva), dbg_sz)
        for i in range(dbg_sz // 28):
            _, _, _, _, typ, sz, addr, ptr = _rd(dd, i * 28, "<IIHHIIII")
            if typ == 2 and sz >= 24:  # IMAGE_DEBUG_TYPE_CODEVIEW
                cv = reader(addr if in_memory else ptr, sz)
                if cv[:4] == b"RSDS":
                    out["pdb_guid_le"] = cv[4:20]
                    out["pdb_age"] = _rd(cv, 20, "<I")
                    out["pdb_name"] = (
                        cv[24:].split(b"\0")[0].decode("latin-1").rsplit("\\", 1)[-1]
                    )
                    out["pdb_ident"] = pdb_ident(out["pdb_guid_le"], out["pdb_age"])
    return out


def parse_pe_file(path: str) -> dict:
    data = Path(path).read_bytes()
    return parse_pe(lambda off, n: data[off : off + n])


# ───────────────────────── PDB (MSF 7) ─────────────────────────
class Pdb:
    """공개 심볼(S_PUB32)·프로시저(S_GPROC32/S_LPROC32)·섹션 헤더·OMAP 을 읽어 RVA → 이름."""

    S_PUB32, S_LPROC32, S_GPROC32 = 0x110E, 0x110F, 0x1110

    def __init__(self, path: str):
        self.path = path
        self.data = Path(path).read_bytes()
        if not self.data.startswith(b"Microsoft C/C++ MSF 7.00\r\n\x1aDS"):
            raise ValueError("not an MSF 7.00 PDB")
        self.block_size, _, _nblocks, dir_bytes, _, block_map = _rd(
            self.data, 0x20, "<IIIIII"
        )
        nblk = (dir_bytes + self.block_size - 1) // self.block_size
        idx = _rd(self.data, block_map * self.block_size, f"<{nblk}I")
        idx = (idx,) if isinstance(idx, int) else idx
        directory = b"".join(self._block(b) for b in idx)[:dir_bytes]
        nstreams = _rd(directory, 0, "<I")
        sizes = (
            list(_rd(directory, 4, f"<{nstreams}I"))
            if nstreams > 1
            else [_rd(directory, 4, "<I")]
        )
        pos = 4 + 4 * nstreams
        self.streams = []
        for sz in sizes:
            if sz == 0xFFFFFFFF:
                self.streams.append((0, ()))
                continue
            n = (sz + self.block_size - 1) // self.block_size
            blocks = _rd(directory, pos, f"<{n}I") if n else ()
            blocks = (blocks,) if isinstance(blocks, int) else blocks
            pos += 4 * n
            self.streams.append((sz, blocks))
        # 스트림 1: PDB 정보
        s1 = self.stream(1)
        self.version, self.signature, self.age = _rd(s1, 0, "<III")
        self.guid_le = s1[12:28]
        # 스트림 3: DBI
        dbi = self.stream(3)
        (
            self.dbi_sig,
            self.dbi_ver,
            self.dbi_age,
            _gs,
            _build,
            _ps,
            _dllver,
            self.sym_stream,
            _rbld,
            mod_sz,
            sc_sz,
            secmap_sz,
            src_sz,
            ts_sz,
            _mfc,
            dbg_sz,
            ec_sz,
            _flags,
            self.dbi_machine,
        ) = _rd(dbi, 0, "<iIIHHHHHHiiiiiIiiHH")
        self._mod_info = dbi[64 : 64 + mod_sz]
        dbg_off = 64 + mod_sz + sc_sz + secmap_sz + src_sz + ts_sz + ec_sz
        self.dbg = {}
        if dbg_sz >= 22:
            names = [
                "FPO",
                "Exception",
                "Fixup",
                "OmapToSrc",
                "OmapFromSrc",
                "SectionHdr",
                "TokenRidMap",
                "Xdata",
                "Pdata",
                "NewFPO",
                "SectionHdrOrig",
            ]
            vals = _rd(dbi, dbg_off, "<11H")
            self.dbg = {n: v for n, v in zip(names, vals) if v != 0xFFFF}
        self._sections = None
        self._omap_to_src = None
        self._symbols = None

    def _block(self, i: int) -> bytes:
        return self.data[i * self.block_size : (i + 1) * self.block_size]

    def stream(self, i: int) -> bytes:
        sz, blocks = self.streams[i]
        return b"".join(self._block(b) for b in blocks)[:sz]

    @property
    def ident(self) -> str:
        return pdb_ident(self.guid_le, self.age)

    # ── 섹션 헤더 (OMAP 있으면 원본 섹션)
    def sections(self) -> list[tuple[int, int]]:
        if self._sections is None:
            key = (
                "SectionHdrOrig"
                if "OmapToSrc" in self.dbg and "SectionHdrOrig" in self.dbg
                else "SectionHdr"
            )
            raw = self.stream(self.dbg[key]) if key in self.dbg else b""
            self._sections = [
                (_rd(raw, i + 12, "<I"), _rd(raw, i + 8, "<I"))
                for i in range(0, len(raw) - 39, 40)
            ]  # (va, vsize)
        return self._sections

    def seg_off_to_rva(self, seg: int, off: int) -> int | None:
        secs = self.sections()
        if 1 <= seg <= len(secs):
            return secs[seg - 1][0] + off
        return None

    # ── OMAP: 최종 이미지 RVA → 원본(심볼 공간) RVA
    def to_src(self, rva: int) -> int | None:
        if "OmapToSrc" not in self.dbg:
            return rva
        if self._omap_to_src is None:
            raw = self.stream(self.dbg["OmapToSrc"])
            self._omap_to_src = [_rd(raw, i, "<II") for i in range(0, len(raw) - 7, 8)]
        import bisect

        tbl = self._omap_to_src
        i = bisect.bisect_right([e[0] for e in tbl], rva) - 1
        if i < 0:
            return None
        src, dst = tbl[i]
        if dst == 0:
            return None
        return dst + (rva - src)

    # ── 심볼 (원본 RVA 기준으로 정렬)
    def symbols(self) -> list[tuple[int, str, str]]:
        if self._symbols is not None:
            return self._symbols
        syms: list[tuple[int, str, str]] = []
        for rva, name in self._iter_records(
            self.stream(self.sym_stream), pubs_only=True
        ):
            syms.append((rva, name, "pub"))
        for rva, name in self._iter_procs():
            syms.append((rva, name, "proc"))
        syms.sort()
        self._symbols = syms
        return syms

    def _iter_records(self, raw: bytes, pubs_only: bool):
        pos = 0
        while pos + 4 <= len(raw):
            ln, kind = _rd(raw, pos, "<HH")
            if ln < 2:
                break
            body = raw[pos + 4 : pos + 2 + ln]
            if kind == self.S_PUB32 and len(body) >= 10:
                _flags, off, seg = _rd(body, 0, "<IIH")
                name = body[10:].split(b"\0")[0].decode("utf-8", "replace")
                rva = self.seg_off_to_rva(seg, off)
                if rva is not None:
                    yield rva, name
            elif (
                not pubs_only
                and kind in (self.S_GPROC32, self.S_LPROC32)
                and len(body) >= 35
            ):
                off, seg = _rd(body, 28, "<IH")
                name = body[35:].split(b"\0")[0].decode("utf-8", "replace")
                rva = self.seg_off_to_rva(seg, off)
                if rva is not None:
                    yield rva, name
            pos += 2 + ln

    def _iter_procs(self):
        """모듈 심볼 스트림의 S_GPROC32/S_LPROC32 — 비공개(private) PDB 에만 있다."""
        mi, pos = self._mod_info, 0
        while pos + 64 <= len(mi):
            sym_stream, sym_bytes = _rd(mi, pos + 34, "<H"), _rd(mi, pos + 36, "<I")
            end = pos + 64
            end = mi.index(b"\0", end) + 1  # ModuleName
            end = mi.index(b"\0", end) + 1  # ObjFileName
            pos = (end + 3) & ~3
            if sym_stream == 0xFFFF or sym_stream >= len(self.streams) or sym_bytes < 4:
                continue
            raw = self.stream(sym_stream)[4:sym_bytes]
            yield from self._iter_records(raw, pubs_only=False)

    def lookup(self, rva: int) -> tuple[str, int, str] | None:
        """최종 RVA → (이름, 오프셋, 종류). OMAP 을 거친다."""
        import bisect

        src = self.to_src(rva)
        if src is None:
            return None
        syms = self.symbols()
        keys = [s[0] for s in syms]
        i = bisect.bisect_right(keys, src) - 1
        if i < 0:
            return None
        s_rva, name, kind = syms[i]
        return name, src - s_rva, kind


# ───────────────────────── 심볼 저장소 ─────────────────────────
class SymbolStore:
    """``<symdir>/<file>/<ident>/<file>`` (심볼 서버 배치) 와 ``<symdir>/<file>`` (평면) 둘 다 찾는다."""

    def __init__(self, symdir: str | None):
        self.root = Path(symdir) if symdir else None
        self._pdb_cache: dict[str, Pdb | None] = {}

    def find(self, name: str, ident: str) -> Path | None:
        if not self.root or not name:
            return None
        cands = []
        if ident:
            cands.append(self.root / name / ident / name)
            cands += (
                list((self.root / name).glob(f"{ident[:16]}*/{name}"))
                if (self.root / name).is_dir()
                else []
            )
        cands.append(self.root / name)
        for c in cands:
            if c.is_file():
                return c
        # 대소문자 무시 (Linux 파일시스템)
        if self.root.is_dir():
            low = name.lower()
            for p in self.root.rglob("*"):
                if (
                    p.is_file()
                    and p.name.lower() == low
                    and (not ident or ident.lower() in str(p.parent).lower())
                ):
                    return p
        return None

    def pdb_for(
        self, m: Module, explicit_pdb: str | None = None, explicit_pe: str | None = None
    ) -> tuple[Pdb | None, list[str]]:
        """모듈의 PDB 를 찾고 GUID 로 검증. 반환: (Pdb|None, 추적 줄들)."""
        trail = []
        pe_info = None
        pe_path = Path(explicit_pe) if explicit_pe else self.find(m.name, m.ident)
        if pe_path:
            try:
                pe_info = parse_pe_file(str(pe_path))
                trail.append(
                    "pe: {} (ident {}, pdb {} {})".format(
                        pe_path,
                        pe_info["pe_ident"],
                        pe_info["pdb_name"],
                        pe_info["pdb_ident"],
                    )
                )
                if m.timestamp and pe_info["timestamp"] != m.timestamp:
                    trail.append(
                        "WARNING: PE TimeDateStamp {:08X} != dump {:08X} — wrong build on disk".format(
                            pe_info["timestamp"], m.timestamp
                        )
                    )
            except (OSError, ValueError, struct.error) as ex:
                trail.append(f"pe: {pe_path} unreadable ({ex})")
        else:
            trail.append(
                "pe: not found in symdir for {}/{}".format(m.name, m.ident or "?")
            )
        pdb_name = (
            (pe_info or {}).get("pdb_name") or m.pdb_name or Path(m.name).stem + ".pdb"
        )
        want_ident = (pe_info or {}).get("pdb_ident") or m.pdb_ident
        want_guid = (pe_info or {}).get("pdb_guid_le") or b""
        pdb_path = (
            Path(explicit_pdb) if explicit_pdb else self.find(pdb_name, want_ident)
        )
        if not pdb_path:
            trail.append(
                "pdb: not found ({} {})".format(
                    pdb_name, want_ident or "ident unknown — need the PE to know it"
                )
            )
            return None, trail
        key = str(pdb_path)
        if key not in self._pdb_cache:
            try:
                self._pdb_cache[key] = Pdb(key)
            except (OSError, ValueError, struct.error) as ex:
                trail.append(f"pdb: {pdb_path} unreadable ({ex})")
                self._pdb_cache[key] = None
        pdb = self._pdb_cache[key]
        if pdb is None:
            return None, trail
        trail.append(
            f"pdb: {pdb_path} (guid ident {pdb.ident}, info age {pdb.age}, dbi age {pdb.dbi_age}, "
            f"omap {'yes' if 'OmapToSrc' in pdb.dbg else 'no'})"
        )
        if want_guid and bytes(want_guid) != bytes(pdb.guid_le):
            trail.append(
                "MISMATCH: PDB GUID differs from the PE's RSDS GUID — refusing to symbolize with it"
            )
            return None, trail
        if want_ident and want_ident[:32] == pdb.ident[:32] and want_ident != pdb.ident:
            trail.append(
                f"note: GUID matches, Age differs (PE {want_ident[32:]} vs PDB {pdb.ident[32:]}) — accepted (WinDbg accepts this too)"
            )
        if not want_guid and not want_ident:
            trail.append(
                "note: no PE/RSDS to verify against — PDB accepted by file name only (UNVERIFIED)"
            )
        return pdb, trail


# ───────────────────────── 분석 ─────────────────────────
def find_module(mods: list[Module], addr: int) -> Module | None:
    for m in mods:
        if m.size and m.contains(addr):
            return m
    return None


def scan_stack(dump, mods: list[Module]) -> list[Frame]:
    """컨텍스트 RIP + 스택 스캔. 버그체크 파라미터가 진짜 문제 지점을 가리키면(`faulting_ip`·`faulting_context`)
    그 명령을 `fault-ip` 프레임으로 넣고, 그 Rsp 자리에 `marker` 를 세운다 — 같은 스택이면 경계선, 다른
    스택이면 따로 스캔해서 뒤에 붙인다. WinDbg `k` 가 `.trap`/`.cxr` 뒤에 보여 주는 것과 같은 구간이다."""
    frames = []
    ctx = dump.context() or {}
    ip = ctx.get("Rip") or ctx.get("Eip")

    def frame(kind, slot, val, note=""):
        m = find_module(mods, val)
        return Frame(
            slot=slot,
            value=val,
            module=m.name if m else "",
            rva=val - m.base if m else None,
            note=note,
            kind=kind,
        )

    if ip:
        src = (
            "exception address"
            if isinstance(dump, UserMinidump)
            else "RIP/EIP from context"
        )
        frames.append(frame("ip", 0, ip, src))
    fsp = None
    src = ""
    if isinstance(dump, KernelDump):
        fault = dump.faulting()
        fc, fi = fault.get("faulting_context"), fault.get("faulting_ip")
        if fc and fc.get("ip") and fc.get("sp"):
            fip, fsp, src = int(fc["ip"], 16), int(fc["sp"], 16), fc["source"]
        elif fi:
            fip, src = int(fi["value"], 16), fi["source"]
        else:
            fip = None
        if fip and fip != ip:
            frames.append(frame("fault-ip", 0, fip, f"faulting instruction from {src}"))
    marked = False
    for slot, val in dump.stack_words():
        if fsp is not None and not marked and slot >= fsp:
            frames.append(
                Frame(
                    slot=fsp,
                    value=0,
                    kind="marker",
                    note=f"faulting sp {_hex(fsp)} from {src} — slots from here up belong to the faulting frames",
                )
            )
            marked = True
        m = find_module(mods, val)
        if m:
            frames.append(Frame(slot=slot, value=val, module=m.name, rva=val - m.base))
    if fsp is not None and not marked:
        try:
            words = dump.stack_words(sp=fsp)
            frames.append(
                Frame(
                    slot=fsp,
                    value=0,
                    kind="marker",
                    note=f"faulting stack from {src} (sp {_hex(fsp)}) is a different stack — scanned separately below",
                )
            )
            for slot, val in words:
                m = find_module(mods, val)
                if m:
                    frames.append(
                        Frame(slot=slot, value=val, module=m.name, rva=val - m.base)
                    )
        except DumpError as ex:
            frames.append(
                Frame(
                    slot=fsp,
                    value=0,
                    kind="marker",
                    note=f"faulting stack from {src} not readable: {ex}",
                )
            )
    return frames


def symbolize(
    frames: list[Frame],
    mods: list[Module],
    store: SymbolStore,
    explicit: dict[str, tuple[str | None, str | None]] | None = None,
) -> list[str]:
    """프레임에 심볼을 채운다. 반환: 모듈별 추적 줄(사람이 어디서 끊겼는지 보게)."""
    trail = []
    by_mod: dict[str, Pdb | None] = {}
    explicit = explicit or {}
    for f in frames:
        if not f.module or f.rva is None:
            continue
        if f.module not in by_mod:
            m = next(x for x in mods if x.name == f.module)
            pe_p, pdb_p = explicit.get(f.module.lower(), (None, None))
            pdb, t = store.pdb_for(m, explicit_pdb=pdb_p, explicit_pe=pe_p)
            by_mod[f.module] = pdb
            trail.append(
                "[{}] base {} size {} ident {}".format(
                    m.name, _hex(m.base), _hex(m.size), m.ident or "?"
                )
            )
            trail += ["    " + line for line in t]
        pdb = by_mod[f.module]
        if pdb is None:
            f.symbol = ""
            continue
        # 복귀 주소는 call 다음 명령 — 경계에서 앞 함수로 붙지 않게 1 을 뺀다 (RIP 자체는 그대로).
        is_ip = f.kind in ("ip", "fault-ip")
        q = f.rva if is_ip else f.rva - 1
        hit = pdb.lookup(q)
        if hit:
            name, off, kind = hit
            off = off + (0 if is_ip else 1)
            f.symbol = f"{name}+0x{off:x}" if off else name
            if kind == "pub" and off > 0x10000:
                f.note = (
                    (f.note + "; " if f.note else "")
                    + "offset >64KB from a public symbol — likely a static function the public PDB does not name"
                )
        else:
            f.symbol = "(no symbol covers this rva)"
    return trail


# ───────────────────────── 출력 ─────────────────────────
def print_kv(d: dict, indent: int = 0):
    pad = " " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            print(f"{pad}{k}:")
            print_kv(v, indent + 2)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"{pad}{k}:")
            for i, item in enumerate(v):
                if "value" in item and "meaning" in item:
                    kind = (" <{}>".format(item["kind"])) if item.get("kind") else ""
                    print(
                        f"{pad}  [{i + 1}] {item['value']:<18} {item['meaning']}{kind}"
                    )
                    if item.get("name"):
                        print("{}      = {}".format(pad, item["name"]))
                    for dk, dv in (item.get("decoded") or {}).items():
                        print(f"{pad}      {dk}: {dv}")
                else:
                    print_kv(item, indent + 2)
        else:
            print(f"{pad}{k}: {v}")


def mod_rows(mods: list[Module]) -> list[dict]:
    return [
        {
            "name": m.name,
            "base": _hex(m.base),
            "end": _hex(m.end),
            "size": _hex(m.size),
            "timestamp": (f"{m.timestamp:08X}") if m.timestamp else "",
            "checksum": (f"{m.checksum:08X}") if m.checksum else "",
            "pe_ident": m.ident,
            "pdb": m.pdb_name,
            "pdb_ident": m.pdb_ident,
            "path": m.path,
            "note": m.note,
        }
        for m in mods
    ]


def print_modules(mods: list[Module]):
    print(
        f"{'name':<28} {'base':<18} {'end':<18} {'size':<9} {'timestamp':<9} {'pe_ident':<16} pdb"
    )
    for m in sorted(mods, key=lambda x: x.base):
        ts = f"{m.timestamp:08X}" if m.timestamp else "-"
        pdb = f"{m.pdb_name} {m.pdb_ident}" if m.pdb_name else ""
        note = ("# " + m.note) if m.note else ""
        print(
            f"{m.name[:28]:<28} {_hex(m.base):<18} {_hex(m.end):<18} {_hex(m.size):<9} {ts:<9} "
            f"{(m.ident or '-'):<16} {pdb} {note}"
        )


def print_frames(frames: list[Frame], bits: int):
    w = 16 if bits == 64 else 8
    print(f"{'slot':<{w + 2}}  {'value':<{w + 2}}  module+rva  symbol  note")
    for f in frames:
        if f.kind == "marker":
            print(f"---- {f.note} ----")
            continue
        slot = {"ip": "context", "fault-ip": "fault-ip"}.get(
            f.kind
        ) or f"0x{f.slot:0{w}x}"
        where = f"{f.module}+0x{f.rva:x}" if f.module else "(not in any module)"
        note = ("# " + f.note) if f.note else ""
        print(f"{slot:<{w + 2}}  0x{f.value:0{w}x}  {where:<40} {f.symbol} {note}")


# ───────────────────────── 명령 ─────────────────────────
def cmd_info(a):
    d = open_dump(a.dump)
    out = d.info()
    if isinstance(d, KernelDump) and d.is_triage:
        out["unloaded_drivers"] = d.unloaded()
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print_kv(out)
    return 0


def cmd_modules(a):
    d = open_dump(a.dump)
    mods = d.modules()
    if a.json:
        print(json.dumps(mod_rows(mods), indent=2))
    else:
        print_modules(mods)
        if isinstance(d, KernelDump) and not d.is_triage:
            print("# physical map:", d._phys_note)
    return 0


def cmd_stack(a):
    d = open_dump(a.dump)
    mods = d.modules()
    frames = scan_stack(d, mods)
    if a.json:
        print(json.dumps([asdict(f) for f in frames], indent=2))
    else:
        print(
            "# scan of stack slots whose value falls inside a loaded module — candidates, NOT an unwound call chain"
        )
        print_frames(frames, d.bits)
    return 0


def _explicit_map(pairs: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """--pe name=path / --pdb name=path 를 모듈 이름(소문자) → (pe, pdb) 로."""
    out: dict[str, list] = {}
    for kind, items in pairs:
        for it in items or []:
            if "=" not in it:
                raise SystemExit(f"--{kind} expects <module>=<path>, got {it!r}")
            name, path = it.split("=", 1)
            out.setdefault(name.lower(), [None, None])[0 if kind == "pe" else 1] = path
    return {k: (v[0], v[1]) for k, v in out.items()}


def cmd_symbolize(a):
    d = open_dump(a.dump)
    mods = d.modules()
    frames = scan_stack(d, mods)
    store = SymbolStore(a.symdir)
    trail = symbolize(
        frames, mods, store, _explicit_map([("pe", a.pe), ("pdb", a.pdb)])
    )
    if a.json:
        print(
            json.dumps(
                {"frames": [asdict(f) for f in frames], "symbol_trail": trail}, indent=2
            )
        )
    else:
        print(
            "# symbol resolution trail (addr → module base → rva → pdb → symbol); fix the first broken link"
        )
        for line in trail:
            print(line)
        print()
        print_frames(frames, d.bits)
    return 0


def cmd_pdbcheck(a):
    pe = parse_pe_file(a.pe)
    pdb = Pdb(a.pdb)
    guid_ok = bytes(pe["pdb_guid_le"]) == bytes(pdb.guid_le)
    verdict = "MATCH" if guid_ok else "MISMATCH"
    age_note = ""
    if guid_ok and pe["pdb_age"] not in (pdb.age, pdb.dbi_age):
        age_note = "GUID equal but Age differs (PE {}, PDB info {}, DBI {}) — still the same PDB lineage; symbols are usable".format(
            pe["pdb_age"], pdb.age, pdb.dbi_age
        )
    pdb_file = pe["pdb_name"] or Path(a.pdb).name
    if age_note:
        note = age_note
    elif guid_ok:
        note = (
            "GUIDs are compared as raw 16 bytes; both sides store the Windows GUID struct, "
            "so no byte-order conversion is applied"
        )
    else:
        note = "the PDB was built from a different link of this image; get the PDB whose ident is wanted_pdb_ident"
    out = {
        "pe": {
            "path": a.pe,
            "machine": pe["machine"],
            "timestamp": "{:08X}".format(pe["timestamp"]),
            "size_of_image": _hex(pe["size_of_image"]),
            "pe_ident": pe["pe_ident"],
            "rsds_pdb_name": pe["pdb_name"],
            "rsds_guid": str(uuid.UUID(bytes_le=bytes(pe["pdb_guid_le"])))
            if pe["pdb_guid_le"]
            else "",
            "rsds_age": pe["pdb_age"],
            "wanted_pdb_ident": pe["pdb_ident"],
        },
        "pdb": {
            "path": a.pdb,
            "guid": str(uuid.UUID(bytes_le=bytes(pdb.guid_le))),
            "info_age": pdb.age,
            "dbi_age": pdb.dbi_age,
            "pdb_ident": pdb.ident,
            "signature": _hex(pdb.signature),
            "has_omap": "OmapToSrc" in pdb.dbg,
            "dbg_streams": sorted(pdb.dbg),
            "symbol_server_path": f"{pdb_file}/{pdb.ident}/{pdb_file}",
        },
        "verdict": verdict,
        "note": note,
    }
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print_kv(out)
    return 0 if guid_ok else 1


def cmd_fetch(a):
    d = open_dump(a.dump)
    mods = d.modules()
    root = Path(a.symdir)
    root.mkdir(parents=True, exist_ok=True)
    server = a.server.rstrip("/")
    only = {x.lower() for x in a.only} if a.only else None
    ok, fail = 0, 0
    for m in sorted(mods, key=lambda x: x.base):
        if only and m.name.lower() not in only:
            continue
        if not m.name or not m.timestamp or not m.size:
            print("skip %s: no TimeDateStamp/SizeOfImage" % (m.name or _hex(m.base)))
            continue
        pe_path = root / m.name / m.ident / m.name
        got_pe = pe_path.is_file() or _download(
            f"{server}/{m.name}/{m.ident}/{m.name}", pe_path
        )
        if not got_pe:
            print(
                f"PE   {m.name:<28} {m.ident}  not on server (third-party driver? use its build output)"
            )
            fail += 1
            if not m.pdb_ident:
                continue
        pdb_name, ident = m.pdb_name, m.pdb_ident
        if got_pe:
            try:
                info = parse_pe_file(str(pe_path))
                pdb_name, ident = info["pdb_name"], info["pdb_ident"]
            except (ValueError, struct.error) as ex:
                print(f"PE   {m.name:<28} unreadable: {ex}")
        if not pdb_name:
            print(f"PDB  {m.name:<28} no RSDS in PE")
            continue
        pdb_path = root / pdb_name / ident / pdb_name
        got = pdb_path.is_file() or _download(
            f"{server}/{pdb_name}/{ident}/{pdb_name}", pdb_path
        )
        print(
            f"{'PDB ' if got else 'PDB?'} {m.name:<28} {pdb_name} {ident} {'ok' if got else 'not on server'}"
        )
        ok += got
        fail += not got
    print(f"fetched/present: {ok}, missing: {fail}  (symdir {root})")
    return 0


def _download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Microsoft-Symbol-Server/10.0.0.0"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except Exception as ex:
        if "404" not in str(ex):
            print(f"   ({url}: {str(ex)[:80]})")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


# ───────────────────────── 자기 검사 (합성 덤프) ─────────────────────────
def build_synthetic_triage64() -> bytes:
    """레이아웃 표대로 만든 가짜 PAGEDU64 미니덤프. 파서가 표와 어긋나면 여기서 잡힌다."""
    buf = bytearray(b"PAGE" * (0x12000 // 4))

    def put(off, fmt, *v):
        struct.pack_into(fmt, buf, off, *v)

    put(0, "<4s4sII", b"PAGE", b"DU64", 15, 19041)
    put(
        0x10,
        "<QQQQ",
        0x1AD000,
        0xFFFF960000000000,
        0xFFFFF80012345678,
        0xFFFFF80012340000,
    )
    put(0x30, "<III", 0x8664, 4, 0xD1)
    put(0x40, "<4Q", 0xFFFFC00000001000, 2, 1, 0xFFFFF8001000A000)
    put(0x80, "<Q", 0xFFFFF80012300000)
    put(0xF98, "<I", 4)
    put(0xFA8, "<q", 133000000000000000)
    put(0x1030, "<q", 36000000000)
    # triage header
    T = 0x2000
    put(T + 0x04, "<I", 0x12000)
    put(T + 0x08, "<I", 0x11FFC)
    put(T + 0x0C, "<II", 0x348, 0xF00)
    put(T + 0x18, "<I", 0x3000)  # unloaded
    put(T + 0x28, "<II", 0x4000, 0x40)  # stack
    put(T + 0x30, "<II", 0x5000, 2)  # drivers
    put(T + 0x38, "<II", 0x6000, 0x100)
    put(T + 0x40, "<I", 0x5000 + 0x90)  # broken driver = second entry
    put(T + 0x48, "<Q", 0xFFFFA000DEAD0000)
    put(T + 0x78, "<II", 0x7000, 1)
    put(0x11FFC, "<4s", b"TRGD")
    # context: Rip inside driver 2, Rsp = TopOfStack
    put(0x348 + 0xF8, "<Q", 0xFFFFF8001000A010)
    put(0x348 + 0x98, "<Q", 0xFFFFA000DEAD0000)
    put(0xF00, "<IIQQI", 0xC0000005, 1, 0, 0xFFFFF8001000A010, 2)
    put(0xF20, "<QQ", 1, 0xFFFFC00000001000)
    # stack: slot0 junk, slot1 return into driver1, slot2 into driver2
    put(0x4000, "<QQQ", 0x1234, 0xFFFFF80012345000 + 0x1230, 0xFFFFF8001000A000 + 0x44)
    buf[0x4018:0x4040] = b"\0" * 0x28
    # drivers
    for i, (name_off, base, size, ts) in enumerate(
        [
            (0x6000, 0xFFFFF80012300000, 0x100000, 0x5507A73C),
            (0x6060, 0xFFFFF80010000000, 0x20000, 0x60000000),
        ]
    ):
        e = 0x5000 + i * 0x90
        buf[e : e + 0x90] = b"\0" * 0x90
        put(e + 0x00, "<I", name_off)
        put(e + 0x38, "<QQI", base, base + 0x1000, size)
        put(e + 0x80, "<I", 0x1111 * (i + 1))
        put(e + 0x88, "<I", ts)
    for off, s in [
        (0x6000, "\\SystemRoot\\system32\\ntoskrnl.exe"),
        (0x6060, "\\??\\C:\\drv\\mydrv.sys"),
    ]:
        put(off, "<I", len(s))
        buf[off + 4 : off + 4 + 2 * len(s) + 2] = s.encode("utf-16-le") + b"\0\0"
    # unloaded: 1 entry
    buf[0x3000:0x3040] = b"\0" * 0x40
    put(0x3000, "<I", 1)
    put(0x3008, "<HH", 22, 22)
    buf[0x3018 : 0x3018 + 22] = "USBSTOR.SYS".encode("utf-16-le")
    put(0x3030, "<QQ", 0xFFFFF80014000000, 0xFFFFF80014020000)
    # data block: 16 bytes at some VA
    put(0x7000, "<QII", 0xFFFFF80012300000, 0x7100, 0x40)
    buf[0x7100:0x7140] = b"MZ" + b"\0" * 0x3E
    return bytes(buf)


def cmd_selftest(a):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "synthetic.dmp"
        p.write_bytes(build_synthetic_triage64())
        d = KernelDump(str(p))
        info = d.info()
        assert info["bugcheck"]["code"] == "0xd1" and info["bugcheck"][
            "name"
        ].startswith("DRIVER_IRQL"), info["bugcheck"]
        assert info["triage"]["valid_marker_ok"], info["triage"]
        assert info["triage"]["broken_driver"].endswith("mydrv.sys"), info["triage"][
            "broken_driver"
        ]
        mods = d.modules()
        assert [m.name for m in mods] == ["ntoskrnl.exe", "mydrv.sys"], mods
        assert mods[0].timestamp == 0x5507A73C and mods[0].ident == "5507A73C100000", (
            mods[0]
        )
        assert mods[1].checksum == 0x2222
        unl = d.unloaded()
        assert unl == [
            {
                "name": "USBSTOR.SYS",
                "start": "0xfffff80014000000",
                "end": "0xfffff80014020000",
            }
        ], unl
        frames = scan_stack(d, mods)
        got = [(f.kind, f.module, f.rva) for f in frames]
        assert got == [
            ("ip", "mydrv.sys", 0xA010),
            ("fault-ip", "mydrv.sys", 0xA000),
            ("slot", "ntoskrnl.exe", 0x46230),
            ("slot", "mydrv.sys", 0xA044),
        ], got
        ps = info["bugcheck"]["parameters"]
        assert ps[0]["decoded"]["class"].startswith("kernel address outside"), ps[0]
        assert ps[1]["decoded"] == {"irql": "DISPATCH_LEVEL"} and ps[2]["decoded"] == {
            "access": "write"
        }, ps[1:3]
        assert ps[3]["decoded"] == {"module": "mydrv.sys", "rva": "0xa000"}, ps[3]
        assert (
            info["faulting_ip"]["module"] == "mydrv.sys"
            and "faulting_context" not in info
        ), info["faulting_ip"]
        assert d.read_virt(0xFFFFF80012300000, 2) == b"MZ"
        assert d.read_virt(0xFFFFA000DEAD0008, 8) == struct.pack(
            "<Q", 0xFFFFF80012346230
        )
        exc = d.exception()
        assert exc["code_name"] == "STATUS_ACCESS_VIOLATION" and exc["parameters"] == [
            "0x1",
            "0xffffc00000001000",
        ], exc
    print(
        "selftest OK (synthetic PAGEDU64 triage dump: header, triage, drivers, strings, "
        "unloaded, stack scan, data blocks, bugcheck parameter decode)"
    )
    return 0


# ───────────────────────── main ─────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kdump", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in [
        ("info", cmd_info, "header, bugcheck, context, exception"),
        ("modules", cmd_modules, "loaded drivers with symbol-server identifiers"),
        ("stack", cmd_stack, "stack scan → module+rva candidates"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("dump")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser(
        "symbolize", help="stack scan + PDB names; prints the resolution trail"
    )
    p.add_argument("dump")
    p.add_argument("--symdir", help="symbol store root (symbol-server layout or flat)")
    p.add_argument(
        "--pe",
        action="append",
        metavar="MODULE=PATH",
        help="explicit PE for a module (repeatable)",
    )
    p.add_argument(
        "--pdb",
        action="append",
        metavar="MODULE=PATH",
        help="explicit PDB for a module (repeatable)",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_symbolize)
    p = sub.add_parser(
        "pdbcheck", help="does this PDB belong to this PE? (GUID/Age from both sides)"
    )
    p.add_argument("pe")
    p.add_argument("pdb")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_pdbcheck)
    p = sub.add_parser(
        "fetch-symbols", help="download PE+PDB of the dump's modules into a symdir"
    )
    p.add_argument("dump")
    p.add_argument("--symdir", required=True)
    p.add_argument("--server", default="https://msdl.microsoft.com/download/symbols")
    p.add_argument("--only", nargs="*", help="module names to fetch (default: all)")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser(
        "selftest", help="parse a synthetic dump built from the layout table"
    )
    p.set_defaults(fn=cmd_selftest)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except DumpError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    except (ValueError, struct.error) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
