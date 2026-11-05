---
name: windows-dump
description: Windows crash dump triage on Linux or macOS (kernel minidump, MEMORY.DMP, user-mode .dmp) with the bundled kdump.py — bugcheck parameters decoded and dereferenced, loaded drivers, stack scan, PDB matching, symbols. Use for any Windows .dmp, blue screen or driver crash instead of writing a dump parser or matching PDBs by hand.
argument-hint: "<dump path> [--symdir <dir>] [--pdb drv.sys=<path> --pe drv.sys=<path>]"
allowed-tools: [shell, read_file, write_file]
---

Triage the Windows crash dump named in: $ARGUMENTS

`${SKILL_DIR}/kdump.py` is a single-file, standard-library Python tool (3.10+). It
reads the dump for you and prints every intermediate value, so a wrong step is
visible instead of silently producing a plausible-looking name.

**Do not write your own parser for these files and do not match PDBs or compute
symbol offsets by hand.** Both failed before: a hand-written parser mis-read the
layout, and a hand comparison declared a correct PDB "mismatched" because the
PDB's info-stream Age (5) differed from the PE's Age (2) while the GUIDs were
equal. `pdbcheck` settles that in one call.

## Commands

```
python3 ${SKILL_DIR}/kdump.py info <dump>                  # bugcheck + decoded parameters, OS build, registers, exception
python3 ${SKILL_DIR}/kdump.py modules <dump>               # loaded drivers: base, size, TimeDateStamp, symbol-server idents
python3 ${SKILL_DIR}/kdump.py stack <dump>                 # stack scan: slots whose value lands inside a module → module+rva
python3 ${SKILL_DIR}/kdump.py symbolize <dump> --symdir D  # same, with names from PDBs; prints the resolution trail
python3 ${SKILL_DIR}/kdump.py pdbcheck <pe> <pdb>          # is this PDB the one for this PE? (GUID/Age from both sides)
python3 ${SKILL_DIR}/kdump.py fetch-symbols <dump> --symdir D [--server URL] [--only ntoskrnl.exe hal.dll]
python3 ${SKILL_DIR}/kdump.py selftest                     # parse a synthetic dump built from the layout table
```

Every command takes `--json` for machine-readable output. Exit code 2 means the
dump could not be read as claimed; the message says which structure.

## Recipe: a kernel crash from `C:\Windows\Minidump\*.dmp` or `MEMORY.DMP`

1. `info`. Read, in this order:
   - `bugcheck.code`, `bugcheck.name`, then the four parameters. Each parameter
     carries a `kind` and, when the tool could follow it, a `decoded` block:
     an instruction address becomes `module` + `rva`; a referenced data address
     is classified (null or near-null, non-canonical, inside a module, inside an
     unloaded driver's former range, inside the captured stack, pool or user
     mode); an IRQL, access type or exception code gets its name; a pointer to a
     trap frame or context record is dereferenced into registers. A subtype
     parameter gets its `name` when the table knows the value.
   - `faulting_ip`: the instruction the bugcheck itself blamed (for example
     parameter 4 of 0xD1 or parameter 2 of 0x7E). This is the culprit line.
   - `faulting_context`: registers at the real fault, read from the trap frame
     or context record a parameter pointed to (0x7E, 0x8E, 0x3B, 0x139). The
     header context of a full dump is the `KeBugCheckEx` call site, not the
     fault, so prefer this block when it exists. If it says a structure was not
     captured, that is a fact about the dump, not an error to work around.
   - `exception`, and for minidumps `triage.broken_driver` (the driver the
     kernel itself blamed; empty means it did not name one) and
     `unloaded_drivers`: a stack address inside one of those ranges means a
     driver crashed after unloading.
2. `modules`: the loaded driver list with `pe_ident` (TimeDateStamp+SizeOfImage).
   Full dumps also show each module's PDB name and `pdb_ident` from the in-memory
   PE header.
3. `stack`: a **scan**, not an unwound call chain. Each line is a stack slot whose
   value falls inside a loaded module. Read it top-down: the first lines are the
   most recent frames. Stale values from earlier calls also appear; treat repeats
   and values ending in `+0x0` with suspicion. Row kinds: `context` is the
   instruction pointer from the header context (for a bugcheck usually
   `KeBugCheckEx`), `fault-ip` is the instruction from `faulting_ip` or
   `faulting_context`, and a `----` marker line shows where the faulting
   frames begin (or that the faulting stack was a different stack and was
   scanned separately below the marker).
4. Symbols. With a symbol store or network access:
   `fetch-symbols <dump> --symdir ./sym` downloads PE+PDB for every module the
   Microsoft server knows. Third-party and in-house drivers come back "not on
   server": put their build output under `./sym/<name>.pdb/<ident>/<name>.pdb`
   or pass `--pdb mydrv.sys=path/to/mydrv.pdb --pe mydrv.sys=path/to/mydrv.sys`.
   Then `symbolize <dump> --symdir ./sym`.
5. Read the **symbol resolution trail** before the frames. For each module it
   prints `pe:` (found or not, its ident, the PDB it wants), `pdb:` (found or
   not, GUID ident, info age, DBI age, OMAP) and verdict lines (`MISMATCH`,
   `note:`). Fix the first broken link; do not reinterpret the addresses.

The address-to-name chain the tool follows, so you can audit it: address → module
whose `[base, base+size)` contains it (from the dump's own module list, never from
the PE's ImageBase) → `rva = addr - base` → PDB matched by GUID → section-relative
symbol offsets converted with the PDB's section headers → OMAP translation when
the PDB has one → nearest preceding symbol. Return addresses are looked up at
`rva-1` so a call at the end of a function is not attributed to the next one.

Offsets larger than 64 KB from a public symbol are flagged: Microsoft public PDBs
name only global functions, so the frame is probably inside a static function the
PDB does not name. WinDbg shows the same thing.

## Recipe: a user-mode `.dmp` (MDMP)

`info` gives the exception (code, address, thread), `modules` gives every module's
PDB name and ident straight from the dump's CodeView records, `stack` scans the
crashing thread's captured stack, `symbolize` names the frames when the PDBs are
in `--symdir`. For a proper unwound stack use rust-minidump's `minidump-stackwalk`.

## Recipe: "is this the right PDB?"

```
python3 ${SKILL_DIR}/kdump.py pdbcheck path/to/driver.sys path/to/driver.pdb
```

`verdict: MATCH` means the 16 GUID bytes are equal. Age differences between the
PE and the PDB's info stream are reported but do not make it a mismatch (the DBI
age is the one that normally equals the PE's). `MISMATCH` prints
`wanted_pdb_ident`, the ident of the PDB you actually need.

## What this tool does not do

- No `!analyze -v`: it does not guess the culprit. Combine `faulting_ip`,
  `broken_driver`, the decoded parameters and the stack scan yourself, and say
  which of them your conclusion rests on.
- No unwinding, no `!irp`, `!devobj`, `!wdfkd` or `!process`. Those need WinDbg
  on a Windows host, for example over ssh: `kd -z x.dmp -c "!analyze -v; lm; k; q"`.
  Pointer parameters (`kind` is `ptr`) are printed for exactly that use.
- Minidumps carry only the crashing thread's stack and a few data blocks; most
  memory is simply not in the file. A kernel-mode trap frame lies below the
  stack pointer and is usually not captured. "not captured" is a fact, not a bug.
- 32-bit full dumps: header and context only (the page-table walk is 64-bit only).

## Reporting

State the bugcheck, the faulting module and function with offset, and which
output lines they come from. Say what you could not determine from this tool
alone. When the user has WinDbg output for the same dump, put the two side by
side; `${SKILL_DIR}/references/verify-prompt.md` is a complete cross-check
procedure you can read with `read_file` and run when asked to verify the tool.
