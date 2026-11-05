# kdump verification run (windows-dump skill)

Fill in the block below, then paste this whole file as one message.

```
DUMP_MINI   = C:\Windows\Minidump\<pick one>.dmp
DUMP_FULL   = C:\Windows\MEMORY.DMP            (or a .dump /k /ka file)
DRIVER_SYS  = <path>\mydrv.sys                 (the build that was loaded when it crashed)
DRIVER_PDB  = <path>\mydrv.pdb                 (the PDB that was called "mismatched" yesterday)
OLD_PDB     = <path>\older build\mydrv.pdb     (any PDB from a different build, for the negative test)
SYM_DIR      = <existing symbol cache, e.g. C:\Symbols, or an empty dir>
SYM_SERVER  = <internal symbol server URL, or "none">
WINDBG_HOST      = <"local" if kd.exe/windbg is on this machine, or "ssh <host>" if reachable over ssh, or "none">
KDUMP_DIR       = <directory of the windows-dump skill: agent_cli/skills/builtin/windows-dump>
OUT_DIR         = <empty directory for results>
```

## Rules for this task

- Use `KDUMP_DIR/kdump.py` for every read of a dump, PE or PDB. Read `KDUMP_DIR/SKILL.md` first.
  Do not write your own dump parser, do not compute symbol offsets by hand, do not
  judge PE/PDB matching by anything other than `kdump.py pdbcheck`.
- Every kdump command also runs with `--json`; save both the text and the json to `OUT_DIR`.
- Record every command you ran and its exit code in `OUT_DIR/commands.log`.
- When a step fails, save the full error output, write one line in the report, and
  continue with the next step. Do not try to fix `kdump.py`; the author will.
- Do not install packages. Python 3.10+ standard library only.
- Report facts only: what printed, what matched, what differed, with both values.

## Step 1 — does it open

```
python3 KDUMP_DIR/kdump.py selftest
python3 KDUMP_DIR/kdump.py info DUMP_MINI
python3 KDUMP_DIR/kdump.py info DUMP_FULL
python3 KDUMP_DIR/kdump.py modules DUMP_FULL
python3 KDUMP_DIR/kdump.py stack DUMP_FULL
```

Record: `python3 --version`; for each command the exit code and wall time; file size
of each dump; `dump_type`, `dump_type_name`, `os_build`, `os_guess`, `bugcheck.code`
and `bugcheck.name` from each `info`. If any command exits 2, copy the message verbatim
(it names the structure that failed).

## Step 2 — WinDbg oracle (skip if WINDBG_HOST = none)

For DUMP_FULL and DUMP_MINI, produce `OUT_DIR/windbg-<name>.txt` with:

```
kd -z <dump> -logo OUT_DIR/windbg-<name>.txt -c ".dumpdebug; .bugcheck; r; lm; lmv; k; dps @rsp L200; !lmi nt; q"
```

(over ssh: `ssh <host> 'kd -z ... -c "..."'`; if `kd` is not on the search path use the full path to
`Debuggers\x64\kd.exe`.) Then compare, writing a table to the report with one row per
check: check, WinDbg value, kdump value, same yes/no.

1. `.dumpdebug` vs `info --json`: DumpType, DirectoryTableBase, PsLoadedModuleList,
   PsActiveProcessHead, KdDebuggerDataBlock, MachineImageType, NumberProcessors,
   BugCheckCode and the four parameters, MinorVersion (= build), SystemTime.
   For a full/bitmap dump also compare the number of physical pages / bitmap size.
2. `.bugcheck` vs `info`: code and parameters.
3. `r` vs `info` → `context`: rip, rsp, rbp, rax..r15, cs/ss, eflags (eip/esp on x86).
4. `lm` vs `modules --json`: module count, and for every module start, end, name.
   List any module present on one side only. For `ntoskrnl.exe` and DRIVER_SYS's
   module also compare the timestamp shown by `lmv` with `timestamp`.
5. `k` vs `stack --json`: write down the return addresses from `k` in order. Each one
   must appear in the kdump scan in the same relative order. Extra lines in the scan
   are expected; a `k` address missing from the scan is a finding.
6. `!lmi nt` vs `modules --json` for `ntoskrnl.exe`: PDB name, GUID, Age (pdb_ident).

## Step 3 — the PDB that was called "mismatched"

```
python3 KDUMP_DIR/kdump.py pdbcheck DRIVER_SYS DRIVER_PDB
python3 KDUMP_DIR/kdump.py pdbcheck DRIVER_SYS OLD_PDB
```

Record for the first run: `verdict`, `pe_age`, `info_age`, `dbi_age`, the GUID ident,
`symbol_server_path`, exit code. Expected `MATCH`; if it says `MISMATCH`, save both
`pdb_ident` values and stop here. Record for the second run: expected `MISMATCH` with
`wanted_pdb_ident` equal to the ident from the first run.

If WINDBG_HOST is available: `kd -z DUMP_FULL -c "!lmi <drivername>; q"` and compare GUID
and Age with the pdbcheck output.

Then symbolize with the in-house driver:

```
python3 KDUMP_DIR/kdump.py symbolize DUMP_FULL --symdir SYM_DIR --pe <drivername>.sys=DRIVER_SYS --pdb <drivername>.sys=DRIVER_PDB
```

Save the full output. The symbol resolution trail printed before the frames must show
the driver module with `pe: found`, `pdb: found`, no `MISMATCH`. Pick three frames that
fall inside the driver module and, if WINDBG_HOST is available, run `ln <addr>` on each in
WinDbg. Table: addr, kdump symbol+offset, WinDbg symbol+offset, same yes/no. Note
whether kdump names static (non-exported) functions: look for `kind: proc` in the
json; a private PDB should produce them.

## Step 4 — symbol store

```
python3 KDUMP_DIR/kdump.py fetch-symbols DUMP_FULL --symdir SYM_DIR --only ntoskrnl.exe hal.dll
```

If SYM_SERVER is not "none", run it again with `--server SYM_SERVER`. Record which
modules came back found, which "not on server", and any network error verbatim.
Then `symbolize DUMP_FULL --symdir SYM_DIR` without `--pe/--pdb`: confirm that modules
without a PDB are printed as `module+rva` with a note, not with an invented name.
Confirm the first frame of DUMP_FULL resolves to `nt!KeBugCheckEx` (or state what it
resolves to).

## Step 5 — stress and edge cases (only if time remains)

- Timing: `modules` and `stack` on the largest MEMORY.DMP available; record seconds.
- If any dump's `info` lists `unloaded_drivers`, check whether any stack-scan value
  falls inside one of those ranges. Currently `stack` matches loaded modules only, so
  such an address would print without a module. Report every stack value that lands
  in an unloaded range: value, range, driver name.
- Bugcheck parameter meanings: for each distinct bugcheck code across the dumps,
  compare the parameter descriptions printed by `info` with the `Arg1..Arg4`
  descriptions in `!analyze -v`. Report wording that disagrees in substance.
- If a `.dump /ka` (KernelAndUser) file exists: does `modules` list only kernel
  modules, or user-mode DLLs too? Report what appears.
- If running on Windows with a Korean locale: run `info` on a dump under a path with
  Korean characters; report any encoding error or garbled output.

## Step 6 — the analysis itself

Only after Steps 1–4: analyze DUMP_FULL as a crash. State the bugcheck, the faulting
module and function with offset, how you know (which kdump output lines), and what
you could not determine from this tool alone. If WINDBG_HOST is available, put the
`!analyze -v` sections `MODULE_NAME`, `IMAGE_NAME`, `FAILURE_BUCKET_ID` and
`STACK_TEXT` next to your conclusion and say where they agree or differ.

## Report

Write `OUT_DIR/report.md` with these headings, in this order: Environment (python,
OS, kdump version line from `kdump.py --help` if any), Step 1, Step 2 table, Step 3,
Step 4, Step 5, Step 6, Findings. "Findings" is a numbered list of every disagreement
or failure, each with: step, field, kdump value, WinDbg value, `dump_type` of the dump
involved. If there are none, write "no findings". Finish by listing the files in `OUT_DIR`.
